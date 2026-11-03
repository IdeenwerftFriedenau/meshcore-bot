"""Federal-state filtering and offline ASA contract; no real RF traffic."""

import copy
import dataclasses
import json
import socket
import time
from datetime import UTC, datetime

import httpx
import pytest

from meshcore_ai_bot import asa
from meshcore_ai_bot.broadcasts import Broadcasts, dwd_warnings, selected_regions
from meshcore_ai_bot.config import Channel, Config
from meshcore_ai_bot.radio import cancel_changed_sources
from meshcore_ai_bot.regions import STATES, bbk_states
from meshcore_ai_bot.store import Store


@pytest.mark.parametrize("code", list(STATES))
def test_dwd_all_states(code):
    now = time.time()
    data = {
        "time": now * 1000,
        "warnings": {
            "a": [
                {
                    "stateShort": code,
                    "state": STATES[code][0],
                    "regionName": "Testgebiet",
                    "headline": "Frost",
                    "level": 2,
                    "start": now * 1000,
                    "end": (now + 300) * 1000,
                }
            ]
        },
    }
    rows = dwd_warnings(data, now, [code])
    assert len(rows) == 1 and rows[0]["codes"] == [code]
    assert not dwd_warnings(data, now, [c for c in STATES if c != code])


@pytest.mark.parametrize(
    ("alias", "code"), [("BL", "BER"), ("BE", "BER"), ("NS", "NI"), ("NRW", "NW"), ("SA", "ST")]
)
def test_dwd_source_aliases(alias, code):
    data = {"time": 1000000, "warnings": {"a": [{"stateShort": alias, "start": 1000000, "end": 2000000}]}}
    assert dwd_warnings(data, 1000, [code])[0]["codes"] == [code]


def test_bbk_target_codes_only():
    assert bbk_states(
        {"parameter": [{"valueName": "warnVerwaltungsbereiche", "value": '["110000000000","120510000000"]'}]}
    ) == {"BER", "BB"}
    assert bbk_states({"area": [{"areaDesc": "Berlin"}], "sender": "DE-BB"}) == set()
    assert bbk_states(
        {"parameter": [{"valueName": "warnVerwaltungsbereiche", "value": "000000000000"}]}
    ) == set(STATES)


async def test_bbk_national_indexes_filter_and_deduplicate():
    count = 0
    now = datetime.now(UTC).isoformat()
    expiry = datetime.fromtimestamp(time.time() + 300, UTC).isoformat()

    def handle(request):
        nonlocal count
        if request.url.path.endswith("mapData.json"):
            return httpx.Response(200, json=[{"id": "one-warning", "version": 1}])
        count += 1
        return httpx.Response(
            200,
            json={
                "status": "Actual",
                "msgType": "Alert",
                "info": [
                    {
                        "language": "de-DE",
                        "headline": "Quelle",
                        "severity": "Moderate",
                        "onset": now,
                        "expires": expiry,
                        "description": "Volltext",
                        "area": [{"areaDesc": "Berlin und Brandenburg"}],
                        "parameter": [
                            {"valueName": "warnVerwaltungsbereiche", "value": "110000000000,120510000000"}
                        ],
                    }
                ],
            },
        )

    config = Config()
    store = Store(":memory:")
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            sources = Broadcasts(config, store, client)
            rows = await sources.fetch_bbk()
            assert len(rows) == 1 and rows[0]["codes"] == ["BER", "BB"]
            assert await sources.fetch_bbk() == rows
            assert count == 1
            config.broadcasts.bbk_regions = ["BY"]
            assert await sources.fetch_bbk() == []
    finally:
        store.close()


def event():
    return {
        "version": 1,
        "protocol": "DAB+ ASA",
        "status": "Actual",
        "kind": "alert",
        "id": "real-source-id",
        "received_at": datetime.now(UTC).isoformat(),
        "onset": datetime.fromtimestamp(time.time() - 10, UTC).isoformat(),
        "expires": datetime.fromtimestamp(time.time() + 600, UTC).isoformat(),
        "regions": ["BER", "BB"],
        "title": "Frost",
        "description": "Vollständige Warnbeschreibung.",
        "area": "Berlin und Brandenburg",
        "severity": "Moderate",
        "instruction": "Empfehlung nicht versenden",
    }


