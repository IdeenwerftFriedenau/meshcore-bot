"""Maintain a private USB-only local model on an explicitly authorized Android."""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ADB_PORT = 18137
API_PORT = 18180
PHONE_ROOT = "/data/local/tmp/meshcore-ai-b12167-qwen35-2b"
STATE = Path("/var/lib/meshcore-ai-bot/phone/status.json")
MODEL_SHA = "aaf42c8b7c3cab2bf3d69c355048d4a0ee9973d48f16c731c0520ee914699223"


def credentials(env):
    serial = env.get("MESHCORE_ANDROID_SERIAL", "")
    key = env.get("MESHCORE_PHONE_API_KEY", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", serial):
        raise ValueError("Configure the private Android serial")
    if not re.fullmatch(r"[a-f0-9]{48,128}", key):
        raise ValueError("Configure a private random phone API key")
    return serial, key


def own_pid_script():
    return f"""ROOT={shlex.quote(PHONE_ROOT)}
own_pid() {{
 test -r "$ROOT/server.pid" || return 1
 p=$(cat "$ROOT/server.pid")
 case "$p" in ''|*[!0-9]*) return 1;; esac
 test "$(readlink /proc/$p/cwd)" = "$ROOT" || return 1
 tr '\\000' ' ' </proc/$p/cmdline | grep -F 'llama-server' >/dev/null || return 1
 kill -0 "$p" 2>/dev/null
}}
"""


def start_script(key):
    # Key travels only over authenticated ADB stdin, never command arguments/logs.
    if not re.fullmatch(r"[a-f0-9]{48,128}", key):
        raise ValueError("Invalid key")
    return own_pid_script() + f"""
test -x "$ROOT/runtime/llama-server" && test -f "$ROOT/model.gguf" || exit 4
chmod 700 "$ROOT"
cd "$ROOT" || exit 4
if own_pid; then echo running; exit 0; fi
test "$(sha256sum model.gguf | cut -d ' ' -f 1)" = '{MODEL_SHA}' || exit 6
umask 077
cat >api-key <<'MESHCORE_PRIVATE_KEY_END'
{key}
MESHCORE_PRIVATE_KEY_END
chmod 600 api-key
export LD_LIBRARY_PATH="$ROOT/runtime"
nohup setsid "$ROOT/runtime/llama-server" -m "$ROOT/model.gguf" \\
 --host 127.0.0.1 --port {API_PORT} -t 4 -tb 4 -c 4096 -b 64 -ub 32 \\
 -ngl 0 -np 1 --no-webui --alias motorola-local --chat-template-kwargs '{{"enable_thinking":false}}' --api-key-file "$ROOT/api-key" \\
 >server.log 2>&1 </dev/null &
echo $! >server.pid
echo started
"""


def write_state(state, path=STATE, now=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".phone-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({"state": state, "phone": "Motorola", "model": "Qwen3.5 2B",
                       "checked_at": time.time() if now is None else now}, stream)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Phone:
    def __init__(self, serial, key, runner=subprocess.run):
        self.serial, self.key, self.runner = serial, key, runner
        self.base = ["/usr/bin/adb", "-P", str(ADB_PORT)]

    def adb(self, *args, script=None, device=True, timeout=12):
        command = self.base + (["-s", self.serial] if device else []) + list(args)
        return self.runner(command, input=script, capture_output=True, text=True, timeout=timeout, check=False)

    def prepare(self):
        self.adb("start-server", device=False)
        result = self.adb("get-state")
        if result.returncode or result.stdout.strip() != "device":
            return "disconnected"
        result = self.adb("shell", "-T", "sh", script=start_script(self.key), timeout=40)
        if result.returncode:
            return "files_missing" if result.returncode in (4, 6) else "error"
        if self.adb("forward", f"tcp:{API_PORT}", f"tcp:{API_PORT}").returncode:
            return "error"
        return "loading"

    def healthy(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{API_PORT}/v1/models", headers={"Authorization": "Bearer " + self.key}
        )
        # The endpoint is fixed loopback and redirects cannot forward the key.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
        with opener.open(request, timeout=4) as response:
            data = json.loads(response.read(32000))
        return any(row.get("id") == "motorola-local" for row in data.get("data", []))

    def stop(self):
        self.adb("shell", "-T", "sh", script=own_pid_script() + 'if own_pid; then kill "$p"; fi\n')
        self.adb("forward", "--remove", f"tcp:{API_PORT}")


def main():
    serial, key = credentials(os.environ)
    phone = Phone(serial, key)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    previous = None
    try:
        while not stop.is_set():
            try:
                state = phone.prepare()
                if state == "loading" and phone.healthy():
                    state = "ready"
            except (OSError, ValueError, subprocess.SubprocessError):
                state = "disconnected"
            write_state(state)
            if state != previous:
                print("Motorola local AI:", state, flush=True)
                previous = state
            stop.wait(15)
    finally:
        try:
            phone.stop()
        except (OSError, subprocess.SubprocessError):
            pass
        write_state("stopped")


if __name__ == "__main__":
    main()
