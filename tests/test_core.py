import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshcore_ai_bot.broadcasts import dwd_warnings, warning_text
from meshcore_ai_bot.cli import simulate
from meshcore_ai_bot.config import Channel, Config, load_config, validate_url
from meshcore_ai_bot.engine import Engine, Pacer
from meshcore_ai_bot.protocol import Incoming, allows_send, question_for, split_text
from meshcore_ai_bot.store import Store

ROOT = Path(__file__).parents[1]


@pytest.fixture
def config():
    return load_config(ROOT / "config.example.toml")


@pytest.mark.parametrize(
    "channel,text,expected",
    [
        ("Public", "KI ping", None),
        ("#public", "KI ping", None),
        ("#bot", "ping", None),
        ("#bot", "Kiste", None),
        ("#bot", "Skifahren", None),
        ("#bot", "ki ping", "ping"),
        ("#bot", "Was ist KI?", "Was ist KI?"),
        ("#fragdenbot", "wx friedenau", "wx friedenau"),
        ("unknown", "KI ping", None),
        ("#fragdenbot", "", None),
    ],
)
def test_channel_policy(config, channel, text, expected):
    assert question_for(Incoming(channel, "Test", text, 1000), config, 1000) == expected


def test_sender_is_not_keyword(config):
    assert question_for(Incoming("#bot", "KI", "ping", 1000), config, 1000) is None


@pytest.mark.parametrize("stamp", [1, 699, 1121, 1000.0])
def test_old_future_and_invalid_timestamp(config, stamp):
    assert question_for(Incoming("#fragdenbot", "Test", "ping", stamp), config, 1000) is None


def test_self_is_ignored(config):
    assert question_for(Incoming("#fragdenbot", config.station.name, "ping", 1000), config, 1000) is None


@pytest.mark.parametrize("name", ["Public", "#public"])
def test_public_cannot_be_enabled(name):
    c = Config(channels=[Channel(name, "all")])
    with pytest.raises(ValueError):
        c.validate()
    assert not allows_send(c, name, "reply")


@pytest.mark.parametrize("text", ["abc " * 100, "ä🙂雷 " * 150, "x" * 12000, " Ende   "])
@pytest.mark.parametrize("limit", [50, 110])
def test_utf8_complete(text, limit):
    parts = split_text(text, limit, "BER/BB")
    assert all(len(p.encode()) <= limit for p in parts)
    rebuilt = "".join(re.sub(r"^BER/BB (?:\[\d+/\d+\] )?", "", p) for p in parts)
    assert rebuilt == text


@pytest.mark.parametrize(
    "url", ["http://example.com/v1", "https://secret:password@example.com/v1", "file:///tmp/x"]
)
def test_unsafe_urls(url):
    with pytest.raises(ValueError):
        validate_url(url)


def test_local_model_url():
    validate_url("http://127.0.0.1:11434/v1", True)
    with pytest.raises(ValueError):
        validate_url("http://127.0.0.1:11434/v1")


def test_store_dedup_recovery_and_history():
    s = Store(":memory:")
    try:
        msg = Incoming("#fragdenbot", "Alice", "ping", 1000)
        assert s.remember(msg, "ping", 1000)
        assert not s.remember(msg, "ping", 1000)
        assert s.next_message(1001, 300)
        s.recover()
        assert s.next_message(1002, 300)
        s.answer(msg.identity, msg.channel, "Pong", ["part1", "part2"], 1002, 2000)
        assert len(s.history(msg.channel, "Alice", 0, 4)) == 2
        assert not s.history(msg.channel, "Bob", 0, 4)
        assert not s.history("#bot", "Alice", 0, 4)
        p = s.claim(1003, lambda *_: True)
        assert p["text"] == "part1"
        s.recover(now=1004)
        assert s.last_tx() == 1004
        assert s.counts()["uncertain"] == 1
        p2 = s.claim(1004, lambda *_: True)
        assert p2["text"] == "part2"
        s.finish(p2["id"], "accepted", 1005)
        assert s.last_tx() == 1005
        assert s.claim(1006, lambda *_: True) is None
    finally:
        s.close()


