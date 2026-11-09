"""'mehr' and 'Quelle?', offline facts for the phone model, departures, caches and research model."""

import json
import time
from types import SimpleNamespace

import httpx
import pytest

import meshcore_ai_bot.assistant as assistant_module
from meshcore_ai_bot.assistant import Assistant, answer_cache_key, kiwix_topic
from meshcore_ai_bot.config import Channel, Config
from meshcore_ai_bot.engine import Engine, source_name, source_names
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
     ("woher weißt du das?", "source"), ("Woher hast du die Nachricht?", "source"),
     ("woher stammt die Info", "source"), ("Wo steht das?", "source"), ("Welche Quelle?", "source"),
     ("Mehr Wetter in Berlin", None), ("Was ist eine Quelle?", None), ("Woher kommt der Wind?", None), ("Woher kommt die Sonne?", None)],
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
        assert "(weiter\u00a0mit\u00a0„mehr“)" in first and "ENDE" not in first

        engine.receive(Incoming("#fragdenbot", "Alice", "mehr", now + 1))
        await engine.process_one()
        second = "".join(sent_texts(store))
        assert "Viele Details" in second and asked == ["Erzähl was über Friedenau"]

        engine.receive(Incoming("#fragdenbot", "Bob", "Quelle?", now + 2))
        await engine.process_one()
        assert "keine Online-Quelle" in "".join(sent_texts(store))

        engine.receive(Incoming("#fragdenbot", "Alice", "Quelle?", now + 3))
        await engine.process_one()
        assert "Quelle: rbb24" in "".join(sent_texts(store))
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
        assert asked[1].startswith("Erzähl mir bitte weitere, neue Details zu: Was ist LoRa?")
    finally:
        store.close()


def test_source_names_instead_of_links():
    assert source_name("https://www.tagesschau.de/inland/x-100.html") == "tagesschau"
    assert source_name("https://de.wikipedia.org/wiki/Hans_Baluschek") == "Wikipedia (Hans Baluschek)"
    assert source_name("https://www.example.org/liveblog-100.html") == "example.org"
    assert source_names(["https://www.rbb24.de/a.html", "https://www.rbb24.de/b.html",
                         "https://www.tagesschau.de/c.html"]) == "Quellen: rbb24, tagesschau"


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


@pytest.mark.parametrize(
    ("question", "text", "clean"),
    [
        ("Was ist wichtig?", "Das, was dir Sinn stiftet. Nenne mir dein Sternzeichen für ein Horoskop.",
         "Das, was dir Sinn stiftet."),
        ("Horoskop bitte", "Nenne mir dein Sternzeichen.", "Nenne mir dein Sternzeichen."),
    ],
)
def test_unasked_zodiac_question_is_dropped(question, text, clean):
    from meshcore_ai_bot.assistant import drop_unasked_zodiac

    assert drop_unasked_zodiac(question, text) == clean


async def test_rate_limit_after_research_still_returns_found_facts(monkeypatch):
    async def no_wait(_):
        pass

    monkeypatch.setattr(assistant_module.asyncio, "sleep", no_wait)
    config = Config()
    config.ai._api_key = "synthetic"
    config.ai.requests_per_minute = 60
    payloads = []
    tool_answer = research_ai(payloads)

    def respond(request):
        if request.url.host == "openrouter.ai" and len(payloads) >= 1:
            payloads.append(None)
            return httpx.Response(429)
        return tool_answer(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        answer = await Assistant(config, Store(":memory:"), client).answer("Was gibt es Neues in Berlin?")
    assert answer.text.startswith("Kurz aus meiner Recherche:") and "Meldung" in answer.text
    assert "https://t.de/m" in answer.sources


async def test_last_news_survive_an_outage():
    online = [True]

    def respond(request):
        if online[0]:
            if request.url.host == "www.tagesschau.de":
                return httpx.Response(200, json={"news": [{"title": "Meldung A", "shareURL": "https://www.tagesschau.de/a"}]})
            return httpx.Response(200, text="<rss></rss>")
        raise httpx.ConnectError("offline", request=request)

    config = Config()
    config.research.news_feeds = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        tools = Tools(config, Store(":memory:"), client)
        first = await tools.run("search_news", {"query": ""})
        online[0] = False
        second = await tools.run("search_news", {"query": "Berlin"})
    assert "Meldung A" in first["text"]
    assert second["text"].startswith("Gerade keine Verbindung. Stand ") and "Meldung A" in second["text"]


async def test_gateway_outage_switches_to_phone_without_waiting_again(monkeypatch):
    config = Config()
    config.ai.base_url = "https://primary.example/v1"
    config.ai._api_key = "primary-test-only"
    config.ai.offline_fallback_url = "http://127.0.0.1:18180/v1"
    config.ai._phone_api_key = "phone-test-only"
    config.ai.requests_per_minute = 60
    primary_calls, phone_calls = [], []

    def unavailable(request):
        primary_calls.append(1)
        raise httpx.ConnectError("offline", request=request)

    def phone(request):
        phone_calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "Antwort."}}]})

    original = httpx.AsyncClient
    async with original(transport=httpx.MockTransport(unavailable)) as primary:
        monkeypatch.setattr(
            assistant_module.httpx, "AsyncClient",
            lambda **kwargs: original(transport=httpx.MockTransport(phone), **kwargs),
        )
        assistant = Assistant(config, Store(":memory:"), primary)
        await assistant.answer("Erzähl mir etwas über Funkwellen bitte")
        await assistant.answer("Was ist ein Repeater im MeshCore-Netz?")
    assert len(primary_calls) == 1 and len(phone_calls) == 2
    assert "MeshCore-Wissen" in phone_calls[1]["messages"][0]["content"]


