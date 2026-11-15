"""rbb-Aufnahme bleibt robust, wenn der lokale DAB-Empfänger kurz nicht antwortet."""

import asyncio
import importlib.util
import os
from pathlib import Path

import httpx
import pytest

spec = importlib.util.spec_from_file_location(
    "rbb_news_archiv", Path(__file__).parents[1] / "deploy/funk-scan/rbb_news.py"
)
news = importlib.util.module_from_spec(spec)
spec.loader.exec_module(news)

SLOT = 1_700_000_000 // 3600 * 3600


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


class Response:
    def __init__(self, text="", data=None):
        self.text, self._data = text, data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class Client:
    """Antwortet erst nach `fail` Anfragen; vorher ConnectTimeout wie ein überlasteter welle-cli."""

    def __init__(self, clock, fail=0, channel="7D", fail_after=None):
        self.clock, self.fail, self.channel, self.calls = clock, fail, channel, 0
        self.fail_after = fail_after

    async def get(self, url, **_):
        self.calls += 1
        self.clock.now += 0.1
        if self.calls <= self.fail or (self.fail_after is not None and self.calls > self.fail_after):
            raise httpx.ConnectTimeout("timed out")
        if url.endswith("/channel"):
            return Response(self.channel)
        return Response(data={"mux": True})


@pytest.fixture
def ready(monkeypatch, tmp_path):
    monkeypatch.setattr(news, "ready", lambda mux, channel, now: channel == "7D")
    monkeypatch.setattr(news, "ROOT", tmp_path / "news")
    monkeypatch.setattr(news, "ARCHIVE", tmp_path / "archiv")
    (tmp_path / "news").mkdir()


def run(coro):
    return asyncio.run(coro)


def recorder(clock, sizes):
    calls = []

    async def fake(path, seconds, limit):
        calls.append(seconds)
        Path(path).write_bytes(b"\xff" * sizes[len(calls) - 1])  # noqa: ASYNC240
        clock.now += min(seconds, limit) if sizes[len(calls) - 1] else 10
        return 0 if sizes[len(calls) - 1] else 1

    return fake, calls


def test_connect_timeouts_at_top_of_hour_no_longer_skip_the_bulletin(ready):
    clock = Clock(SLOT)
    fake, calls = recorder(clock, [2_000_000])
    path = run(news.capture(Client(clock, fail=8), SLOT, clock, clock.sleep, fake))
    assert path.exists() and calls == [300]
    meta = (news.archive_base(SLOT).with_suffix(".json")).read_text()
    assert "rbb888" in meta


def test_receiver_dead_for_whole_window_reports_clear_stage(ready):
    clock = Clock(SLOT)
    with pytest.raises(news.StageError) as err:
        run(news.capture(Client(clock, fail=10_000), SLOT, clock, clock.sleep, recorder(clock, [0])[0]))
    assert err.value.stage == "empfang"
    assert "nicht erreichbar" in str(err.value) and "ConnectTimeout" in str(err.value)


def test_stream_that_breaks_at_once_is_restarted_and_shortened(ready):
    clock = Clock(SLOT)
    fake, calls = recorder(clock, [0, 2_000_000])
    path = run(news.capture(Client(clock), SLOT, clock, clock.sleep, fake))
    assert path.exists() and len(calls) == 2 and calls[1] <= 300


def test_finished_recording_is_kept_when_check_afterwards_times_out(ready):
    clock = Clock(SLOT)
    fake, _ = recorder(clock, [2_000_000])
    path = run(news.capture(Client(clock, fail_after=2), SLOT, clock, clock.sleep, fake))
    assert path.exists()
    assert "nicht bestätigt" in news.archive_base(SLOT).with_suffix(".json").read_text()


