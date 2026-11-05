"""Shared local DAB controls. Standard library only, also used by the receiver."""

import fcntl
import json
import os
import tempfile
from pathlib import Path

CONTROL_PATH = Path(os.environ.get("MESHCORE_DAB_CONTROL", "/var/lib/meshcore-gateway/dab-control.json"))
PRESETS = {
    "5C": {"name": "Deutschlandfunk / Bundesmux", "frequency_mhz": 178.352},
    "7D": {"name": "RBB Berlin", "frequency_mhz": 194.064},
    "10B": {"name": "RBB Brandenburg / Berlin", "frequency_mhz": 211.648},
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
    return {k: data[k] for k in ("version", "channel", "forward_asa")}


def save_settings(updates, path=CONTROL_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_suffix(".lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _save_settings(updates, path)


def _save_settings(updates, path):
    if not isinstance(updates, dict) or not updates or set(updates) - {"channel", "forward_asa"}:
        raise ValueError("Unknown DAB setting")
    data = load_settings(path)
    data.update(updates)
    if data["channel"] not in PRESETS or type(data["forward_asa"]) is not bool:
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