@pytest.fixture
def offline_config(tmp_path):
    config = Config()
    config.asa.inbox = str(tmp_path / "inbox")
    config.asa.enabled = True
    config.broadcasts.warnings_enabled = True
    config.channels = [
        Channel("#warnings", broadcasts=["warnings"]),
        Channel("Public"),
        Channel("#bot", "keyword"),
    ]
    return config


def test_asa_entirely_offline_fresh_first_event_dedup_and_update(offline_config, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Network forbidden in offline warning path")

    monkeypatch.setattr(socket, "create_connection", no_network)
    store = Store(":memory:")
    try:
        row = event()
        raw = json.dumps(row).encode()
        asa.ingest(offline_config, raw)
        assert asa.poll(offline_config, store) == 1
        packets = store.db.execute("SELECT channel,text FROM packets WHERE state='queued'").fetchall()
        assert {p[0] for p in packets} == {"#warnings"}
        text = "".join(p[1] for p in packets)
        assert "BER/BB" in text and "Empfehlung nicht" not in text
        assert "Gültig von" in text and "bis" in text
        asa.ingest(offline_config, raw)
        assert asa.poll(offline_config, store) == 0
        row["kind"] = "update"
        row["description"] = "Neue Beschreibung"
        asa.ingest(offline_config, json.dumps(row).encode())
        assert asa.poll(offline_config, store) == 1
        assert store.db.execute("SELECT count(*) FROM packets WHERE state='cancelled'").fetchone()[0] > 0
        row["kind"] = "cancel"
        asa.ingest(offline_config, json.dumps(row).encode())
        asa.poll(offline_config, store)
        assert store.db.execute("SELECT count(*) FROM packets WHERE state='queued'").fetchone()[0] == 0
    finally:
        store.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "Test"},
        {"protocol": "Radiotext"},
        {"received_at": "2000-01-01T00:00:00Z"},
        {"onset": None},
        {"expires": None},
        {"regions": ["unknown"]},
        {"description": ""},
        {"title": 42},
    ],
)
def test_asa_rejects_unusable_events(offline_config, changes):
    row = event()
    row.update(changes)
    with pytest.raises((ValueError, TypeError)):
        asa.ingest(offline_config, json.dumps(row).encode())


def test_asa_disabled_wrong_regions_and_bad_file_isolation(offline_config):
    store = Store(":memory:")
    try:
        asa.ingest(offline_config, json.dumps(event()).encode())
        offline_config.asa.enabled = False
        assert asa.poll(offline_config, store) == 0
        offline_config.asa.enabled = True
        offline_config.asa.regions = ["BY"]
        assert asa.poll(offline_config, store) == 0
        offline_config.asa.regions = ["BER", "BB"]
        from pathlib import Path

        (Path(offline_config.asa.inbox) / "bad.json").write_text("not JSON")
        row = event()
        row["id"] = "second"
        asa.ingest(offline_config, json.dumps(row).encode())
        assert asa.poll(offline_config, store) == 1
        assert (Path(offline_config.asa.inbox) / "rejected" / "bad.json").exists()
    finally:
        store.close()


def test_source_regions_and_cancellation_only_for_changed_source():
    old = Config()
    old.broadcasts.warnings_enabled = True
    old.broadcasts.bbk_regions = ["BY"]
    assert selected_regions(old, "dwd") == ["BER", "BB"]
    assert selected_regions(old, "bbk") == ["BY"]
    store = Store(":memory:")
    try:
        for source in ["dwd", "bbk", "asa"]:
            store.broadcast(
                source + ":event", "v1", time.time() + 300, {"#warnings": ["test"]}, "warnings", time.time()
            )
        new = copy.deepcopy(old)
        new.ai.model = "different"
        cancel_changed_sources(store, old, new)
        assert store.counts()["queued"] == 3
        next_config = copy.deepcopy(new)
        next_config.broadcasts.bbk_regions = ["HE"]
        cancel_changed_sources(store, new, next_config)
        assert store.counts()["queued"] == 2
        assert (
            store.db.execute("SELECT state FROM packets WHERE batch LIKE 'bbk:%'").fetchone()[0]
            == "cancelled"
        )
    finally:
        store.close()


def test_config_all_states_independent_selections():
    config = Config()
    config.broadcasts.warning_regions = list(STATES)
    config.broadcasts.dwd_regions = ["DE-BE", "NRW"]
    config.asa.regions = list(STATES)
    config.validate()
    assert config.broadcasts.dwd_regions == ["BER", "NW"]
    assert len(dataclasses.asdict(config)["asa"]["regions"]) == 16
