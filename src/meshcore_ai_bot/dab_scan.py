"""One local Band III scan; restore tuning and ASA setting even on interruption."""

import contextlib
import fcntl
import json
import os
import signal
import tempfile
import time
from pathlib import Path
from urllib.request import Request, urlopen

try:
    from .dab_control import CONTROL_PATH, PRESETS, fresh_signal, load_settings, save_settings
except ImportError:
    from dab_control import CONTROL_PATH, PRESETS, fresh_signal, load_settings, save_settings

INVENTORY = CONTROL_PATH.with_name("dab-ensembles.json")
SCAN_STATE = CONTROL_PATH.with_name("dab-scan.json")


class ScanCancelled(Exception):
    pass


def read_json(path, default):
    try:
        data = Path(path).read_bytes()
        if len(data) > 1_000_000:
            return default
        return json.loads(data)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".dab-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def scan_status():
    result = read_json(SCAN_STATE, {"running": False})
    if result.get("running"):
        pid = result.get("pid")
        if type(pid) is int and pid > 0:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                result = {**result, "running": False, "error": "Suchlauf unterbrochen; Empfangskanal prüfen."}
        elif time.time() - result.get("updated_at", 0) > 45:
            result = {**result, "running": False, "error": "Suchlauf unterbrochen; Empfangskanal prüfen."}
    return result


def mux():
    with urlopen("http://127.0.0.1:7979/mux.json", timeout=3) as response:
        return json.loads(response.read(500000))


def tune(channel):
    save_settings({"channel": channel})
    request = Request("http://127.0.0.1:7979/channel", channel.encode(), method="POST")
    with urlopen(request, timeout=6) as response:
        response.read(1000)
    with urlopen("http://127.0.0.1:7979/channel", timeout=3) as response:
        if response.read(32).decode().strip() != channel:
            raise RuntimeError("Empfangskanal nicht bestätigt")


def ensemble_record(channel, data, now):
    label = str(data.get("ensemble", {}).get("label", {}).get("label", "")).strip()[:100]
    services = [
        {
            "sid": str(s.get("sid", ""))[:16],
            "name": str(s.get("label", {}).get("label", "")).strip()[:80],
            "audio": any(c.get("transportmode") == "audio" for c in s.get("components", [])),
        }
        for s in data.get("services", [])[:100]
    ]
    if not label or not services or not fresh_signal(data, now):
        return None
    return {
        "channel": channel,
        "name": label,
        "frequency_mhz": PRESETS[channel]["frequency_mhz"],
        "snr": data.get("demodulator", {}).get("snr"),
        "services": services,
        "asa_observed": data.get("asa", {}).get("ews_ensemble") is True,
        "checked_at": now,
    }


def scan():
    SCAN_STATE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with SCAN_STATE.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = load_settings()
        found = []
        state = {
            "running": True,
            "pid": os.getpid(),
            "started_at": time.time(),
            "total": len(PRESETS),
            "index": 0,
            "restore_channel": previous["channel"],
        }

        def publish(**updates):
            state.update(updates, updated_at=time.time(), found=len(found))
            write_json(SCAN_STATE, state)

        def interrupted(signum, frame):
            raise ScanCancelled("Suchlauf beendet")

        old_handlers = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}
        publish()
        error = None
        save_settings({"forward_asa": False})
        try:
            for index, channel in enumerate(PRESETS, 1):
                publish(index=index, channel=channel)
                tune(channel)
                record = None
                for _step in range(4):
                    time.sleep(1)
                    with contextlib.suppress(OSError, ValueError):
                        record = ensemble_record(channel, mux(), time.time()) or record
                if record:
                    # Collect the slower service-label cycle on occupied blocks.
                    for _step in range(6):
                        time.sleep(1)
                        with contextlib.suppress(OSError, ValueError):
                            fresh = ensemble_record(channel, mux(), time.time())
                            if fresh:
                                fresh["asa_observed"] |= record["asa_observed"]
                                record = fresh
                    found.append(record)
                    write_json(
                        INVENTORY,
                        {"version": 1, "checked_at": time.time(), "complete": False, "ensembles": found},
                    )
                publish()
        except (OSError, ValueError, RuntimeError, ScanCancelled) as exc:
            error = (
                "Suchlauf unterbrochen."
                if isinstance(exc, ScanCancelled)
                else "Empfangsprüfung fehlgeschlagen. Bitte Empfangskanal prüfen."
            )
        finally:
            restored = False
            try:
                tune(previous["channel"])
                restored = True
            except (OSError, RuntimeError):
                error = "Wiederherstellung des Empfangskanals fehlgeschlagen; ASA bleibt ausgeschaltet."
            save_settings({"forward_asa": previous["forward_asa"] if restored else False})
            write_json(
                INVENTORY,
                {"version": 1, "checked_at": time.time(), "complete": error is None, "ensembles": found},
            )
            publish(running=False, error=error)
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)


if __name__ == "__main__":
    scan()