def test_wrong_channel_after_recording_is_still_rejected(ready):
    clock = Clock(SLOT)
    client = Client(clock)
    fake, _ = recorder(clock, [2_000_000])

    async def scenario():
        await news.wait_ready(client, SLOT, clock, clock.sleep)
        client.channel = "5C"
        return await news.capture(client, SLOT, clock, clock.sleep, fake)

    client.channel = "7D"
    orig = news.wait_ready

    async def ready_then_switch(c, slot, clk, slp):
        await orig(c, slot, clk, slp)
        c.channel = "5C"

    news.wait_ready = ready_then_switch
    try:
        with pytest.raises(news.StageError) as err:
            run(news.capture(client, SLOT, clock, clock.sleep, fake))
    finally:
        news.wait_ready = orig
    assert err.value.stage == "aufnahme"
    assert not (news.ROOT / f"rbb-{SLOT}.mp3").exists()


def test_recording_always_ends_before_tuner_switches_back(ready):
    clock = Clock(SLOT + 28)
    fake, calls = recorder(clock, [2_000_000])
    run(news.capture(Client(clock), SLOT, clock, clock.sleep, fake))
    assert SLOT + 28 + calls[0] <= SLOT + news.CAPTURE_END


class Store:
    def reserve_model_request(self, *args):
        return True


class Ai:
    base_url, completion_path, api_key, model, timeout_seconds = "http://ki", "/v1", "", "m", 30
    requests_per_minute = requests_per_day = 10


class Config:
    ai = Ai()

    class radio:
        packet_bytes = 110


def test_summary_retries_when_ai_gateway_connection_times_out(monkeypatch):
    attempts = []

    async def flaky(client, method, url, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise httpx.ConnectTimeout("timed out")
        return {"choices": [{"message": {"content": '{"is_news": false}'}}]}

    import meshcore_ai_bot.http as http
    monkeypatch.setattr(http, "read_json", flaky)
    clock = Clock(SLOT)
    assert run(news.summarize(Config, Store(), None, "x" * 300, SLOT, sleep=clock.sleep)) == []
    assert len(attempts) == 3


def test_summary_gives_clear_stage_when_gateway_stays_down(monkeypatch):
    async def down(client, method, url, **kwargs):
        raise httpx.ConnectTimeout("timed out")

    import meshcore_ai_bot.http as http
    monkeypatch.setattr(http, "read_json", down)
    clock = Clock(SLOT)
    with pytest.raises(news.StageError) as err:
        run(news.summarize(Config, Store(), None, "x" * 300, SLOT, sleep=clock.sleep))
    assert err.value.stage == "auswertung"


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    """dab_monitor.py so laden, wie es auf dem Pi liegt: mit config.yaml und dab_control.py daneben."""
    import shutil
    import sys

    root = Path(__file__).parents[1]
    shutil.copy(root / "deploy/dab/dab_monitor.py", tmp_path / "dab_monitor.py")
    shutil.copy(root / "src/meshcore_ai_bot/dab_control.py", tmp_path / "dab_control.py")
    (tmp_path / "config.yaml").write_text("dab: {}\n")
    monkeypatch.setenv("DAB_INGEST_URL", "http://example.invalid/ingest")
    monkeypatch.setenv("INGEST_TOKEN", "x")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "dab_control", raising=False)
    pytest.importorskip("aiohttp")
    pytest.importorskip("yaml")
    spec = importlib.util.spec_from_file_location("dab_monitor_test", tmp_path / "dab_monitor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_monitor_restarts_welle_when_it_runs_out_of_files(monitor):
    assert not monitor.welle_stuck(0, 0)
    assert not monitor.welle_stuck(monitor.FD_EXHAUSTED_LIMIT - 1, monitor.FAILED_CYCLES_LIMIT - 1)
    assert monitor.welle_stuck(monitor.FD_EXHAUSTED_LIMIT, 0)
    assert monitor.welle_stuck(0, monitor.FAILED_CYCLES_LIMIT)


def test_monitor_restarts_leaking_welle_early_but_never_during_news(monitor):
    hour = 1_700_000_000 // 3600 * 3600
    assert monitor.preemptive_restart(900, hour + 1200)
    assert not monitor.preemptive_restart(300, hour + 1200)
    assert not monitor.preemptive_restart(None, hour + 1200)
    assert not monitor.preemptive_restart(900, hour + 60)       # Nachrichten laufen
    assert not monitor.preemptive_restart(900, hour + 3590)     # Vorlauf HH:59:30
    assert monitor.open_fds(os.getpid()) > 0