def test_lora_glossary_explains():
    assert radio_check("Was ist LoRa?") is None
    from meshcore_ai_bot.radio_terms import short_radio_reply

    answer = short_radio_reply("Was ist LoRa?", name="X", location="Berlin", timezone_name="Europe/Berlin")
    assert answer["answer"].startswith("LoRa: Long Range")


def test_broadcaster_names():
    assert source_name("https://www.swr.de/swraktuell/x.html") == "SWR"
    assert source_name("https://www.zdfheute.de/x") == "ZDF"


def test_cut_ends_after_a_sentence_and_hint_stays_in_one_packet():
    from meshcore_ai_bot.protocol import MORE_ENDING, fit_reply
    text = "Erster Satz ist da. " * 3 + "Ein sehr langer Satz ohne Ende " * 20
    shown, parts = fit_reply("@[A] ", text, 110, 3)
    assert shown.endswith(MORE_ENDING)
    head = shown[: -len(MORE_ENDING)]
    assert text[len(head)] == " "  # whole words only
    text = "Kurzer Satz. " * 30
    shown, _ = fit_reply("@[A] ", text, 110, 3)
    assert shown[: -len(MORE_ENDING)].endswith("Satz.")
    assert any(MORE_ENDING.strip() in part for part in parts)


def test_lines_addressed_to_someone_else_are_ignored():
    import time

    from meshcore_ai_bot.config import Config
    from meshcore_ai_bot.protocol import Incoming, question_for
    config = Config()
    config.station.name = "Ideenwerft Friedenau"
    channel = config.channel("#fragdenbot")
    if channel is None or channel.mode == "off":
        return
    now = int(time.time())
    assert question_for(Incoming("#fragdenbot", "X", "@[Matze T1000E] 😸", now), config, now) is None
    assert question_for(Incoming("#fragdenbot", "X", "@[Ideenwerft Friedenau 2] hallo", now), config, now) is None


def test_dry_day_is_not_called_rain():
    from meshcore_ai_bot.tools import day_description
    daily = {"weather_code": [61], "precipitation_sum": [0], "precipitation_probability_max": [0]}
    assert day_description(daily, 0) == "bewölkt, trocken"
    daily["precipitation_sum"] = [2.5]
    assert day_description(daily, 0) == "Regen"


