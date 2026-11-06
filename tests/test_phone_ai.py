import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "phone_ai", Path(__file__).parents[1] / "deploy/android_phone_ai.py"
)
phone_ai = importlib.util.module_from_spec(spec)
spec.loader.exec_module(phone_ai)
KEY = "a" * 48


@pytest.mark.parametrize("serial,key", [("", KEY), ("device;reboot", KEY), ("test", "short"), ("test", "x" * 48)])
def test_private_device_configuration_rejects_invalid_values(serial, key):
    with pytest.raises(ValueError):
        phone_ai.credentials({"MESHCORE_ANDROID_SERIAL": serial, "MESHCORE_PHONE_API_KEY": key})


def test_unauthorized_device_does_not_start_or_forward_a_model():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0 if len(calls) == 1 else 1, stdout="", stderr="unauthorized")

    phone = phone_ai.Phone("test-device", KEY, runner)
    assert phone.prepare() == "disconnected"
    assert len(calls) == 2
    assert all("18137" in cmd and "5037" not in cmd for cmd, _ in calls)
    assert all(KEY not in " ".join(cmd) for cmd, _ in calls)


def test_private_phone_start_keeps_key_out_of_arguments_and_uses_own_forward():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="device\n" if command[-1] == "get-state" else "started\n")

    phone = phone_ai.Phone("test-device", KEY, runner)
    assert phone.prepare() == "loading"
    shell = next((cmd, opts) for cmd, opts in calls if "shell" in cmd)
    assert KEY not in " ".join(shell[0]) and KEY in shell[1]["input"]
    assert "--host 127.0.0.1" in shell[1]["input"]
    assert "--api-key-file" in shell[1]["input"]
    assert calls[-1][0][-3:] == ["forward", "tcp:18180", "tcp:18180"]
    assert all("--remove-all" not in cmd and "kill-server" not in cmd for cmd, _ in calls)
    subprocess.run(["sh", "-n"], input=shell[1]["input"], text=True, check=True)


def test_status_is_private_and_contains_no_serial_or_key(tmp_path):
    path = tmp_path / "phone" / "status.json"
    phone_ai.write_state("ready", path, now=100)
    data = json.loads(path.read_text())
    assert data == {"state": "ready", "phone": "Motorola", "model": "Qwen3.5 2B", "checked_at": 100}
    assert path.stat().st_mode & 0o777 == 0o600
    assert KEY not in path.read_text()


def test_health_uses_authenticated_loopback_without_proxy_or_redirect(monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, maximum):
            assert maximum <= 32000
            return b'{"data":[{"id":"motorola-local"}]}'

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            assert timeout == 4
            return Response()

    def build(*handlers):
        assert handlers[0].proxies == {}
        redirect = handlers[1]()
        assert redirect.redirect_request(None, None, 302, "", {}, "https://external.invalid") is None
        return Opener()

    monkeypatch.setattr(phone_ai.urllib.request, "build_opener", build)
    assert phone_ai.Phone("test-device", KEY).healthy()
    assert calls[0].full_url == "http://127.0.0.1:18180/v1/models"
    assert calls[0].get_header("Authorization") == "Bearer " + KEY
