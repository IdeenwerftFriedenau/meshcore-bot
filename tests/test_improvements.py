"""'mehr' and 'Quelle?', offline facts for the phone model, departures, caches and research model."""

import json
import time
from types import SimpleNamespace

import httpx
import pytest

import meshcore_ai_bot.assistant as assistant_module
from meshcore_ai_bot.assistant import Assistant, answer_cache_key, kiwix_topic
from meshcore_ai_bot.config import Channel, Config
from meshcore_ai_bot.engine import Engine, short_url
from meshcore_ai_bot.protocol import Incoming, followup_command, radio_check
from meshcore_ai_bot.store import Store
from meshcore_ai_bot.tools import Tools, schemas


def sent_texts(store):
    texts = []
    while packet := store.claim(time.time(), lambda *_: True):
        texts.append(packet["text"])
        store.finish(packet["id"], "accepted", time.time())
    return texts


@pytest.mark.parametrize(
    ("text", "kind"),
    [("mehr", "more"), ("Weiter!", "more"), ("Quelle?", "source"), ("Quellen bitte", "source"),
     ("woher weißt du das?", "source"), ("Mehr Wetter in Berlin", None), ("Was ist eine Quelle?", None)],
)
def test_followup_commands(text, kind):
    assert followup_command(text) == kind


def test_help_mentions_followups():
    assert "mehr" in radio_check("hilfe") and "Quelle" in radio_check("hilfe")


async def test_more_continues_cut_answer_and_source_names_links():
    config = Config(channels=[Channel("#fragdenbot", "all")])
    config.radio.incoming_per_sender_per_minute = 10
    store = Store(":memory:")
    asked = []
    long = "Erster Teil der Recherche. " + "Viele Details zur Geschichte von Friedenau. " * 20 + "ENDE"

    class Answerer:
        async def answer(self, question, history):
            asked.append(question)
            return SimpleNamespace(text=long, sources=["https://www.rbb24.de/friedenau.html"])

    try:
        engine = Engine(config, store, Answerer())
        now = int(time.time())
        engine.receive(Incoming("#fragdenbot", "Alice", "Erzähl was über Friedenau", now))
        await engine.process_one()
        first = "".join(sent_texts(store))
        assert "(weiter mit „mehr“)" in first and "ENDE" not in first

        engine.receive(Incoming("#fragdenbot", "Alice", "mehr", now + 1))
        await engine.process_one()
        second = "".join(sent_texts(store))
        assert "Viele Details" in second and asked == ["Erzähl was über Friedenau"]

        engine.receive(Incoming("#fragdenbot", "Bob", "Quelle?", now + 2))
        await engine.process_one()
        assert "keine Online-Quelle" in "".join(sent_texts(store))

        engine.receive(Incoming("#fragdenbot", "Alice", "Quelle?", now + 3))
        await engine.process_one()
        assert "Quellen: rbb24.de/friedenau.html" in "".join(sent_texts(store))
        assert asked == ["Erzähl was über Friedenau"]
    finally:
        store.close()


async def test_more_without_rest_asks_the_ai_for_details():
    config = Config(channels=[Channel("#fragdenbot", "all")])
    store = Store(":memory:")
    asked = []

    class Answerer:
        async def answer(self, question, history):
            asked.append(question)
            return SimpleNamespace(text="Kurz.", sources=[])

    try:
        engine = Engine(config, store, Answerer())
        now = int(time.time())
        engine.receive(Incoming("#fragdenbot", "Alice", "Was ist LoRa?", now))
        await engine.process_one()
        engine.receive(Incoming("#fragdenbot", "Alice", "mehr", now + 1))
        await engine.process_one()
        assert asked[1] == "Erzähl mir bitte mehr Details zu: Was ist LoRa?"
    finally:
        store.close()


def test_short_url():
    assert short_url("https://www.tagesschau.de/inland/x-100.html") == "tagesschau.de/inland/x-100.html"