async def test_trains_only_with_platform():
    def respond(request):
        if request.url.path == "/locations":
            return httpx.Response(200, json=[{"type": "stop", "id": "900003201", "name": "S+U Berlin Hauptbahnhof"}])
        assert request.url.params["bus"] == "false" and request.url.params["duration"] == "90"
        return httpx.Response(200, json={"departures": [
            {"when": "2026-11-07T16:48:00+01:00", "delay": 300, "direction": "Karlsruhe, Hauptbahnhof",
             "line": {"name": "ICE 379"}, "platform": "3"},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await Tools(Config(), Store(":memory:"), client).run(
            "get_departures", {"stop": "Berlin Hbf", "trains": "ja"}
        )
    assert "16:48 ICE 379 → Karlsruhe, Hauptbahnhof Gl. 3 (+5)" in result["text"]


async def test_ber_departures_and_flight_number():
    seen = []

    def respond(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"data": {"items": [{
            "flight_number": "FR  146", "scheduled_time": "2026-11-07T16:40:00+01:00",
            "dep_estimated_time": "2026-11-07T17:00:00+01:00", "arr_airport_name": "London Stansted",
            "dep_airport_name": "Berlin", "flight_status_label": "Ende Einstieg", "gate": "B40",
        }]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        tools = Tools(Config(), Store(":memory:"), client)
        result = await tools.run("get_flights", {"direction": "abflug", "query": "fr 146"})
        arrivals = await tools.run("get_flights", {"direction": "ankunft"})
    assert seen[0]["search"] == "FR146" and seen[0]["arrivalDeparture"] == "D"
    assert result["text"] == "BER Abflüge (Live): 16:40 FR 146 → London Stansted (+20), Ende Einstieg, Gate B40."
    assert seen[1]["arrivalDeparture"] == "A" and arrivals["text"].startswith("BER Ankünfte (Live): 16:40 FR 146 aus Berlin")


async def test_planes_overhead_with_route():
    def respond(request):
        if "opensky" in request.url.host:
            return httpx.Response(200, json={"states": [
                ["3c6743", "DLH6LJ  ", "Germany", 0, 0, 13.35, 52.48, 3900.0, False, 200, 90, 0, None, 4012.0],
                ["abc", "", "X", 0, 0, 13.3, 52.4, 100.0, False, 0, 0, 0, None, None],
                ["def", "GRND1", "X", 0, 0, 13.3, 52.4, None, True, 0, 0, 0, None, None],
            ]})
        return httpx.Response(200, json={"response": {"flightroute": {
            "callsign_iata": "LH6LJ", "airline": {"name": "Lufthansa"},
            "origin": {"municipality": "London"}, "destination": {"municipality": "Frankfurt am Main"}}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await Tools(Config(), Store(":memory:"), client).run("get_planes_overhead", {})
    assert result["text"].startswith("Flugzeuge bei ")
    assert "LH6LJ Lufthansa, London → Frankfurt am Main, 4000 m hoch, 2 km entfernt" in result["text"]
    assert "GRND1" not in result["text"]


async def test_rescue_helicopter_only_and_stale_routes_dropped():
    def respond(request):
        if "opensky" in request.url.host:
            return httpx.Response(200, json={"states": [
                ["a", "VJT683", "X", 0, 0, 13.4, 52.5, 200.0, False, 0, 0, 0, None, 200.0],
                ["b", "CHX31", "X", 0, 0, 13.3, 52.45, 300.0, False, 0, 0, 0, None, 300.0],
            ]})
        return httpx.Response(200, json={"response": {"flightroute": {
            "callsign_iata": "VJT683", "origin": {"municipality": "Cork"},
            "destination": {"municipality": "Farnborough"}}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        tools = Tools(Config(), Store(":memory:"), client)
        rescue = await tools.run("get_planes_overhead", {"area": "rettung"})
        everything = await tools.run("get_planes_overhead", {"area": "berlin"})
    assert "Rettungshubschrauber Christoph 31" in rescue["text"] and "VJT" not in rescue["text"]
    assert "Cork" not in everything["text"] and "VJT683" in everything["text"]


async def test_no_rescue_helicopter_in_the_air():
    def respond(request):
        return httpx.Response(200, json={"states": [
            ["a", "DLH1", "X", 0, 0, 13.4, 52.5, 9000.0, False, 0, 0, 0, None, 9000.0]]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await Tools(Config(), Store(":memory:"), client).run("get_planes_overhead", {"area": "rettung"})
    assert result["text"].startswith("Gerade ist kein Rettungshubschrauber")


def test_flight_tools_offered_with_research():
    names = {s["function"]["name"] for s in schemas(Config())}
    assert {"get_flights", "get_planes_overhead"} <= names


@pytest.mark.parametrize("question,expected", [
    ("Welche Flugzeuge fliegen gerade über uns?", ("get_planes_overhead", {})),
    ("was fliegt da gerade", ("get_planes_overhead", {})),
    ("Welche Flugzeuge sind gerade über Berlin?", ("get_planes_overhead", {"area": "berlin"})),
    ("Flugzeuge über Berlin", ("get_planes_overhead", {"area": "berlin"})),
    ("welches Flugzeug fliegt gleich am BER ab", ("get_flights", {"direction": "abflug", "query": ""})),
    ("Wann landet LH 1958 am BER?", ("get_flights", {"direction": "ankunft", "query": "LH 1958"})),
    ("Status Flug FR146", ("get_flights", {"direction": "abflug", "query": "FR146"})),
    ("Flüge vom BER nach London", ("get_flights", {"direction": "abflug", "query": "London"})),
    ("Ankünfte BER aus Lissabon", ("get_flights", {"direction": "ankunft", "query": "Lissabon"})),
    ("Wo ist der Rettungshubschrauber?", ("get_planes_overhead", {"area": "rettung"})),
    ("wo ist christoph 31 gerade", ("get_planes_overhead", {"area": "rettung"})),
    ("Wie funktioniert ein Flugzeug?", None),
    ("Wann fährt der nächste Bus am Knausplatz", None),
    ("Was ist LoRa?", None),
])
def test_flight_questions_go_to_live_data(question, expected):
    from meshcore_ai_bot.assistant import flight_request
    assert flight_request(question) == expected


def test_sunshine_hours_in_forecast():
    from meshcore_ai_bot.tools import sunshine
    assert sunshine({"sunshine_duration": [14400.0]}, 0) == ", etwa 4 Std. Sonne"
    assert sunshine({"sunshine_duration": [600.0]}, 0) == ", kaum Sonne"
    assert sunshine({}, 0) == ""


@pytest.mark.parametrize("question,expected", [
    ("wird es morgen sonnig", {"location": "Berlin-Friedenau", "period": "tomorrow"}),
    ("wie viel Sonne wird es geben", {"location": "Berlin-Friedenau", "period": "today"}),
    ("Regnet es heute in Hamburg?", {"location": "Hamburg", "period": "today"}),
    ("Wetter übermorgen", {"location": "Berlin-Friedenau", "period": "day_after_tomorrow"}),
    ("Wie warm ist es gerade?", {"location": "Berlin-Friedenau", "period": "now"}),
    ("Was ist eine Inversionswetterlage?", None),
    ("Brauche ich morgen eine Mütze?", None),
    ("Was kann man bei Regen in Berlin unternehmen?", None),
])
def test_weather_questions_answered_from_data(question, expected):
    from meshcore_ai_bot.assistant import weather_question
    assert weather_question(question, "Berlin-Friedenau") == expected


def test_weather_verdicts():
    from meshcore_ai_bot.assistant import weather_verdict
    assert weather_verdict("wird es morgen sonnig", {"sun_h": 0.2}) == "Eher nicht sonnig."
    assert weather_verdict("wird es sonnig", {"sun_h": 5}) == "Ja, ziemlich sonnig."
    assert weather_verdict("regnet es morgen", {"rain_prob": 0, "rain_mm": 0}) == "Nein, es bleibt wohl trocken."
    assert weather_verdict("brauche ich einen Schirm", {"rain_prob": 80, "rain_mm": 3}) == "Ja, es wird regnen (etwa 3 mm)."
    assert weather_verdict("wie wird das Wetter", {"sun_h": 5}) == ""


async def test_sunny_question_gets_verdict_without_ai(monkeypatch):
    config = Config()
    assistant = Assistant(config, Store(":memory:"), None)

    async def fake_run(name, args, **_):
        assert name == "get_weather" and args["period"] == "tomorrow"
        return {"text": "Berlin-Friedenau 2026-11-08: 1.1–7.6 °C, Nebel, kaum Sonne.", "sources": ["https://open-meteo.com/"],
                "day": {"sun_h": 0.1, "rain_prob": 0, "rain_mm": 0, "code": 45}}

    monkeypatch.setattr(assistant.tools, "run", fake_run)
    answer = await assistant.answer("wird es morgen sonnig")
    assert answer.text.startswith("Eher nicht sonnig. Berlin-Friedenau 2026-11-08")


def test_when_does_the_sun_come_back():
    from datetime import date

    from meshcore_ai_bot.assistant import weather_question, weather_when
    assert weather_question("wann scheint die sonne wieder", "Friedenau")["period"] == "week"
    days = [
        {"date": date(2026, 11, 9), "sun_h": 0.1, "rain_prob": 10, "rain_mm": 0, "code": 45},
        {"date": date(2026, 11, 10), "sun_h": 0.5, "rain_prob": 70, "rain_mm": 3, "code": 61},
        {"date": date(2026, 11, 11), "sun_h": 5.2, "rain_prob": 0, "rain_mm": 0, "code": 1},
    ]
    assert weather_when("wann scheint die sonne wieder", days) == "Am Mittwoch (11.11.) gibt es wieder Sonne, etwa 5 Std."
    assert weather_when("wann regnet es wieder", days) == "Morgen ist wieder mit Regen zu rechnen."
    assert weather_when("wann scheint die sonne", days[:2]) == "In den nächsten Tagen ist kaum Sonne in Sicht."


async def test_week_outlook_from_forecast(monkeypatch):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from meshcore_ai_bot.tools import week_outlook
    today = datetime.now(ZoneInfo("Europe/Berlin")).date()
    daily = {"time": [(today + timedelta(days=n)).isoformat() for n in range(3)],
             "sunshine_duration": [0, 1800, 18000], "precipitation_probability_max": [0, 0, 0],
             "precipitation_sum": [0, 0, 0], "weather_code": [45, 3, 1]}
    result = week_outlook("Friedenau", daily, "Europe/Berlin", 0)
    assert result["text"].startswith("Friedenau, Sonnenstunden: heute 0h, morgen 0h, ")
    assert len(result["days"]) == 3 and result["days"][2]["sun_h"] == 5


def test_hour_exact_sun_and_rain():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from meshcore_ai_bot.assistant import hourly_when
    from meshcore_ai_bot.tools import hour_windows, sunny_hour, windows
    now = datetime.now(ZoneInfo("Europe/Berlin")).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    base = now.replace(hour=0)
    hours = [{"time": base + timedelta(hours=n), "sun_s": 0, "mm": 0, "prob": 0} for n in range(72)]
    for n in (24 + 10, 24 + 11, 24 + 12):  # tomorrow 10–13
        hours[n]["sun_s"] = 3600
    for n in (24 + 15, 24 + 16):  # tomorrow rain 15–17
        hours[n]["mm"] = 1.2
    assert hourly_when("wann scheint die sonne wieder", hours, "Europe/Berlin") == (
        "Morgen ab 10 Uhr scheint die Sonne, bis etwa 13 Uhr."
    )
    assert hourly_when("wann regnet es", hours, "Europe/Berlin") == "Morgen ab 15 Uhr regnet es, bis etwa 17 Uhr."
    assert hourly_when("wann hört der regen auf", hours, "Europe/Berlin").startswith("Es ist gerade trocken")
    assert hour_windows(hours[24:48], sunny_hour) == [(10, 13)]
    target = (base + timedelta(days=1)).date().isoformat()
    hourly = {"time": [h["time"].isoformat() for h in hours], "sunshine_duration": [h["sun_s"] for h in hours],
              "precipitation": [h["mm"] for h in hours], "precipitation_probability": [h["prob"] for h in hours]}
    assert windows({"hourly": hourly}, target, "sun") == " (10–13 Uhr)"
    assert windows({"hourly": hourly}, target, "rain") == ", Regen vor allem 15–17 Uhr"


@pytest.mark.parametrize("question,expected", [
    ("naechster zug ab s friedenau", {"stop": "s friedenau", "trains": "ja"}),
    ("wann kommt der nächste Bus am Knausplatz", {"stop": "Knausplatz"}),
    ("Züge ab Berlin Hbf", {"stop": "Berlin Hbf", "trains": "ja"}),
    ("nächste Abfahrt Breslauer Platz", {"stop": "Breslauer Platz"}),
    ("Wann fährt der nächste ICE von Berlin Hbf nach Hamburg", {"stop": "Berlin Hbf", "trains": "ja"}),
    ("Was ist ein Zug?", None),
    ("Was kostet ein Ticket von Spandau nach Hamburg", None),
    ("Wie lange fährt man von Spandau nach Hamburg mit dem Zug", None),
])
def test_transit_questions_go_to_departures(question, expected):
    from meshcore_ai_bot.assistant import transit_request
    assert transit_request(question) == expected


async def test_train_at_sbahn_only_station_falls_back_to_sbahn():
    calls = []

    def respond(request):
        if request.url.path == "/locations":
            return httpx.Response(200, json=[{"type": "stop", "id": "900061101", "name": "S Friedenau (Berlin)"}])
        calls.append(request.url.params.get("suburban"))
        if request.url.params.get("suburban") == "false":
            return httpx.Response(200, json={"departures": []})
        return httpx.Response(200, json={"departures": [
            {"when": "2026-11-09T12:25:00+01:00", "direction": "S Frohnau (Berlin)", "line": {"name": "S1"}, "platform": "1"},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await Tools(Config(), Store(":memory:"), client).run(
            "get_departures", {"stop": "S Friedenau", "trains": "ja"}
        )
    assert calls == ["false", None]
    assert "12:25 S1 → S Frohnau (Berlin) Gl. 1" in result["text"]


@pytest.mark.parametrize("question,expected", [
    ("wie komme ich von s friedenau nach postdammer platz", {"origin": "s friedenau", "destination": "postdammer platz"}),
    ("Verbindung von Südkreuz nach Alexanderplatz", {"origin": "Südkreuz", "destination": "Alexanderplatz"}),
    ("Wie komme ich zum Alexanderplatz?", {"origin": "Berlin-Friedenau", "destination": "Alexanderplatz"}),
    ("Wie komme ich mit dem Fahrrad nach Potsdam", None),
    ("Ich komme von der Arbeit nach Hause", None),
    ("Was kostet ein Ticket von Spandau nach Hamburg", None),
])
def test_journey_questions(question, expected):
    from meshcore_ai_bot.assistant import journey_request
    assert journey_request(question, "Berlin-Friedenau") == expected


async def test_journey_from_transport_rest():
    def respond(request):
        if request.url.path == "/locations":
            q = request.url.params["query"]
            if "friedenau" in q:
                return httpx.Response(200, json=[{"type": "stop", "id": "900061102", "name": "S Friedenau (Berlin)"}])
            return httpx.Response(200, json=[{"type": "stop", "id": "900100020", "name": "S+U Potsdamer Platz Bhf (Berlin)"}])
        assert request.url.path == "/journeys"
        return httpx.Response(200, json={"journeys": [
            {"legs": [
                {"line": {"name": "S1"}, "origin": {"name": "S Friedenau (Berlin)"},
                 "destination": {"name": "S+U Potsdamer Platz Bhf (Berlin)"}, "departure": "2026-11-09T12:34:00+01:00",
                 "arrival": "2026-11-09T12:46:00+01:00", "departurePlatform": "2", "departureDelay": 60},
            ]},
            {"legs": [
                {"walking": True, "origin": {"name": "S Friedenau (Berlin)"}, "destination": {"name": "U Walther-Schreiber-Platz"},
                 "departure": "2026-11-09T12:30:00+01:00", "arrival": "2026-11-09T12:35:00+01:00"},
                {"line": {"name": "M76"}, "origin": {"name": "U Walther-Schreiber-Platz (Berlin)"},
                 "destination": {"name": "S Priesterweg (Berlin)"}, "departure": "2026-11-09T12:36:00+01:00",
                 "arrival": "2026-11-09T12:44:00+01:00"},
                {"line": {"name": "S2"}, "origin": {"name": "S Priesterweg (Berlin)"},
                 "destination": {"name": "S+U Potsdamer Platz Bhf (Berlin)"}, "departure": "2026-11-09T12:47:00+01:00",
                 "arrival": "2026-11-09T12:56:00+01:00"},
            ]},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await Tools(Config(), Store(":memory:"), client).run(
            "get_journey", {"origin": "s friedenau", "destination": "postdammer platz"}
        )
    assert result["text"] == (
        # the bus trip leaves only 4 minutes earlier but arrives 10 minutes later: not worth airtime
        "S Friedenau → S+U Potsdamer Platz Bhf: 12:34 (+1) S1 Gl. 2, an 12:46 (12 Min.)."
    )


@pytest.mark.parametrize("typed,expected", [
    ("wie wird das weter morgn", "wie wird das wetter morgen"),
    ("wan scheint die sone wieder?", "wann scheint die sonne wieder?"),
    ("Wo is der retungshubschrauber", "Wo ist der rettungshubschrauber"),
    ("nexter zug ab s friedenau", "nächster zug ab s friedenau"),
    ("wie kom ich zum alex", "wie komme ich zum Alexanderplatz"),
    ("ist es 20 grad warm", "ist es 20 grad warm"),
    ("Was fliegt da grad", "Was fliegt da gerade"),
    ("Wer war Hans Baluschek?", "Wer war Hans Baluschek?"),
    ("welche fluege starten am ber", "welche fluege starten am ber"),
])
def test_typos_fixed_before_routing(typed, expected):
    from meshcore_ai_bot.assistant import fix_typos
    assert fix_typos(typed) == expected


@pytest.mark.parametrize("typed,route", [
    ("wird es morgn sonnig", ("weather", {"location": "Berlin-Friedenau", "period": "tomorrow"})),
    ("regnet es heute in hamburg", ("weather", {"location": "Hamburg", "period": "today"})),
    ("wetter ubermorgen", ("weather", {"location": "Berlin-Friedenau", "period": "day_after_tomorrow"})),
    ("was fliegt da grad", ("flight", ("get_planes_overhead", {}))),
    ("wann landet fr146", ("flight", ("get_flights", {"direction": "ankunft", "query": "FR146"}))),
    ("ist der flug aus london gelandet", ("flight", ("get_flights", {"direction": "ankunft", "query": "london"}))),
    ("wann faehrt die s1 ab friedenau", ("transit", {"stop": "friedenau"})),
    ("abfarten rathaus steglitz", ("transit", {"stop": "rathaus steglitz"})),
    ("wie komm ich zum alex", ("journey", {"origin": "S Friedenau", "destination": "Alexanderplatz"})),
    ("wie komme ich zum flughafen ber", ("journey", {"origin": "S Friedenau", "destination": "flughafen ber"})),
])
def test_typed_questions_take_the_right_route(typed, route):
    from meshcore_ai_bot.assistant import (
        fix_typos,
        flight_request,
        journey_request,
        transit_request,
        weather_question,
    )
    text = fix_typos(typed)
    found = [("weather", weather_question(text, "Berlin-Friedenau")), ("journey", journey_request(text, "S Friedenau")),
             ("transit", transit_request(text)), ("flight", flight_request(text))]
    assert next((name, value) for name, value in found if value) == route


def test_journey_follow_ups_keep_the_conversation():
    from datetime import datetime

    from meshcore_ai_bot.assistant import CLOSING, journey_request, transit_request
    now = datetime(2026, 11, 9, 22, 46)
    history = []

    def ask(text):
        result = journey_request(text, "S Friedenau", history, now)
        history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": "…"}])
        return result

    first = ask("ich muss morgen um acht am Heidelberger Platz sein von s Friedenau mit den öffentlichen Verkehrsmitteln")
    assert first == {"origin": "s Friedenau", "destination": "Heidelberger Platz",
                     "when": "2026-11-10T08:00", "arrive": "ja"}
    assert ask("gehts auch ein bisschen früher")["when"] == "2026-11-10T07:30"
    assert ask("komm ich auch mit den Bus hin")["only"] == "bus"
    assert ask("mit umsteigen")["only"] == "bus"
    late = ask("nein damit ich um 8uhr da bin")
    assert late["when"] == "2026-11-10T08:00" and late["only"] == "bus" and late["arrive"] == "ja"
    assert ask("und zurück?")["origin"] == "Heidelberger Platz"
    assert CLOSING.fullmatch("ok stop") and not CLOSING.fullmatch("ok wie wird das wetter")
    assert ask("Wetter morgen") is None
    assert ask("gehts auch früher") is None  # the weather question ended the trip
    assert ask("Verbindung s Friedenau Rathaus Spandau") is None
    question = "welchen Bus muss ich nehmen damit ich um 8uhr da bin Heidelberger Platz von s Friedenau"
    assert transit_request(question) is None
    assert journey_request(question, "S Friedenau", [], now) == {
        "origin": "s Friedenau", "destination": "Heidelberger Platz", "when": "2026-11-10T08:00",
        "arrive": "ja", "only": "bus"}
    assert journey_request("welchen Bus vom Knausplatz zum Heidelberger Platz mit Ankunft um 8", "S Friedenau",
                           [], now)["origin"] == "Knausplatz"


async def test_journey_with_arrival_time_and_bus_only():
    seen = {}

    def respond(request):
        if request.url.path == "/locations":
            name = request.url.params["query"]
            return httpx.Response(200, json=[{"type": "stop", "id": "9" + str(len(name)), "name": name.title()}])
        seen.update(dict(request.url.params))
        legs = [
            {"line": {"name": "Bus 186"}, "origin": {"name": "S Friedenau"},
             "departure": f"2026-11-10T07:{m}:00+01:00", "arrival": f"2026-11-10T07:{int(m) + 12}:00+01:00"}
            for m in ("20", "35", "47")
        ]
        return httpx.Response(200, json={"journeys": [{"legs": [leg]} for leg in legs]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await Tools(Config(), Store(":memory:"), client).run("get_journey", {
            "origin": "s friedenau", "destination": "heidelberger platz", "when": "2026-11-10T08:00",
            "arrive": "ja", "only": "bus"})
    assert seen["arrival"].startswith("2026-11-10T08:00") and "departure" not in seen
    assert seen["bus"] == "true" and seen["suburban"] == "false" and seen["subway"] == "false"
    assert "Ankunft bis 08:00, nur Bus" in result["text"]
    assert "07:35 Bus 186" in result["text"] and "07:20" not in result["text"]


async def test_cancelled_departures_stay_in_time_order():
    def respond(request):
        if request.url.path == "/locations":
            return httpx.Response(200, json=[{"type": "stop", "id": "900061102", "name": "S Friedenau (Berlin)"}])
        return httpx.Response(200, json={"departures": [
            {"when": None, "plannedWhen": "2026-11-09T23:25:00+01:00", "cancelled": True,
             "line": {"name": "S1"}, "direction": "S Nordbahnhof"},
            {"when": "2026-11-09T23:10:00+01:00", "line": {"name": "S1"}, "direction": "S Wannsee"},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await Tools(Config(), Store(":memory:"), client).run("get_departures", {"stop": "S Friedenau"})
    assert result["text"].index("23:10") < result["text"].index("23:25 S1 → S Nordbahnhof fällt aus")


@pytest.mark.parametrize("question,expected", [
    ("Wie komme ich morgen früh um halb acht nach Mitte?",
     {"origin": "S Friedenau", "destination": "Mitte", "when": "2026-11-10T07:30"}),
    ("Wann muss ich los um 18 Uhr am Ostbahnhof zu sein?",
     {"origin": "S Friedenau", "destination": "Ostbahnhof", "when": "2026-11-10T18:00", "arrive": "ja"}),
    ("Wie komme ich nach Potsdam?", {"origin": "S Friedenau", "destination": "Potsdam"}),
    ("verbindung friedenau - hermannplatz", {"origin": "friedenau", "destination": "hermannplatz"}),
    ("Wie komme ich ohne Umsteigen nach Wannsee?", {"origin": "S Friedenau", "destination": "Wannsee", "direct": "ja"}),
    ("S1 nach Oranienburg wann?", {"origin": "S Friedenau", "destination": "Oranienburg"}),
    ("von hier zum Brandenburger Tor", {"origin": "S Friedenau", "destination": "Brandenburger Tor"}),
    ("Wie lange brauche ich nach Charlottenburg?", {"origin": "S Friedenau", "destination": "Charlottenburg"}),
    ("wie komme ich nach hause", None),
])
def test_more_journey_wordings(question, expected):
    from datetime import datetime

    from meshcore_ai_bot.assistant import fix_typos, journey_request
    assert journey_request(fix_typos(question), "S Friedenau", [], datetime(2026, 11, 9, 23, 20)) == expected


async def test_journey_to_a_place_and_without_tram_line():
    seen = []

    def respond(request):
        if request.url.path == "/locations":
            if "kadewe" in request.url.params["query"].casefold():
                return httpx.Response(200, json=[
                    {"type": "location", "id": "980105411", "name": "KaDeWe, 10789 Berlin-Schöneberg", "poi": True,
                     "latitude": 52.502, "longitude": 13.341},
                    {"type": "stop", "id": "900000001", "name": "Badeweg"}])
            return httpx.Response(200, json=[{"type": "stop", "id": "900061102", "name": "S Friedenau (Berlin)"}])
        seen.append(dict(request.url.params))
        if request.url.params.get("tram") == "true":
            return httpx.Response(404, json={"message": "no journeys found"})
        return httpx.Response(200, json={"journeys": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        tools = Tools(Config(), Store(":memory:"), client)
        await tools.run("get_journey", {"origin": "S Friedenau", "destination": "KaDeWe"})
        tram = await tools.run("get_journey", {"origin": "S Friedenau", "destination": "Alex", "only": "tram"})
    assert seen[0]["to.id"] == "980105411" and seen[0]["to.latitude"] == "52.502" and "to" not in seen[0]
    assert tram["text"] == "S Friedenau → Alex: nur mit Tram keine Verbindung gefunden."


def test_weather_follow_up_keeps_the_topic():
    from meshcore_ai_bot.assistant import weather_followup
    history = [{"role": "user", "content": "wie wird das wetter morgen"}, {"role": "assistant", "content": "…"}]
    assert weather_followup("und übermorgen?", history, "Berlin-Friedenau") == {
        "location": "Berlin-Friedenau", "period": "day_after_tomorrow"}
    assert weather_followup("und in Potsdam?", history, "Berlin-Friedenau") == {
        "location": "Potsdam", "period": "tomorrow"}
    other = [{"role": "user", "content": "Wer war Hans Baluschek?"}]
    assert weather_followup("und übermorgen?", other, "Berlin-Friedenau") is None


def test_way_back_starts_without_the_old_arrival_time():
    from datetime import datetime

    from meshcore_ai_bot.assistant import journey_request
    history = [{"role": "user", "content": "ich muss morgen um acht am Heidelberger Platz sein von s Friedenau"}]
    back = journey_request("und zurück?", "S Friedenau", history, datetime(2026, 11, 9, 22, 0))
    assert back == {"origin": "Heidelberger Platz", "destination": "s Friedenau"}


def test_place_search_keeps_food_and_place_apart():
    from meshcore_ai_bot.assistant import local_place_request
    assert local_place_request("Wo kann ich in Schöneberg Pizza essen?", "Berlin-Friedenau") == {
        "query": "Pizzeria", "location": "Schöneberg"}
    assert local_place_request("Gibt es ein gutes Café in Friedenau?", "Berlin-Friedenau") == {
        "query": "Café", "location": "Friedenau"}


@pytest.mark.parametrize("question,day,expected", [
    ("in Berlin wird es morgen nicht regnen.", {"rain_prob": 68, "rain_mm": 0}, "Eher kein Regen: 0 mm erwartet, nur Schauerrisiko bis 68 %."),
    ("wird es morgen regnen?", {"rain_prob": 90, "rain_mm": 4.2}, "Ja, es wird regnen (etwa 4 mm)."),
    ("morgen bleibt es trocken", {"rain_prob": 10, "rain_mm": 0}, "Es bleibt wohl trocken."),
    ("Regnet es morgen", {"rain_prob": 10, "rain_mm": 0}, "Nein, es bleibt wohl trocken."),
])
def test_rain_verdict_follows_the_amount(question, day, expected):
    from meshcore_ai_bot.assistant import weather_verdict
    assert weather_verdict(question, day) == expected


def test_question_about_a_stand_day_is_no_place_list():
    from meshcore_ai_bot.assistant import local_place_request
    assert local_place_request("ist morgen wieder Zeit für Gaumenschmaus Hähnchen in der Nähe", "Friedenau") is None
    assert local_place_request("Wo gibt es Hähnchen in der Nähe?", "Friedenau") == {
        "query": "Hähnchen-Restaurant", "location": "Friedenau"}


# GaumenSchmaus chicken trucks -------------------------------------------------------------------------

def _grill_rows(code: str) -> list[dict]:
    rows = {
        "today": [{"b": "REWE", "e": "Germaniastraße", "f": "12", "d": "12099", "i": 4, "p": "10:30",
                   "q": "18:30", "t": [1, 5], "v": None, "w": None, "x": ""}],
        "tue": [{"b": "REWE", "e": "Schlangenbaderstraße", "f": "25", "d": "14197", "i": 2, "p": "10:30",
                 "q": "18:30", "t": [2], "v": None, "w": None, "x": ""},
                {"b": "ROLLER", "e": "Goerzalle", "f": None, "d": "14167", "i": 7, "p": "10:30",
                 "q": "18:30", "t": [1, 2, 3, 4, 5], "v": "2026-11-10", "w": "2026-11-10", "x": "Urlaub"}],
        "fri": [{"b": "REWE", "e": "Germaniastraße", "f": "12", "d": "12099", "i": 4, "p": "10:30",
                 "q": "18:30", "t": [1, 5], "v": None, "w": None, "x": ""}],
    }
    return rows.get(code, [])


async def _grill(day: str, place: str = "", hour: int = 12) -> str:
    from datetime import datetime

    import httpx

    from meshcore_ai_bot.research import grill_stands

    def respond(request):
        code = request.url.path.split("/")[6]
        rows = _grill_rows(code)
        return httpx.Response(200, json={"status": "OK", "locations": rows} if rows else {"status": "ERROR"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await grill_stands(client, 52.47, 13.33, "Europe/Berlin", day, place,
                                    now=datetime(2026, 11, 9, hour, 0))  # a Monday
    assert result["sources"] == ["https://standorte.gaumenschmaus.de/liste"]
    return result["text"]


async def test_grill_tomorrow_lists_nearest_and_skips_cancelled():
    text = await _grill("morgen")
    assert text.startswith("GaumenSchmaus Morgen (Di 10.11.), am nächsten: REWE Schlangenbaderstraße 25, 2 km")
    assert "Goerzalle" not in text  # cancelled that day


async def test_grill_place_finds_next_date():
    text = await _grill("heute", "Schlangenbader")
    assert text == ("GaumenSchmaus Heute (Mo 09.11.) nicht dort. Nächster Termin Morgen (Di 10.11.): "
                    "REWE Schlangenbaderstraße 25, 2 km, 10:30–18:30.")
    assert "Freitag 13.11.: REWE Germaniastraße 12" in await _grill("morgen", "Germaniastraße")


async def test_grill_cancelled_place_says_why_and_no_day():
    text = await _grill("morgen", "Goerzalle")
    assert text.startswith("GaumenSchmaus Morgen (Di 10.11.) fällt aus (Urlaub). Nächster Termin")
    assert await _grill("samstag") == "GaumenSchmaus Samstag 14.11.: kein Grillwagen in der Nähe."
    assert "heute aber schon" not in await _grill("heute", hour=12)
    assert "(heute schon zu)" in await _grill("heute", hour=20)


def test_grill_questions_route_to_trucks():
    from meshcore_ai_bot.assistant import fix_typos, grill_request, local_place_request

    question = fix_typos("ist morgen wieder zeit fur gaumenschmauss hahnchen")
    assert grill_request(question) == {"day": "morgen", "place": ""}
    assert grill_request("Wann steht der Hähnchenwagen wieder am REWE Schlangenbader?") == {
        "day": "", "place": "REWE Schlangenbader"}
    assert grill_request("gaumenschmaus am freitag") == {"day": "freitag", "place": ""}
    assert grill_request("und am freitag?", [{"role": "user", "content": "gaumenschmaus morgen"}]) == {
        "day": "freitag", "place": ""}
    assert grill_request("wo gibt es hähnchen in schöneberg") is None
    assert grill_request("hähnchen rezept für morgen") is None
    assert grill_request("und morgen?", [{"role": "user", "content": "wetter heute"}]) is None
    assert local_place_request("wo gibt es hähnchen in schöneberg", "Friedenau")["query"] == "Hähnchen-Restaurant"
