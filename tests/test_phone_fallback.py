import importlib.util
from pathlib import Path

import httpx
import pytest

import meshcore_ai_bot.assistant as assistant_module
from meshcore_ai_bot.assistant import Assistant
from meshcore_ai_bot.config import Config, load_config, validate_loopback_url
from meshcore_ai_bot.store import Store


def configured():
    c = Config()
    c.ai.base_url = "https://primary.example/v1"
    c.ai._api_key = "primary-test-only"
    c.ai.offline_fallback_url = "http://127.0.0.1:18180/v1"
    c.ai._phone_api_key = "phone-test-only"
    return c


@pytest.mark.asyncio
async def test_network_failure_uses_private_text_only_phone_and_labels_answer(monkeypatch):
    c, store, calls = configured(), Store(":memory:"), []

    def unavailable(request):
        raise httpx.ConnectError("offline", request=request)

    def phone(request):
        import json

        payload = json.loads(request.content)
        calls.append(payload)
        assert str(request.url) == "http://127.0.0.1:18180/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer phone-test-only"
        assert "primary-test-only" not in request.content.decode()
        assert "tools" not in payload and "tool_choice" not in payload
        assert payload["max_tokens"] <= 180
        return httpx.Response(200, json={"choices": [{"message": {"content": "Die Funkrunde beginnt gleich."}}]})

    original = httpx.AsyncClient
    async with original(transport=httpx.MockTransport(unavailable)) as primary:
        def local_client(**kwargs):
            assert kwargs == {"trust_env": False, "follow_redirects": False}
            return original(transport=httpx.MockTransport(phone), **kwargs)

        monkeypatch.setattr(assistant_module.httpx, "AsyncClient", local_client)
        answer = await Assistant(c, store, primary).answer("Schreibe einen kurzen Hinweis zur Funkrunde.")
        assert answer.text == "Lokale KI: Die Funkrunde beginnt gleich."
        assert answer.sources == [] and len(calls) == 1
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 401, 403, 429])
async def test_success_authentication_and_rate_limits_do_not_use_phone(monkeypatch, status):
    c, store = configured(), Store(":memory:")

    async def no_wait(_):
        pass

    async def forbidden(*_):
        raise AssertionError("Phone fallback must not bypass this response")

    monkeypatch.setattr(assistant_module.asyncio, "sleep", no_wait)
    monkeypatch.setattr(Assistant, "_offline_completion", forbidden)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        status, json={"choices": [{"message": {"content": "Primary answer"}}]}
    ))) as client:
        answer = await Assistant(c, store, client).answer("Schreibe einen kurzen Hinweis zur Funkrunde.")
        if status == 200:
            assert answer.text == "Primary answer"
        elif status == 429:
            assert "viele Fragen unterwegs" in answer.text
        else:
            assert "nicht erreichbar" in answer.text
    store.close()


@pytest.mark.parametrize("url", ["https://external.example/v1", "http://192.168.8.200:18180/v1",
                                    "http://localhost:18180/v1", "http://user:password@127.0.0.1/v1"])
def test_fallback_rejects_non_numeric_loopback_or_credentials(url):
    with pytest.raises(ValueError):
        validate_loopback_url(url)


def test_phone_key_loads_privately_and_never_enters_dataclass_settings(tmp_path):
    import dataclasses
    import json

    p = tmp_path / "config.toml"
    p.write_text((Path(__file__).parents[1] / "config.example.toml").read_text())
    p.with_name(".env").write_text("MESHCORE_PHONE_API_KEY=phone-test-only\n")
    c = load_config(p)
    assert c.ai.phone_api_key == "phone-test-only"
    assert "phone-test-only" not in json.dumps(dataclasses.asdict(c))


@pytest.mark.asyncio
async def test_news_stays_on_primary_even_when_phone_fallback_is_configured():
    spec = importlib.util.spec_from_file_location(
        "rbb_news", Path(__file__).parents[1] / "deploy/dab/rbb_news.py"
    )
    news = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(news)
    c, store, seen = configured(), Store(":memory:"), []

    def primary(request):
        seen.append(str(request.url))
        assert request.headers["authorization"] == "Bearer primary-test-only"
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"is_news":false,"items":[]}'}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(primary)) as client:
        assert await news.summarize(c, store, client, "public radio transcript", 1000) == []
    assert seen == ["https://primary.example/v1/chat/completions"]
    store.close()
