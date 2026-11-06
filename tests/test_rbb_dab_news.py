import importlib.util
from datetime import datetime
from pathlib import Path

import pytest

from meshcore_ai_bot.config import Channel, Config
from meshcore_ai_bot.dab_control import (
    effective_channel,
    load_settings,
    rbb_news_window,
    save_settings,
    scan_allowed,
)
from meshcore_ai_bot.store import Store

spec = importlib.util.spec_from_file_location(
    "rbb_news", Path(__file__).parents[1] / "deploy/dab/rbb_news.py"
)
news = importlib.util.module_from_spec(spec)
spec.loader.exec_module(news)


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


@pytest.mark.parametrize("date,reserved,channel,scan", [
    ("2026-11-06T10:29:30+01:00", False, "5D", True),
    ("2026-11-06T10:59:29+01:00", False, "5D", False),
    ("2026-11-06T10:59:30+01:00", True, "7D", False),
    ("2026-11-06T11:00:00+01:00", True, "7D", False),
    ("2026-11-06T11:05:49+01:00", True, "7D", False),
    ("2026-11-06T11:05:50+01:00", False, "5D", True),
    ("2026-11-06T11:59:30+01:00", False, "5C", False),
    ("2026-11-06T12:00:00+01:00", False, "5C", False),
    ("2026-10-25T01:00:00+02:00", True, "7D", False),
    ("2026-10-25T02:00:00+02:00", False, "5C", False),
    ("2026-10-25T02:00:00+01:00", False, "5C", False),
    ("2026-03-29T03:00:00+02:00", True, "7D", False),
])
def test_tuner_reservations_preserve_listener_choice(date, reserved, channel, scan):
    now = stamp(date)
    settings = {"channel": "5D", "rbb_news_enabled": True}
    assert rbb_news_window(now) is reserved
    assert effective_channel(settings, now) == channel
    assert scan_allowed(now, settings) is scan
    assert settings["channel"] == "5D"


def test_rbb_is_explicit_opt_in_and_setting_is_preserved(tmp_path):
    path = tmp_path / "settings.json"
    now = stamp("2026-11-06T11:00:00+01:00")
    assert effective_channel(load_settings(path), now) == "5C"
    save_settings({"channel": "5D", "rbb_news_enabled": True}, path)
    save_settings({"forward_asa": True}, path)
    assert effective_channel(load_settings(path), now) == "7D"
    with pytest.raises(ValueError):
        save_settings({"rbb_news_enabled": "true"}, path)


@pytest.mark.parametrize("date,ok", [
    ("2026-11-06T13:00:00+01:00", True),
    ("2026-11-06T13:00:15+01:00", True),
    ("2026-11-06T13:00:16+01:00", False),
    ("2026-11-06T14:00:00+01:00", False),
    ("2026-11-06T12:59:59+01:00", False),
])
def test_no_late_or_wrong_hour_capture(date, ok):
    now = stamp(date)
    if ok:
        assert news.bulletin(now) == int(now // 3600) * 3600
    else:
        with pytest.raises(ValueError):
            news.bulletin(now)


def test_receiver_must_have_fresh_rbb_on_correct_ensemble():
    now = stamp("2026-11-06T11:00:00+01:00")
    mux = {"demodulator": {"time_last_fct0_frame": now * 1000},
           "services": [{"sid": "0xd321", "components": [{"transportmode": "audio"}]}]}
    assert news.ready(mux, "7D", now)
    assert not news.ready(mux, "5C", now)
    assert not news.ready(mux, "7D", now + 35)
    mux["services"][0]["sid"] = "0xd210"
    assert not news.ready(mux, "7D", now)


def test_only_source_supported_short_news_packets():
    transcript = "in berlin wird heute über den haushalt beraten weitere nachrichten folgen"
    item = {"headline": "Berlin berät über den Haushalt.", "evidence": "in berlin wird heute über den haushalt beraten"}
    slot = stamp("2026-11-06T13:00:00+01:00")
    assert news.messages({"is_news": True, "items": [item]}, transcript, slot) == [
        "rbb88.8 13:00: Berlin berät über den Haushalt."
    ]
    assert news.messages({"is_news": False, "items": [item]}, transcript, slot) == []
    assert news.messages({"is_news": True, "items": [
        {**item, "evidence": "erfundene information steht hier nicht"},
        {**item, "headline": "Überprüfung " * 30},
        "wrong shape", {**item, "evidence": "berlin"},
    ]}, transcript, slot) == []


def test_ten_supported_headlines_from_the_whole_bulletin():
    evidence = "eine klar erkannte nachricht aus der sendung"
    items = [{"headline": f"Nachricht {i} aus Deutschland", "evidence": evidence} for i in range(12)]
    slot = stamp("2026-11-06T13:00:00+01:00")
    texts = news.messages({"is_news": True, "items": items}, evidence, slot)
    assert len(texts) == 10
    assert all(len(t.encode()) <= 110 for t in texts)


def test_followup_shares_ten_packet_budget_without_cancelling_pending():
    store = Store(":memory:")
    c = Config(channels=[Channel("#news", "off", broadcasts=["news"])])
    c.broadcasts.news_enabled = True
    slot = stamp("2026-11-06T13:00:00+01:00")
    try:
        assert news.queue_messages(store, c, ["First report"], slot, slot + 300)
        assert news.queue_messages(store, c, ["First report"] + [f"Report {i}" for i in range(15)], slot, slot + 400)
        assert len(news.prior_messages(store, slot)) == 10
        assert store.db.execute("SELECT count(*) FROM packets WHERE state='queued'").fetchone()[0] == 10
        assert not news.queue_messages(store, c, ["Extra"], slot, slot + 500)
        assert not news.queue_messages(store, c, ["Stale"], slot, slot + 1800)
    finally:
        store.close()


@pytest.mark.parametrize("now,expected", [
    ("2026-11-06T12:30:00+01:00", "2026-11-06T14:00:00+01:00"),
    ("2026-11-06T14:05:00+01:00", "2026-11-06T16:00:00+01:00"),
    ("2026-11-06T23:30:00+01:00", "2026-11-07T00:00:00+01:00"),
    ("2026-10-25T01:30:00+02:00", "2026-10-25T02:00:00+02:00"),
    ("2026-03-29T01:30:00+01:00", "2026-03-29T04:00:00+02:00"),
])
def test_dlf_uses_even_hours_when_alternating(monkeypatch, now, expected):
    spec = importlib.util.spec_from_file_location(
        "dab_news", Path(__file__).parents[1] / "deploy/dab/dab_news.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("DAB_NEWS_EVEN_HOURS_ONLY", "1")
    assert module.next_hour(stamp(now)) == stamp(expected)
    monkeypatch.delenv("DAB_NEWS_EVEN_HOURS_ONLY")
    now = stamp("2026-11-06T12:30:00+01:00")
    assert module.next_hour(now) == stamp("2026-11-06T13:00:00+01:00")
