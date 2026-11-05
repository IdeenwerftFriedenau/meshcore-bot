import importlib.util
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from meshcore_ai_bot.assistant import Assistant
from meshcore_ai_bot.config import Config
from meshcore_ai_bot.store import Store


def bridge_module():
    path = Path(__file__).parents[1] / "deploy/pi_gateway_bridge.py"
    spec = importlib.util.spec_from_file_location("pi_gateway_bridge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_existing_gateway_delivers_local_reply_without_cloud_or_second_usb(tmp_path):
    module = bridge_module()
    config = Path(__file__).parents[1].joinpath("config.example.toml").read_text()
    config = config.replace('path = "data/bot.sqlite3"', f'path = "{tmp_path}/bot.sqlite3"')
    path = tmp_path / "config.toml"
    path.write_text(config)
    sent = []

    async def send(index, text):
        sent.append((index, text))
        return SimpleNamespace(type=SimpleNamespace(name="OK"), payload={})

    mc = SimpleNamespace(self_info={"name": "Ideenwerft"}, commands=SimpleNamespace(send_chan_msg=send))
    bridge = module.GatewayBridge(path, mc, {4: "#fragdenbot"})
    bridge.engine.pacer.jitter = lambda: 0
    bridge.engine.pacer.config.radio.receive_quiet_seconds = 0
    try:
        assert bridge.receive("Public", "Lena: @Ideenwerft ping", int(time.time())) == "ignored"
        assert bridge.receive("#fragdenbot", "Lena: ping", int(time.time())) == "queued"
        assert await bridge.engine.process_one()
        assert await bridge.engine.deliver_one(bridge)
        assert len(sent) == 1
        assert sent[0][0] == 4 and "Pong" in sent[0][1]
        assert bridge.store.history("#fragdenbot", "Lena", 0, 12)[-1]["role"] == "assistant"
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_gateway_uncertain_ack_requires_reconnect(tmp_path):
    module = bridge_module()
    config = Path(__file__).parents[1].joinpath("config.example.toml").read_text()
    path = tmp_path / "config.toml"
    path.write_text(config.replace('path = "data/bot.sqlite3"', f'path = "{tmp_path}/bot.sqlite3"'))
    calls = []

    async def send(index, text):
        calls.append(text)
        return SimpleNamespace(type=SimpleNamespace(name="ERROR"), payload={"reason": "timeout"})

    mc = SimpleNamespace(self_info={}, commands=SimpleNamespace(send_chan_msg=send))
    bridge = module.GatewayBridge(path, mc, {1: "#fragdenbot"})
    try:
        with pytest.raises(ConnectionError):
            await bridge.send("#fragdenbot", "first")
        with pytest.raises(ConnectionError):
            await bridge.send("#fragdenbot", "suffix")
        assert calls == ["first"]
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_custom_completion_endpoint_keeps_auth_and_local_request_limit():
    config = Config()
    config.ai.base_url = "https://example.org/wp-json/ideenwerft/v1/gateway"
    config.ai.completion_path = ""
    config.ai._api_key = "test-only-token"
    config.ai.requests_per_minute = 1
    store = Store(":memory:")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            assistant = Assistant(config, store, client)
            await assistant._completion([{"role": "user", "content": "test"}], tools=False)
            limited = await assistant._completion([{"role": "user", "content": "test"}], tools=False)
        assert len(requests) == 1
        assert str(requests[0].url) == config.ai.base_url
        assert requests[0].headers["Authorization"] == "Bearer test-only-token"
        assert requests[0].headers["X-IW-Task"] == "meshcore-radio"
        assert "viele Fragen" in limited["choices"][0]["message"]["content"]
    finally:
        store.close()


@pytest.mark.parametrize("path", ["https://other.example/", "//other.example/", "/x?secret=yes", "/x\\y"])
def test_completion_path_cannot_replace_origin(path):
    config = Config()
    config.ai.completion_path = path
    with pytest.raises(ValueError, match="completion_path"):
        config.validate()