def test_persistent_limits(tmp_path):
    path = tmp_path / "state.db"
    s = Store(path)
    assert s.reserve_model_request(1000, 1, 2)
    assert not s.reserve_model_request(1001, 1, 2)
    s.close()
    s = Store(path)
    assert not s.reserve_model_request(1002, 1, 2)
    assert s.reserve_model_request(1061, 1, 2)
    assert not s.reserve_model_request(1122, 1, 2)
    s.close()


def test_broadcast_baseline_updates_and_expiry():
    s = Store(":memory:")
    try:
        assert not s.broadcast("event", "r1", 2000, {"#warnings": ["old"]}, "warnings", 1000, baseline=True)
        assert not s.broadcast("event", "r1", 2000, {"#warnings": ["old"]}, "warnings", 1001)
        assert s.broadcast("event", "r2", 2000, {"#warnings": ["new"]}, "warnings", 1002)
        p = s.claim(1003, lambda *_: True)
        assert p["text"] == "new"
        s.broadcast("event", "withdrawn", 1004, {}, "warnings", 1004)
        assert not s.still_sendable(p["id"], 1005)
    finally:
        s.close()


def test_repeater_aliases_and_no_false_live_data():
    s = Store(":memory:")
    try:
        key = "a" * 64
        s.update_repeaters([{"public_key": key, "type": 2, "adv_name": "Hügel", "last_advert": 900}], 1000)
        s.update_repeaters([{"public_key": key, "type": 2, "adv_name": "Hill", "last_advert": 0}], 1100)
        row = s.repeaters()[0]
        assert row["advert"] == 900
        assert "Hügel" in row["aliases"]
        assert row["checked"] == 1100
        s.heard_advert(key, 1200)
        assert s.repeaters()[0]["advert"] == 1200
    finally:
        s.close()


async def test_pacing_includes_device_response_and_restart(config):
    now = [1000.0]

    async def sleep(seconds):
        now[0] += seconds

    async def send():
        now[0] += 2
        return True

    pacer = Pacer(
        config, last_tx=990, monotonic=lambda: now[0], wall=lambda: now[0], sleep=sleep, jitter=lambda: 0
    )
    assert await pacer.transmit(send, lambda: True)
    assert now[0] == 1022
    assert await pacer.transmit(send, lambda: True)
    assert now[0] == 1054
    assert await pacer.transmit(send, lambda: False) is None
    assert now[0] == 1084


async def test_engine_uncertain_not_retried(config):
    class Assistant:
        async def answer(self, *_):
            return SimpleNamespace(text="Pong!")

    class Transport:
        async def send(self, *_):
            raise TimeoutError("ambiguous device result")

    class Immediate:
        def received(self):
            pass

        async def transmit(self, send, valid):
            return await send() if valid() else None

    s = Store(":memory:")
    try:
        engine = Engine(config, s, Assistant(), pacer=Immediate())
        msg = Incoming("#fragdenbot", "Tester", "ping", int(time.time()))
        assert engine.receive(msg) == "queued"
        assert engine.receive(msg) == "duplicate"
        await engine.process_one()
        assert await engine.deliver_one(Transport())
        assert s.counts() == {"uncertain": 1}
        assert not await engine.deliver_one(Transport())
    finally:
        s.close()


def test_dwd_region_merge_and_times():
    common = {
        "headline": "Frost",
        "level": 1,
        "start": 900000,
        "end": 2000000,
        "description": "Frost möglich",
        "instruction": "Vorsicht",
    }
    data = {
        "time": 1000000,
        "warnings": {
            "a": [dict(common, state="Berlin", regionName="Berlin")],
            "b": [dict(common, state="Brandenburg", regionName="Potsdam")],
        },
    }
    rows = dwd_warnings(data, 1000, ["BER", "BB"])
    assert len(rows) == 1
    assert rows[0]["codes"] == ["BER", "BB"]
    text = warning_text(rows[0])
    assert "Gültig von" in text and " bis " in text
    assert "Vorsicht" not in text and "Handlungsempfehlung" not in text
    assert "Potsdam" in text and "Berlin" in text
    assert "Vorsicht" in warning_text(rows[0], instructions=True)


async def test_offline_replay(config):
    import json

    cases = json.loads((ROOT / "examples/radio-cases.json").read_text())
    results = await simulate(config, cases, offline=True)
    assert all(r["passed"] for r in results)
