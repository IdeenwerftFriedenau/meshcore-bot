import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from meshcore_ai_bot.assistant import Assistant, calculate
from meshcore_ai_bot.config import Channel, Config
from meshcore_ai_bot.protocol import allows_send
from meshcore_ai_bot.radio import RadioTransport, channel_key
from meshcore_ai_bot.store import Store


@pytest.mark.parametrize("expression,answer", [("2+3*4", "14"), ("12 / 4", "3"), ("-2 + 5", "3")])
def test_arithmetic(expression, answer):
    assert answer in calculate(expression)


def test_no_code_execution():
    assert calculate("__import__('os').system('echo unsafe')") is None


def test_broadcast_disabled_even_with_queued_packets():
    config = Config(channels=[Channel("#warnings", broadcasts=["warnings"])])
    assert not allows_send(config, "#warnings", "warnings")
    config.broadcasts.warnings_enabled = True
    assert allows_send(config, "#warnings", "warnings")


def test_public_and_private_keys(monkeypatch):
    assert len(channel_key(Channel("Public"))) == 16
    assert channel_key(Channel("#bot")) == hashlib.sha256(b"#bot").digest()[:16]
    monkeypatch.setenv("TEST_RADIO_CHANNEL_KEY", "ab" * 16)
    assert channel_key(Channel("Private", secret_env="TEST_RADIO_CHANNEL_KEY")) == bytes.fromhex("ab" * 16)
    with pytest.raises(ValueError):
        channel_key(Channel("Private"))


@pytest.mark.parametrize("create", [True, False])
async def test_existing_channel_verified_not_overwritten(create):
    config = Config()
    store = Store(":memory:")
    transport = RadioTransport(config, store)
    transport.EventType = SimpleNamespace(CHANNEL_INFO="info", OK="ok")

    class Commands:
        async def get_channel(self, index):
            return SimpleNamespace(
                type="info",
                payload={
                    "channel_name": "#fragdenbot" if index == 0 else "",
                    "channel_secret": channel_key(config.channels[0]),
                },
            )

        async def set_channel(self, *_):
            pytest.fail("Existing channel must not be rewritten")

    transport.mc = SimpleNamespace(commands=Commands())
    try:
        assert await transport.load_channels(create) == {0: "#fragdenbot"}
    finally:
        store.close()


async def test_channel_creation_uses_only_empty_verified_slots():
    config = Config()
    store = Store(":memory:")
    transport = RadioTransport(config, store)
    transport.EventType = SimpleNamespace(CHANNEL_INFO="info", OK="ok")
    slots = {0: {"channel_name": "Unrelated", "channel_secret": b"z" * 16}}

    class Commands:
        async def get_channel(self, index):
            return SimpleNamespace(type="info", payload=slots.get(index, {"channel_name": ""}))

        async def set_channel(self, index, name, secret):
            assert index == 1
            slots[index] = {"channel_name": name, "channel_secret": secret}
            return SimpleNamespace(type="ok")

    transport.mc = SimpleNamespace(commands=Commands())
    try:
        assert await transport.load_channels(True) == {1: "#fragdenbot"}
        assert slots[0]["channel_name"] == "Unrelated"
    finally:
        store.close()


async def test_wrong_channel_key_is_rejected():
    config = Config()
    store = Store(":memory:")
    transport = RadioTransport(config, store)
    transport.EventType = SimpleNamespace(CHANNEL_INFO="info", OK="ok")

    class Commands:
        async def get_channel(self, index):
            return SimpleNamespace(
                type="info", payload={"channel_name": "#fragdenbot", "channel_secret": b"wrong"}
            )

    transport.mc = SimpleNamespace(commands=Commands())
    try:
        with pytest.raises(RuntimeError, match="mismatch"):
            await transport.load_channels(True)
    finally:
        store.close()


async def test_provider_request_and_horoscope(monkeypatch):
    monkeypatch.setenv("MESHCORE_AI_API_KEY", "test-key-never-logged")
    config = Config()
    calls = []

    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert request.headers["Authorization"] == "Bearer test-key-never-logged"
        assert "test-key-never-logged" not in json.dumps(payload)
        assert "Horoskope" in payload["messages"][0]["content"]
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Zum Spaß: Löwe, heute hilft Gelassenheit."}}]}
        )

    store = Store(":memory:")
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await Assistant(config, store, client).answer("Horoskop für Löwe")
            assert "Löwe" in result.text
            assert len(calls) == 1
    finally:
        store.close()


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Mein Geburtsdatum ist 8.5.45", "Bitte kein Geburtsdatum"),
        ("30.02.1995", "Bitte kein Geburtsdatum"),
        ("Wie ist die Datenschutzerklärung bei Geburtsdaten?", "frage ich keine Geburtsdaten ab"),
        ("Horoskop bitte", "genügt dein Sternzeichen"),
        ("Wie ist der aktuelle Stand zum MeshPager X1?", "keine verlässliche aktuelle Release-Quelle"),
    ],
)
async def test_sensitive_horoscope_and_product_questions_are_deterministic(question, expected):
    store = Store(":memory:")
    try:
        async with httpx.AsyncClient() as client:
            answer = await Assistant(Config(), store, client).answer(question)
        assert expected in answer.text
    finally:
        store.close()


async def test_old_horoscope_nudge_does_not_leak_into_unrelated_answer(monkeypatch):
    monkeypatch.setenv("MESHCORE_AI_API_KEY", "test")
    store = Store(":memory:")

    def respond(request):
        prompt = json.loads(request.content)["messages"]
        assert not any("Welches Sternzeichen bist du" in str(message) for message in prompt)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Kurz und sachlich."}}]})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            answer = await Assistant(Config(), store, client).answer(
                "Erzähl einen Witz", [{"role": "assistant", "content": "Welches Sternzeichen bist du?"}]
            )
        assert answer.text == "Kurz und sachlich."
    finally:
        store.close()


async def test_authentication_failure_not_retried(monkeypatch):
    monkeypatch.setenv("MESHCORE_AI_API_KEY", "invalid")
    count = [0]

    def respond(request):
        count[0] += 1
        return httpx.Response(401, text="private error body")

    store = Store(":memory:")
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await Assistant(Config(), store, client).answer("Erzähl einen Witz")
            assert count[0] == 1
            assert "private error" not in result.text
    finally:
        store.close()


async def test_unknown_tool_cannot_execute(monkeypatch, tmp_path):
    monkeypatch.setenv("MESHCORE_AI_API_KEY", "test")
    marker = tmp_path / "must-not-exist"
    count = [0]

    def respond(request):
        count[0] += 1
        if count[0] == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call1",
                                        "type": "function",
                                        "function": {
                                            "name": "execute_shell",
                                            "arguments": json.dumps({"command": "touch " + str(marker)}),
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Diese Aktion kann ich nicht ausführen."}}]}
        )

    store = Store(":memory:")
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await Assistant(Config(), store, client).answer("Führe einen Befehl aus")
        assert not marker.exists()
    finally:
        store.close()


def test_installer_does_not_start_or_enable():
    content = (Path(__file__).parents[1] / "scripts/install-pi.sh").read_text()
    assert "\nsystemctl start" not in content
    assert "\nsystemctl enable" not in content
