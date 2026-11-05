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


def test_radio_hub_shares_source_and_one_disconnect_keeps_other_listener(console):
    import queue

    pieces = queue.Queue()
    closed = threading.Event()
    opens = []

    class Source:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.set()

        def read(self, size):
            return pieces.get(timeout=2)

    def opener():
        opens.append(True)
        return Source()

    hub = console.RadioHub()
    first = hub.subscribe(("5C", 1, "0x15dc"), opener)
    second = hub.subscribe(("5C", 1, "0x15dc"), opener)
    assert first.stream.ready.wait(1)
    assert len(opens) == 1
    assert hub.status()["listeners"] == 2
    pieces.put(b"shared audio")
    assert first.read() == second.read() == b"shared audio"
    first.close()
    pieces.put(b"still playing")
    assert second.read() == b"still playing"
    assert hub.status()["listeners"] == 1
    second.close()
    pieces.put(b"")
    assert closed.wait(1)
    assert hub.status()["listeners"] == 0


def test_slow_radio_listener_is_dropped_without_stalling_others(console):
    hub = console.RadioHub()
    stream = console.RadioStream(hub, ("5C", 1, "0x15dc"), lambda: None)
    slow, fast = stream.add(), stream.add()
    for i in range(32):
        chunk = str(i).encode()
        assert stream.publish(chunk)
        assert fast.read() == chunk
    assert stream.publish(b"newest")
    assert slow.dropped.is_set()
    assert slow.read() == b""
    assert fast.read() == b"newest"
    assert not fast.dropped.is_set()
    fast.close()


def test_radio_generation_change_never_reuses_old_receiver_stream(console):
    import queue

    pieces = queue.Queue()
    opened = threading.Event()

    class Source:
        def __enter__(self):
            opened.set()
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            return pieces.get(timeout=2)

    hub = console.RadioHub()
    old = hub.subscribe(("5C", 1, "0x15dc"), Source)
    new = hub.subscribe(("5C", 2, "0x15dc"), Source)
    assert opened.wait(1)
    assert old.stream is not new.stream
    old.close()
    new.close()
    pieces.put(b"")
    pieces.put(b"")


def test_radio_source_failure_wakes_all_waiters_and_removes_stream(console):
    hub = console.RadioHub()

    def unavailable():
        raise OSError("Receiver offline")

    listener = hub.subscribe(("5C", 1, "0x15dc"), unavailable)
    assert listener.stream.ready.wait(1)
    assert not listener.stream.opened
    assert listener.read() == b""
    listener.close()
    assert hub.status()["listeners"] == 0


def test_song_info_rejects_metadata_cached_before_retuning(console):
    mux = {
        "demodulator": {"time_last_fct0_frame": 1000000},
        "receiver": {"software": {"lastchannelchange": 990000}},
    }
    song = {"dls": {"time": 999, "label": "Artist - Song"}}
    assert console.song_info(song, mux, 1000)["text"] == "Artist - Song"
    song["dls"]["time"] = 989
    assert console.song_info(song, mux, 1000)["text"] == ""


def test_local_recording_is_saved_and_online_recognition_is_explicit(console, tmp_path):
    from types import SimpleNamespace

    calls = []

    class Listener:
        def __init__(self):
            ready = threading.Event()
            ready.set()
            self.stream = SimpleNamespace(ready=ready, buffered=ready, opened=True)
            self.remaining = 8

        def read(self):
            self.remaining -= 1
            return b"a" * 4096 if self.remaining >= 0 else b""

        def close(self):
            pass

    def subscribe(sid):
        return Listener(), {"station": "Radio", "channel": "5C"}

    def recognize(path):
        calls.append(path.read_bytes())
        return {"title": "Song", "artist": "Artist"}

    features = console.RadioFeatures(tmp_path, subscribe, recognize)
    record = features.start("record", "0xd210", 60)
    deadline = time.time() + 3
    while features.jobs[record["id"]]["status"] not in ("ready", "failed") and time.time() < deadline:
        time.sleep(0.01)
    job = features.jobs[record["id"]]
    assert job["status"] == "ready"
    assert (tmp_path / job["file"]).stat().st_size >= 16000
    assert not calls
    recognition = features.start("identify", "0xd210")
    deadline = time.time() + 3
    while features.jobs[recognition["id"]]["status"] not in ("matched", "failed") and time.time() < deadline:
        time.sleep(0.01)
    assert features.jobs[recognition["id"]]["result"]["title"] == "Song"
    assert len(calls) == 1
    assert not list(tmp_path.glob("*.part"))


def test_recording_inputs_and_concurrency_are_bounded(console, tmp_path):
    features = console.RadioFeatures(tmp_path, lambda sid: None)
    for sid, duration in [("http://external", 60), ("0xd210", 999999), ("0xd210", True)]:
        with pytest.raises(ValueError):
            features.start("record", sid, duration)
    features.jobs = {str(i): {"status": "capturing", "kind": "record"} for i in range(4)}
    with pytest.raises(ValueError):
        features.start("record", "0xd210", 60)


def test_late_listener_gets_prefill_without_reopening_source(console):
    hub = console.RadioHub()
    stream = console.RadioStream(hub, ("5C", 1, "0xd210"), lambda: None)
    first = stream.add()
    for index in range(16):
        stream.publish(bytes([index]) * 4096)
        first.read()
    assert stream.buffered.is_set()
    second = stream.add()
    assert second.read() == bytes([0]) * 4096
    first.close()
    assert second.read() == bytes([1]) * 4096
    second.close()
