import importlib.util
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.fixture
def console(tmp_path):
    root = Path(__file__).parents[1] / "deploy/local-console"
    spec = importlib.util.spec_from_file_location("local_console", root / "local_dashboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ASSETS = root
    module.OUTBOX = tmp_path / "outbox.sqlite3"
    module.INBOX = tmp_path / "inbox.sqlite3"
    module.RECORDINGS = tmp_path / "recordings"
    module.CONTROL_PATH = tmp_path / "dab-control.json"
    return module


def test_asa_receiver_and_warning_are_separate_states(console):
    now = 1000
    mux = {"asa": {"active": False, "ews_ensemble": True}, "demodulator": {"time_last_fct0_frame": 999000}}
    result = console.asa_diagnostics(mux, now)
    assert result["signal_fresh"] and result["asa_data_present"]
    assert result["ews_ensemble"] and not result["emergency_warning"]
    assert result["asa_forwarding"] is False
    mux["asa"]["active"] = True
    mux["asa"]["emergency_warning"] = True
    assert console.asa_diagnostics(mux, now)["emergency_warning"]


@pytest.mark.parametrize("frame", [None, 0, 900000, 1200000, True])
def test_old_missing_or_invalid_dab_timestamps_are_not_ready(console, frame):
    result = console.asa_diagnostics(
        {"asa": {"active": False}, "demodulator": {"time_last_fct0_frame": frame}}, 1000
    )
    assert result["signal_fresh"] is False


def test_missing_asa_is_not_reported_as_present(console):
    result = console.asa_diagnostics({"demodulator": {"time_last_fct0_frame": 999000}}, 1000)
    assert result["signal_fresh"] and not result["asa_data_present"]


def test_channel_filter_reads_older_messages_beyond_global_latest_limit(console):
    now = time.time()
    with sqlite3.connect(console.INBOX) as db:
        db.execute("CREATE TABLE messages(channel TEXT,text TEXT,received REAL,state TEXT)")
        db.execute(
            "INSERT INTO messages VALUES(?,?,?,?)",
            ("#bot", "older selected message", now - 500, "local_queued"),
        )
        db.execute(
            "INSERT INTO messages VALUES(?,?,?,?)", ("#bot", "outside retention", now - 90000, "local_queued")
        )
        db.executemany(
            "INSERT INTO messages VALUES(?,?,?,?)",
            [("#test", f"newer {n}", now - n, "received") for n in range(120)],
        )
    assert len(console.inbox_state()["latest"]) == 100
    selected = console.inbox_state("#bot")
    assert [row["text"] for row in selected["latest"]] == ["older selected message"]
    assert selected["counts"] == {"local_queued": 1}
    assert selected["channels"] == ["#bot", "#test"]
    assert console.inbox_state("' OR 1=1 --")["latest"] == []


def test_offline_assets_and_manual_send_stay_local(console, monkeypatch):
    monkeypatch.setattr(console, "local_json", lambda path: None)
    monkeypatch.setattr(console, "service", lambda name: "active")
    server = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        for path in ["/", "/console.css", "/console.js", "/notfall", "/api/status"]:
            with urllib.request.urlopen(base + path, timeout=3) as response:
                assert response.status == 200
                assert response.headers["X-Content-Type-Options"] == "nosniff"
                assert response.read()
        for channel, origin in [("Public", None), ("#test", "https://foreign.example")]:
            headers = {"Content-Type": "application/json"}
            if origin:
                headers["Origin"] = origin
            request = urllib.request.Request(
                base + "/api/send", json.dumps({"channel": channel, "text": "test"}).encode(), headers=headers
            )
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            assert error.value.code == 400
        request = urllib.request.Request(
            base + "/api/send",
            b'{"channel":"#test","text":"isolated test queue"}',
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 202
        assert console.local_outbox()[0]["status"] == "queued"
        request = urllib.request.Request(
            base + "/api/dab/settings",
            b'{"channel":"7D","forward_asa":true}',
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 202
        assert console.load_settings(console.CONTROL_PATH)["channel"] == "7D"
        request = urllib.request.Request(
            base + "/api/dab/settings",
            b'{"channel":"7D;reboot"}',
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_live_radio_streams_only_current_audio_service(console, monkeypatch):
    import io

    monkeypatch.setattr(console, "current_dab_channel", lambda: "5C")

    monkeypatch.setattr(console, "scan_status", lambda: {"running": False})
    monkeypatch.setattr(
        console,
        "local_json",
        lambda path: {
            "demodulator": {"time_last_fct0_frame": time.time() * 1000},
            "services": [{"sid": "0xd210", "components": [{"transportmode": "audio"}]}],
        },
    )
    requested = []

    def stream(url, timeout):
        requested.append(url)
        return io.BytesIO(b"local-radio-audio")

    monkeypatch.setattr(console, "urlopen", stream)
    server = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(base + "/radio/0xd210", timeout=3) as response:
            assert response.headers["Content-Type"] == "audio/mpeg"
            assert response.read() == b"local-radio-audio"
        for path, status in [("/radio/0xdead", 409), ("/radio/http://external", 404)]:
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + path, timeout=3)
            assert error.value.code == status
        monkeypatch.setattr(console, "scan_status", lambda: {"running": True})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(base + "/radio/0xd210", timeout=3)
        assert error.value.code == 409
        assert requested == ["http://127.0.0.1:7979/mp3/0xd210"]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("unavailable", [0, 1, 4, 5])
def test_radio_waits_for_receiver_audio_handler(console, monkeypatch, unavailable):
    import io

    attempts = []
    monkeypatch.setattr(console, "current_dab_channel", lambda: "7D")
    monkeypatch.setattr(console, "scan_status", lambda: {"running": False})
    monkeypatch.setattr(console.time, "sleep", lambda duration: None)

    def stream(url, timeout):
        attempts.append(url)
        if len(attempts) <= unavailable:
            raise urllib.error.HTTPError(url, 503, "Audio handler not ready", {}, io.BytesIO())
        return io.BytesIO(b"ready audio")

    monkeypatch.setattr(console, "urlopen", stream)
    if unavailable == 5:
        with pytest.raises(urllib.error.HTTPError):
            console.open_radio("0xd321")
    else:
        with console.open_radio("0xd321") as response:
            assert response.read() == b"ready audio"
    assert attempts == ["http://127.0.0.1:7979/mp3/0xd321"] * min(unavailable + 1, 5)


def test_radio_retry_stops_if_another_listener_retunes(console, monkeypatch):
    import io

    channels = iter(["7D", "5C"])
    calls = []
    monkeypatch.setattr(console, "current_dab_channel", lambda: next(channels))
    monkeypatch.setattr(console, "scan_status", lambda: {"running": False})
    monkeypatch.setattr(console.time, "sleep", lambda duration: None)

    def stream(url, timeout):
        calls.append(url)
        raise urllib.error.HTTPError(url, 503, "Preparing audio", {}, io.BytesIO())

    monkeypatch.setattr(console, "urlopen", stream)
    with pytest.raises(OSError, match="Receiver changed"):
        console.open_radio("0xd321")
    assert len(calls) == 1