async def test_departures_from_transport_rest():
    def respond(request):
        if request.url.path == "/locations":
            assert request.url.params["query"] == "S Friedenau"
            return httpx.Response(200, json=[{"type": "stop", "id": "900061101", "name": "S Friedenau (Berlin)"}])
        assert request.url.path == "/stops/900061101/departures"
        return httpx.Response(200, json={"departures": [
            {"when": "2026-11-07T13:07:00+01:00", "delay": 120, "direction": "Wannsee", "line": {"name": "S1"}},
            {"when": None, "plannedWhen": "2026-11-07T13:10:00+01:00", "direction": "Rathaus Steglitz",
             "line": {"name": "187"}, "cancelled": True},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await Tools(Config(), Store(":memory:"), client).run("get_departures", {"stop": "S Friedenau"})
    assert result["text"] == (
        "Abfahrten S Friedenau (Berlin) (Echtzeit, BVG/VBB): 13:07 S1 → Wannsee (+2); "
        "13:10 187 → Rathaus Steglitz fällt aus."
    )


def test_departures_tool_can_be_disabled():
    config = Config()
    assert "get_departures" in {s["function"]["name"] for s in schemas(config)}
    config.research.transit_url = ""
    assert "get_departures" not in {s["function"]["name"] for s in schemas(config)}


async def test_research_results_are_cached():
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/search/page"):
            return httpx.Response(200, json={"pages": [{"key": "LoRa"}]})
        return httpx.Response(200, json={"title": "LoRa", "extract": "Funktechnik."})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        tools = Tools(Config(), Store(":memory:"), client)
        first = await tools.run("search_wikipedia", {"query": "LoRa"})
        second = await tools.run("search_wikipedia", {"query": " lora "})
    assert first == second and len(calls) == 2


@pytest.mark.parametrize(
    ("question", "cacheable"),
    [("Was gibt es Neues in Berlin?", True), ("und morgen?", False), ("Wer war Hans Baluschek?", True),
     ("Was hältst du davon?", False), ("mehr", False)],
)
def test_answer_cache_key(question, cacheable):
    assert (answer_cache_key(question) is not None) is cacheable


def research_ai(payloads, final="Laut tagesschau: Neues."):
    def respond(request):
        if request.url.host == "openrouter.ai":
            payload = json.loads(request.content)
            payloads.append(payload)
            if not any(m["role"] == "tool" for m in payload["messages"]):
                call = {"id": "a", "type": "function",
                        "function": {"name": "search_news", "arguments": json.dumps({"query": "Berlin"})}}
                return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [call]}}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": final}}]})
        if request.url.host == "www.tagesschau.de":
            return httpx.Response(200, json={"news": [{"title": "Meldung", "shareURL": "https://t.de/m"}]})
        return httpx.Response(503)

    return respond


async def test_researched_answers_are_reused_and_research_model_used():
    config = Config()
    config.ai._api_key = "synthetic"
    config.ai.requests_per_minute = 60
    config.research.model = "google/gemini-2.5-pro"
    payloads = []
    store = Store(":memory:")
    async with httpx.AsyncClient(transport=httpx.MockTransport(research_ai(payloads))) as client:
        assistant = Assistant(config, store, client)
        first = await assistant.answer("Was gibt es Neues in Berlin?")
        second = await assistant.answer("was gibt es neues in berlin")
    assert first.text == second.text == "Laut tagesschau: Neues."
    assert len(payloads) == 2
    assert payloads[0]["model"] == config.ai.model and payloads[1]["model"] == "google/gemini-2.5-pro"
    store.close()


async def test_offline_phone_model_gets_kiwix_facts(monkeypatch):
    config = Config()
    config.ai.base_url = "https://primary.example/v1"
    config.ai._api_key = "primary-test-only"
    config.ai.offline_fallback_url = "http://127.0.0.1:18180/v1"
    config.ai._phone_api_key = "phone-test-only"
    config.research.kiwix_url = "http://127.0.0.1:8081"
    seen = []

    def unavailable(request):
        raise httpx.ConnectError("offline", request=request)

    def phone(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "Ein Maler aus Berlin."}}]})

    async def fake_kiwix(topic):
        assert topic == "Hans Baluschek"
        return {"text": "Wikipedia offline „Hans Baluschek“: deutscher Maler.", "sources": ["https://x"]}

    original = httpx.AsyncClient
    async with original(transport=httpx.MockTransport(unavailable)) as primary:
        monkeypatch.setattr(
            assistant_module.httpx, "AsyncClient",
            lambda **kwargs: original(transport=httpx.MockTransport(phone), **kwargs),
        )
        assistant = Assistant(config, Store(":memory:"), primary)
        monkeypatch.setattr(assistant.tools.research, "wikipedia_offline", fake_kiwix)
        answer = await assistant.answer("Wer war Hans Baluschek?")
    assert answer.text == "Lokale KI: Ein Maler aus Berlin."
    assert "Quellenmaterial" in seen[0]["messages"][0]["content"]
    assert "deutscher Maler" in seen[0]["messages"][0]["content"]


@pytest.mark.parametrize(
    ("question", "topic"),
    [("Wer war Hans Baluschek?", "Hans Baluschek"), ("Was ist ein Repeater?", "Repeater"),
     ("Schreibe einen kurzen Hinweis zur Funkrunde.", "")],
)
def test_kiwix_topic(question, topic):
    assert kiwix_topic(question) == topic


def test_answer_cache_seconds_validated():
    config = Config()
    config.research.answer_cache_seconds = 99999
    with pytest.raises(ValueError):
        config.validate()


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Hier die Details:\n\n*   **Politik:** Verhandlungen.\n*   **Wirtschaft:** Mäc Geiz.",
         "Hier die Details: Politik: Verhandlungen. Wirtschaft: Mäc Geiz."),
        ("17. Mai ist ein Sonntag.", "17. Mai ist ein Sonntag."),
        ("## Titel\n1. Eins\n2. Zwei", "Titel Eins Zwei"),
    ],
)
def test_plain_radio_text(raw, clean):
    from meshcore_ai_bot.engine import plain_radio_text

    assert plain_radio_text(raw) == clean


async def test_gateway_rate_limit_gets_clear_message(monkeypatch):
    async def no_wait(_):
        pass

    monkeypatch.setattr(assistant_module.asyncio, "sleep", no_wait)
    config = Config()
    config.ai._api_key = "synthetic"
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(429))) as client:
        answer = await Assistant(config, Store(":memory:"), client).answer("Erzähl mir etwas über Funkwellen bitte")
    assert "viele Fragen unterwegs" in answer.text


@pytest.mark.parametrize("query", ["*", "alle", ""])
def test_repeater_catalogue_wildcards_list_everything(query):
    store = Store(":memory:")
    store.update_repeaters([{"public_key": "ab" * 32, "adv_name": "Friedenau Rep", "type": 2, "last_advert": 1}], 1)
    tools = Tools(Config(), store, None)
    result = tools.catalogue(query)
    assert result.get("count", 0) >= 1, result
