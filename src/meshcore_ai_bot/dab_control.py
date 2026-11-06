"""Shared local DAB controls. Standard library only, also used by the receiver."""

import fcntl
import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CONTROL_PATH = Path(os.environ.get("MESHCORE_DAB_CONTROL", "/var/lib/meshcore-gateway/dab-control.json"))
PRESETS = {
    "5C": {"name": "Deutschlandfunk / Bundesmux", "frequency_mhz": 178.352},
    "7D": {"name": "RBB Berlin", "frequency_mhz": 194.064},
    "10B": {"name": "RBB Brandenburg / Berlin", "frequency_mhz": 211.648},
}
# DAB Band III block frequencies, as used by the installed receiver.
_BLOCKS = "5A 174928 5B 176640 5C 178352 5D 180064 6A 181936 6B 183648 6C 185360 6D 187072 7A 188928 7B 190640 7C 192352 7D 194064 8A 195936 8B 197648 8C 199360 8D 201072 9A 202928 9B 204640 9C 206352 9D 208064 10A 209936 10B 211648 10C 213360 10D 215072 11A 216928 11B 218640 11C 220352 11D 222064 12A 223936 12B 225648 12C 227360 12D 229072 13A 230784 13B 232496 13C 234208 13D 235776 13E 237488 13F 239200".split()
PRESETS = {
    name: {
        "name": PRESETS.get(name, {}).get("name", "DAB+ Block " + name),
        "frequency_mhz": int(frequency) / 1000,
    }
    for name, frequency in zip(_BLOCKS[::2], _BLOCKS[1::2], strict=True)
}


def load_settings(path=CONTROL_PATH):
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {"version": 1, "channel": "5C", "forward_asa": False}
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("Invalid local DAB settings")
    if data.get("channel") not in PRESETS or type(data.get("forward_asa")) is not bool:
        raise ValueError("Invalid DAB channel or ASA setting")
    if type(data.get("rbb_news_enabled", False)) is not bool:
        raise ValueError("Invalid RBB news setting")
    result = {k: data[k] for k in ("version", "channel", "forward_asa")}
    if "rbb_news_enabled" in data:
        result["rbb_news_enabled"] = data["rbb_news_enabled"]
    return result


def save_settings(updates, path=CONTROL_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_suffix(".lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _save_settings(updates, path)


def _save_settings(updates, path):
    if not isinstance(updates, dict) or not updates or set(updates) - {"channel", "forward_asa", "rbb_news_enabled"}:
        raise ValueError("Unknown DAB setting")
    data = load_settings(path)
    data.update(updates)
    if (data["channel"] not in PRESETS or type(data["forward_asa"]) is not bool
            or type(data.get("rbb_news_enabled", False)) is not bool):
        raise ValueError("Invalid DAB channel or ASA setting")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".dab-control-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return data


def fresh_signal(mux, now):
    demod = mux.get("demodulator", {})
    frame = demod.get("time_last_fct0_frame")
    changed = mux.get("receiver", {}).get("software", {}).get("lastchannelchange", 0)
    return (
        type(frame) in (int, float)
        and -5 <= now - frame / 1000 < 30
        and type(changed) in (int, float)
        and frame >= changed
    )


def news_window(now=None):
    """Reserve the single tuner from HH:59:30 through HH:05:50 for news."""
    second = (time.time() if now is None else now) % 3600
    return second >= 3570 or second < 350


def effective_channel(settings, now=None):
    if news_window(now):
        return "7D" if settings.get("rbb_news_enabled", False) and rbb_news_window(now) else "5C"
    return settings["channel"]


def rbb_news_window(now=None):
    """Odd Berlin hours use RBB; pre-tune 30 seconds before the full hour."""
    now = time.time() if now is None else now
    local = datetime.fromtimestamp(now + 30, ZoneInfo("Europe/Berlin"))
    return local.hour % 2 == 1 and news_window(now)


def scan_allowed(now=None, settings=None):
    # Leave ten minutes for the full scan before the next bulletin reservation.
    now = time.time() if now is None else now
    second = now % 3600
    return 350 <= second < 2970
