"""A realistic text preview must never touch a radio or the durable message queue."""

import asyncio
import copy
import json
import shutil
import socket
import threading
from pathlib import Path

import httpx
import pytest

from meshcore_ai_bot.assistant import Answer, Assistant
from meshcore_ai_bot.config import load_config
from meshcore_ai_bot.dashboard import make_server
from meshcore_ai_bot.engine import Engine
from meshcore_ai_bot.preview import load_preview_config, preview, validate_request
from meshcore_ai_bot.store import Store


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.toml"
    shutil.copyfile(Path(__file__).parents[1] / "config.example.toml", path)
    return load_config(path)


@pytest.mark.parametrize(
    ("channel", "text", "expected"),
    [
        ("#fragdenbot", "Was bedeutet QTH?", True),
        ("#bot", "Was bedeutet QTH?", False),
        ("#bot", "KI, was bedeutet QTH?", True),
        ("Public", "KI, was bedeutet QTH?", False),
        ("#public", "ping", False),
        ("#ideenwerft", "ping", True),
    ],
)
async def test_real_channel_rules_without_network_or_rf(config, channel, text, expected, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Preview attempted network or RF")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(Engine, "deliver_one", forbidden)
    before = copy.deepcopy(config)
    result = await preview(config, {"text": text, "channel": channel})
    assert bool(result["packets"]) == expected
    assert bool(result["answer"]) == expected
    assert result["decision"] == ("queued" if expected else "ignored")
    assert result["radio_sent"] is False and result["offline"] is True
    assert all(size <= config.radio.packet_bytes for size in result["packet_bytes"])
    assert config == before
    assert not await asyncio.to_thread(Path(config.storage.path).exists)


async def test_connected_preview_uses_real_engine_and_splits_packets(config, monkeypatch):
    async def answer(self, question, history=None):
        assert not self.offline and not history
        return Answer("Eine längere Antwort. " * 15)

    monkeypatch.setattr(Assistant, "answer", answer)
    result = await preview(config, {"text": "Freie Frage", "channel": "#fragdenbot", "offline": False})
    assert len(result["packets"]) > 1
    assert result["answer"].startswith("Eine längere Antwort.")
    assert result["offline"] is False
    assert all(size <= 110 for size in result["packet_bytes"])


async def test_own_station_does_not_reply(config):
    result = await preview(config, {"text": "ping", "channel": "#fragdenbot", "sender": config.station.name})
    assert result["decision"] == "ignored"


async def test_contacts_read_only_no_conversation_history(config):
    store = Store(config.storage.path)
    try:
        store.update_repeaters([{"public_key": "ab" * 32, "adv_name": "Test-Repeater", "type": 2}], 12345)
        before = list(store.db.execute("SELECT * FROM repeaters").fetchall())
        result = await preview(config, {"text": "Was bedeutet QTH?", "channel": "#fragdenbot"})
        assert result["contacts_loaded"]
        assert list(store.db.execute("SELECT * FROM repeaters").fetchall()) == before
        assert store.db.execute("SELECT count(*) FROM packets").fetchone()[0] == 0
        assert store.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    finally:
        store.close()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"text": "", "channel": "#fragdenbot"},
        {"text": "a" * 601, "channel": "#fragdenbot"},
        {"text": "ä" * 301, "channel": "#fragdenbot"},
        {"text": "ping", "channel": "missing"},
        {"text": "ping", "channel": "#fragdenbot", "offline": "false"},
        {"text": "ping", "channel": "#fragdenbot", "sender": "a" * 33},
        {"text": "ping", "channel": "#fragdenbot", "settings": {}},
    ],
)
def test_reject_invalid_requests(config, payload):
    with pytest.raises(ValueError):
        validate_request(payload, config)


def test_preview_endpoint_protection_quota_and_no_persistence(config, monkeypatch):
    path = Path(config.storage.path).parents[1] / "config.toml"
    server = make_server(path, port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = "http://127.0.0.1:" + str(server.server_port)
    before = path.read_bytes()
    try:
        with httpx.Client(base_url=url, trust_env=False) as client:
            state = client.get("/api/state").json()
            headers = {"Origin": url, "X-CSRF-Token": state["csrf"]}
            payload = {"text": "Was bedeutet QTH?", "channel": "#fragdenbot"}
            assert client.post("/api/preview", json=payload).status_code == 403
            result = client.post("/api/preview", json=payload, headers=headers)
            assert result.status_code == 200
            assert result.json()["radio_sent"] is False
            assert result.json()["answer"]
            assert not Path(config.storage.path).exists() and before == path.read_bytes()
            assert (
                client.post("/api/preview", json={**payload, "settings": {}}, headers=headers).status_code
                == 400
            )
            for _ in range(config.ai.requests_per_minute):
                assert (
                    client.post(
                        "/api/preview", json={**payload, "offline": False}, headers=headers
                    ).status_code
                    == 200
                )
            assert (
                client.post("/api/preview", json={**payload, "offline": False}, headers=headers).status_code
                == 429
            )
            assert client.post("/api/preview", json=payload, headers=headers).status_code == 200

            def timeout(*args):
                raise TimeoutError()

            monkeypatch.setattr("meshcore_ai_bot.dashboard.run_preview", timeout)
            assert client.post("/api/preview", json=payload, headers=headers).status_code == 504
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
    assert "API_KEY" not in json.dumps(result.json())


def test_preview_provider_credentials_are_frozen_without_global_changes(config, monkeypatch):
    path = Path(config.storage.path).parents[1] / "config.toml"
    env = path.parent / ".env"
    monkeypatch.setenv("MESHCORE_AI_API_KEY", "process-key")
    env.write_text("MESHCORE_AI_API_KEY=saved-key\n")
    snapshot = load_preview_config(path)
    env.write_text("MESHCORE_AI_API_KEY=new-provider-key\n")
    monkeypatch.setenv("MESHCORE_AI_API_KEY", "other-process-key")
    assert snapshot.ai.api_key == "saved-key"
    assert load_preview_config(path).ai.api_key == "new-provider-key"
    import os

    assert os.environ["MESHCORE_AI_API_KEY"] == "other-process-key"
