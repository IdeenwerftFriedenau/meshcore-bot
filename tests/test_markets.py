"""#börsenbot: routing, quote format, assessment and channel topic."""

import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from meshcore_ai_bot.assistant import Assistant
from meshcore_ai_bot.config import Channel, Config
from meshcore_ai_bot.engine import Engine, source_names
from meshcore_ai_bot.markets import (
    assessment,
    corrected,
    market_answer,
    market_request,
    number,
    pretty,
    quote,
    resolve,
)
from meshcore_ai_bot.protocol import Incoming
from meshcore_ai_bot.store import Store

NOW = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
DAY = 86400


def chart(symbol, price, closes, *, kind="EQUITY", currency="EUR", day=(None, None), high=None, low=None,
          change=None, name="Firma"):
    start = int(NOW.timestamp()) - (len(closes) - 1) * DAY
    meta = {"symbol": symbol, "currency": currency, "instrumentType": kind, "shortName": name,
            "regularMarketPrice": price, "regularMarketDayLow": day[0], "regularMarketDayHigh": day[1],
            "fiftyTwoWeekHigh": high or max(closes), "fiftyTwoWeekLow": low or min(closes),
            "regularMarketTime": int(NOW.timestamp()) - 600}
    if change is not None:
        meta["regularMarketChangePercent"] = change
    return {"chart": {"result": [{"meta": meta, "timestamp": [start + i * DAY for i in range(len(closes))],
                                  "indicators": {"quote": [{"close": closes}]}}]}}


RISING = [100 + i * 0.5 for i in range(365)]  # steady climb to 282
FALLING = [300 - i * 0.5 for i in range(365)]


def yahoo(charts, searches=None, seen=None):
    def respond(request):
        if seen is not None:
            seen.append(str(request.url))
        if request.url.path.startswith("/v8/finance/chart/"):
            symbol = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=charts[symbol]) if symbol in charts else httpx.Response(404)
        if request.url.path == "/v1/finance/search":
            return httpx.Response(200, json={"quotes": (searches or {}).get(request.url.params["q"], [])})
        return httpx.Response(503)

    return respond


@pytest.mark.parametrize(
    "question,channel,expected",
    [
        ("Wie steht Rheinmetall?", True, ["Rheinmetall"]),
        ("rheinmetall", True, ["Rheinmetall"]),
        ("Vergleiche Apple, Nvidia und Microsoft", True, ["Apple", "Nvidia", "Microsoft"]),
        ("Bitcoin Einschätzung", True, ["Bitcoin"]),
        ("Goldpreis?", True, ["Gold (Feinunze)"]),
        ("Wie steht der DAX?", False, ["DAX"]),
        ("Was macht die SAP Aktie?", False, ["SAP"]),
        ("Wie steht Goldman Sachs Aktie", False, ["Goldman Sachs"]),
        ("Wie wird das Wetter morgen?", False, None),
        ("Was ist Gold?", False, None),
        ("Was ist ein ETF?", True, None),
        ("Hallo", True, None),
        ("Erzähl mir bitte weitere, neue Details zu: SAP", True, None),
    ],
)
def test_market_routing(question, channel, expected):
    found = market_request(question, channel=channel)
    assert (found["assets"] if found else None) == expected


def test_overview_and_advice_flags():
    assert market_request("Wie läuft die Börse heute?", channel=True)["overview"] is True
    assert market_request("Wie steht der DAX?", channel=False)["advice"] is False
    assert market_request("Soll ich die Tesla Aktie kaufen?", channel=False)["advice"] is True
    assert market_request("Tesla", channel=True)["advice"] is True  # the channel always gives a view


def test_german_numbers():
    assert number(26419.97, "INDEX") == "26.420"
    assert number(209.55) == "209,55"
    assert number(1.11532, "CURRENCY") == "1,1153"
    assert number(0.45213) == "0,4521"


async def test_single_quote_has_day_and_52_week_range_and_assessment():
    charts = {"RHM.DE": chart("RHM.DE", 285.0, RISING, day=(280.0, 286.5), high=286.0, low=99.0, change=1.234)}
    searches = {"Rheinmetall": [
        {"symbol": "RNMBY", "exchange": "PNK", "quoteType": "EQUITY", "longname": "Rheinmetall AG"},
        {"symbol": "RHM.DE", "exchange": "GER", "quoteType": "EQUITY", "longname": "Rheinmetall AG"},
    ]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(yahoo(charts, searches))) as client:
        result = await market_answer(client, ["Rheinmetall"], advice=True, tz="Europe/Berlin", now=NOW)
    text = result["text"]
    assert text.startswith("Rheinmetall 285,00 € +1,2 %. Tag 280,00–286,50, 52W 99,00–286,00, 1J +")
    assert "Einschätzung (keine Anlageberatung): Aufwärtstrend, nahe 52W-Hoch" in text
    assert "Rücksetzer" in text
    assert result["sources"] == ["https://finance.yahoo.com/quote/RHM.DE"]
    assert source_names(result["sources"]) == "Quelle: Yahoo Finance"


async def test_xetra_listing_preferred_and_unrelated_hits_rejected():
    charts = {"APC.DE": chart("APC.DE", 300.0, RISING, name="Apple")}
    searches = {
        "Apple": [
            {"symbol": "AAPL", "exchange": "NMS", "quoteType": "EQUITY", "longname": "Apple Inc."},
            {"symbol": "APC.DE", "exchange": "GER", "quoteType": "EQUITY", "longname": "Apple Inc."},
        ],
        "Hallo Welt": [{"symbol": "HLO", "exchange": "NMS", "quoteType": "EQUITY", "longname": "Halozyme"}],
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(yahoo(charts, searches))) as client:
        result = await market_answer(client, ["Apple", "Hallo Welt"], advice=False, tz="Europe/Berlin", now=NOW)
    assert result["text"].startswith("Apple 300,00 €")
    assert "Nicht gefunden: Hallo Welt." in result["text"]


async def test_comparison_is_compact_and_carries_one_disclaimer():
    charts = {"BTC-EUR": chart("BTC-EUR", 120.0, FALLING, kind="CRYPTOCURRENCY", change=-2.0),
              "GC=F": chart("GC=F", 280.0, RISING, kind="FUTURE", currency="USD", change=0.5)}
    async with httpx.AsyncClient(transport=httpx.MockTransport(yahoo(charts))) as client:
        result = await market_answer(client, ["Bitcoin", "Gold"], advice=True, tz="Europe/Berlin", now=NOW)
    text = result["text"]
    assert "Bitcoin 120,00 € -2,0 % (52W 118,00–300,00), Abwärtstrend, nahe 52W-Tief" in text
    assert "Gold (Feinunze) 280,00 $ +0,5 %" in text
    assert text.count("keine Anlageberatung") == 1
    assert len(text.encode()) < 330


async def test_falling_asset_is_called_weak():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            yahoo({"X": chart("X", 150.0, FALLING[:300] + [150.0] * 65)}))) as client:
        data = await quote(client, "X", "X")
    assert assessment(data).startswith("Abwärtstrend")


def stock_ai(payloads, note="Rüstungsaufträge treiben den Kurs, politische Risiken bleiben."):
    charts = {"RHM.DE": chart("RHM.DE", 285.0, RISING, change=1.0)}
    searches = {"Rheinmetall": [{"symbol": "RHM.DE", "exchange": "GER", "quoteType": "EQUITY",
                                 "longname": "Rheinmetall AG"}]}
    market = yahoo(charts, searches)

    def respond(request):
        if request.url.host == "openrouter.ai":
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": note}}]})
        return market(request)

    return respond


async def test_channel_advice_adds_one_ai_sentence_without_history():
    config = Config()
    config.ai._api_key = "synthetic"
    payloads = []
    store = Store(":memory:")
    async with httpx.AsyncClient(transport=httpx.MockTransport(stock_ai(payloads))) as client:
        answer = await Assistant(config, store, client).answer(
            "Soll ich Rheinmetall kaufen?", [{"role": "user", "content": "Wie wird das Wetter?"}], topic="boerse")
    store.close()
    assert answer.text.startswith("Rheinmetall 285,00 €")
    assert "keine Anlageberatung" in answer.text
    assert answer.text.endswith("Rüstungsaufträge treiben den Kurs, politische Risiken bleiben.")
    assert len(payloads) == 1 and "tools" not in payloads[0]
    assert "Wetter" not in json.dumps(payloads[0], ensure_ascii=False)


async def test_channel_small_talk_goes_to_the_model_with_stock_role():
    config = Config()
    config.ai._api_key = "synthetic"
    payloads = []
    store = Store(":memory:")
    async with httpx.AsyncClient(transport=httpx.MockTransport(stock_ai(payloads, "Moin!"))) as client:
        answer = await Assistant(config, store, client).answer("Was ist ein ETF?", topic="boerse")
        plain = await Assistant(config, store, client).answer("Was ist ein ETF?")
    store.close()
    assert answer.text == plain.text == "Moin!"
    assert "#börsenbot" in payloads[0]["messages"][0]["content"]
    assert "#börsenbot" not in payloads[1]["messages"][0]["content"]
    assert any(t["function"]["name"] == "get_quote" for t in payloads[0]["tools"])


async def test_quote_outage_is_said_plainly():
    config = Config()
    store = Store(":memory:")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(429))) as client:
        answer = await Assistant(config, store, client).answer("DAX", topic="boerse")
    store.close()
    assert answer.text == "Die Kursdaten sind gerade nicht abrufbar. Versuch es bitte gleich nochmal."


async def test_engine_passes_channel_topic():
    config = Config(channels=[Channel("#fragdenbot", "all"), Channel("#börsenbot", "all", topic="boerse")])
    store = Store(":memory:")
    seen = []

    class Answerer:
        async def answer(self, question, history, topic=""):
            seen.append(topic)
            return SimpleNamespace(text="ok", sources=[])

    try:
        engine = Engine(config, store, Answerer())
        now = int(time.time())
        engine.receive(Incoming("#börsenbot", "Alice", "SAP", now))
        await engine.process_one()
        engine.receive(Incoming("#fragdenbot", "Alice", "Hallo", now + 1))
        await engine.process_one()
    finally:
        store.close()
    assert seen == ["boerse", ""]


def test_unknown_topic_is_rejected():
    with pytest.raises(ValueError, match="topic"):
        Config(channels=[Channel("#x", "all", topic="casino")]).validate()


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Wie steht Rheinmetall?", ["Rheinmetall"]),  # #fragdenbot: known share + market word
        ("Soll ich Nvidia kaufen?", ["Nvidia"]),
        ("Wie läuft Siemens Energy?", ["Siemens Energy"]),
        ("Bitcoin Kurs", ["Bitcoin"]),
        ("Goldpreis aktuell", ["Gold (Feinunze)"]),
        ("Ist VW gestiegen?", ["Volkswagen"]),
        ("Wann kommt die Post?", None),
        ("Was macht Rheinmetall eigentlich?", None),  # what the company does: the model with get_quote
        ("Erste Hilfe Kurs in Berlin", None),
        ("Wie viel ist ein Pfund in Gramm?", None),
    ],
)
def test_general_channel_knows_shares(question, expected):
    found = market_request(question)
    assert (found["assets"] if found else None) == expected


def test_general_channel_market_overview():
    assert market_request("Wie läuft die Börse heute?")["overview"] is True
    assert market_request("Was machen die Märkte?")["overview"] is True
    assert market_request("Wie läuft der DAX?")["assets"] == ["DAX"]


@pytest.mark.parametrize(
    "typo,name",
    [("rheinmetal", "Rheinmetall"), ("bitcon", "Bitcoin"), ("siemns", "Siemens"), ("nvdia", "Nvidia"),
     ("telkom", "Deutsche Telekom"), ("hallo", None), ("wetter", None)],
)
def test_typos_find_known_assets(typo, name):
    found = corrected(typo)
    assert (found[1] if found else None) == name


def test_pretty_names():
    assert pretty("Siemens Aktiengesellschaft") == "Siemens"
    assert pretty("Dr. Ing. h.c. F. Porsche AG") == "Porsche"
    assert pretty("RENK Group AG") == "Renk Group"
    assert pretty("Münchener Rückversicherungs-Gesellschaft Aktiengesellschaft in München") == (
        "Münchener Rückversicherungs-Gesellschaft")
    assert pretty("The Coca-Cola Company") == "Coca-Cola Company"
    assert pretty("NVIDIA Corporation") == "Nvidia"


async def test_search_rejects_lookalikes_and_prefers_major_exchanges():
    searches = {
        "Shiba": [{"symbol": "SHIB-EUR", "exchange": "CCC", "quoteType": "CRYPTOCURRENCY", "shortname": "Shiba Inu EUR"}],
        "Wolfcoin": [{"symbol": "WOLFCOINX-USD", "exchange": "CCC", "quoteType": "CRYPTOCURRENCY",
                      "shortname": "WolfcoinX USD"}],
        "Ferrari": [{"symbol": "RACE.MI", "exchange": "JKT", "quoteType": "EQUITY", "longname": "Ferrari Indo"},
                    {"symbol": "RACE", "exchange": "NYQ", "quoteType": "EQUITY", "longname": "Ferrari N.V."}],
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(yahoo({}, searches))) as client:
        assert await resolve(client, "Shiba") == ("SHIB-EUR", "Shiba Inu EUR")
        assert await resolve(client, "Wolfcoin") is None
        assert await resolve(client, "Ferrari") == ("RACE", "Ferrari")


async def test_throttled_quote_falls_back_to_recent_price():
    good = yahoo({"ZZZ.DE": chart("ZZZ.DE", 12.5, RISING)})
    async with httpx.AsyncClient(transport=httpx.MockTransport(good)) as client:
        first = await quote(client, "ZZZ.DE", "Zett")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(429))) as client:
        second = await quote(client, "ZZZ.DE", "Zett")
    assert second["price"] == first["price"] == 12.5


def test_channel_overview_word_and_why_questions():
    assert market_request("Marktüberblick", channel=True)["overview"] is True
    why = market_request("Warum fällt Rheinmetall?", channel=True)
    assert why["news"] and why["why"] and why["assets"] == ["Rheinmetall"]
    assert market_request("Warum ist der Himmel blau?", channel=True) is None


@pytest.mark.parametrize(
    "question,channel,key",
    [("Top Flop DAX", False, "movers"), ("Wer sind die Gewinner heute?", True, "movers"),
     ("Wie ist die Stimmung bei Krypto?", False, "mood"), ("Angst und Gier", True, "mood"),
     ("Nachrichten zu SAP", False, "news"), ("News Rheinmetall", True, "news")],
)
def test_new_market_intents(question, channel, key):
    assert market_request(question, channel=channel).get(key) is True


def test_spark_layouts():
    from meshcore_ai_bot.markets import _spark_rows
    old = {"spark": {"result": [{"symbol": "SAP.DE", "response": [
        {"meta": {"regularMarketPrice": 110.0, "chartPreviousClose": 100.0}}]}]}}
    new = {"RHM.DE": {"close": [None, 90.0], "chartPreviousClose": 100.0}}
    assert _spark_rows(old) == [("SAP.DE", 110.0, 100.0)]
    assert _spark_rows(new) == [("RHM.DE", 90.0, 100.0)]


async def test_dax_movers_names_best_and_worst():
    from meshcore_ai_bot.markets import DAX, dax_movers

    def respond(request):
        symbols = request.url.params["symbols"].split(",")
        return httpx.Response(200, json={s: {"close": [100 + DAX.index(s)], "chartPreviousClose": 100.0}
                                         for s in symbols})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        text = (await dax_movers(client))["text"]
    assert text.startswith("DAX Top: Zalando +39,0 %, Vonovia +38,0 %, Volkswagen +37,0 %. Flop: Adidas +0,0 %")


async def test_crypto_falls_back_to_coingecko():
    def respond(request):
        if request.url.host.endswith("yahoo.com"):
            return httpx.Response(429)
        if request.url.path.endswith("/coins/markets"):
            return httpx.Response(200, json=[{"name": "Bitcoin", "current_price": 65000, "high_24h": 66000,
                                              "low_24h": 64000, "price_change_percentage_24h": -1.5}])
        if request.url.path.endswith("/market_chart"):
            start = int(NOW.timestamp() * 1000) - 364 * DAY * 1000
            return httpx.Response(200, json={"prices": [[start + i * DAY * 1000, 50000 + i * 50] for i in range(365)]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await market_answer(client, ["Bitcoin"], advice=False, tz="Europe/Berlin", now=NOW)
    assert result["text"].startswith("Bitcoin 65.000 € -1,5 %. Tag 64.000–66.000, 52W 50.000–68.200")
    assert "coingecko" in result["sources"][0]


async def test_currency_adds_ecb_reference_and_falls_back_to_it():
    from meshcore_ai_bot import markets

    def ecb(request):
        if request.url.path.endswith("/latest"):
            return httpx.Response(200, json={"rates": {"USD": 1.1214}})
        rates = {f"2026-{m:02d}-01": {"USD": 1.10 + m / 100} for m in range(1, 12)}
        return httpx.Response(200, json={"rates": rates})

    def yahoo_ok(request):
        if request.url.host.endswith("yahoo.com"):
            return httpx.Response(200, json=chart("EURUSD=X", 1.1153, [1.1 + i / 1000 for i in range(260)],
                                                  kind="CURRENCY", currency="USD"))
        return ecb(request)

    def yahoo_down(request):
        return httpx.Response(429) if request.url.host.endswith("yahoo.com") else ecb(request)

    markets._LAST.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(yahoo_ok)) as client:
        text = (await market_answer(client, ["Euro Dollar"], advice=False, tz="Europe/Berlin", now=NOW))["text"]
    assert "EZB-Referenzkurs 1,1214." in text
    markets._LAST.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(yahoo_down)) as client:
        text = (await market_answer(client, ["Euro Dollar"], advice=False, tz="Europe/Berlin", now=NOW))["text"]
    assert text.startswith("Euro in Dollar 1,2100")


def watch_setup(price_box):
    from meshcore_ai_bot.market_watch import MarketWatch

    def respond(request):
        if request.url.path.endswith("/RHM.DE"):
            return httpx.Response(200, json=chart("RHM.DE", price_box[0], RISING, change=1.0))
        return httpx.Response(404)

    config = Config(channels=[Channel("#börsenbot", "all", topic="boerse")])
    store = Store(":memory:")
    return config, store, respond, MarketWatch


async def test_price_alarm_set_list_fire_and_clear():
    from meshcore_ai_bot import markets
    from meshcore_ai_bot.protocol import allows_send

    markets._LAST.clear()
    price = [850.0]
    config, store, respond, MarketWatch = watch_setup(price)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        watch = MarketWatch(config, store, client)
        now = NOW.timestamp()
        reply = await watch.command("Marc", "#börsenbot", "Alarm Rheinmetall unter 800", now)
        assert reply.startswith("Kursalarm gesetzt: Rheinmetall unter 800,00 € (jetzt 850,00 €)")
        assert "steht schon" in await watch.command("Marc", "#börsenbot", "Alarm Rheinmetall über 840", now)
        assert await watch.command("Marc", "#börsenbot", "meine Alarme", now) == "Deine Alarme: Rheinmetall unter 800,00"
        await watch.check_alarms(now + 300)
        assert store.counts() == {}
        price[0] = 795.0
        markets._LAST.clear()
        await watch.check_alarms(now + 600)
        packet = store.claim(now + 601, lambda ch, stream: allows_send(config, ch, stream))
        assert packet["text"].startswith("@[Marc] Kursalarm: Rheinmetall steht bei 795,00 €, unter deiner Marke")
        assert watch.alarms() == []
        await watch.command("Marc", "#börsenbot", "Alarm SAP über 9999", now)  # unknown in mock: no alarm
        assert await watch.command("Marc", "#börsenbot", "Alarme löschen", now) == "Du hast keine Kursalarme."
    store.close()


async def test_alarm_limits():
    from meshcore_ai_bot import market_watch

    config, store, respond, MarketWatch = watch_setup([850.0])
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        watch = MarketWatch(config, store, client)
        for level in (700, 710, 720):
            await watch.command("Bob", "#börsenbot", f"Alarm Rheinmetall unter {level}", NOW.timestamp())
        assert "schon 3 Kursalarme" in await watch.command("Bob", "#börsenbot", "Alarm Rheinmetall unter 600")
        assert len(watch.alarms()) == market_watch.PER_SENDER
    store.close()


def test_alarm_commands_and_levels():
    from meshcore_ai_bot.market_watch import is_alarm_command, parse_level
    assert is_alarm_command("Alarm Rheinmetall unter 800")
    assert is_alarm_command("Kursalarm Bitcoin über 70.000")
    assert is_alarm_command("Alarme?") and is_alarm_command("Alarme löschen")
    assert not is_alarm_command("Feueralarm in Friedenau")
    assert parse_level("70.000") == 70000 and parse_level("1.234,5") == 1234.5 and parse_level("0,45") == 0.45


async def test_close_report_once_on_trading_days_only():
    from meshcore_ai_bot import markets
    from meshcore_ai_bot.market_watch import MarketWatch

    tuesday = datetime(2026, 11, 10, 16, 50, tzinfo=UTC).timestamp()  # 17:50 Berlin

    def respond(request):
        if request.url.path.startswith("/v8/finance/chart/"):
            symbol = request.url.path.rsplit("/", 1)[-1]
            data = chart(symbol, 100.0, RISING, kind="INDEX", change=0.5)
            data["chart"]["result"][0]["meta"]["regularMarketTime"] = int(tuesday) - 1800
            return httpx.Response(200, json=data)
        if request.url.path == "/v8/finance/spark":
            return httpx.Response(200, json={s: {"close": [101.0], "chartPreviousClose": 100.0}
                                             for s in request.url.params["symbols"].split(",")})
        return httpx.Response(503)

    markets._LAST.clear()
    config = Config(channels=[Channel("#börsenbot", "all", topic="boerse"), Channel("#fragdenbot", "all")])
    store = Store(":memory:")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        watch = MarketWatch(config, store, client)
        assert watch.report_due(tuesday)
        await watch.tick(tuesday)
        assert not watch.report_due(tuesday + 60)
        saturday = tuesday + 4 * 86400
        assert not watch.report_due(saturday)
    texts = []
    while packet := store.claim(tuesday + 1, lambda *_: True):
        texts.append((packet["channel"], packet["text"]))
        store.finish(packet["id"], "accepted", tuesday + 1)
    store.close()
    assert texts and all(ch == "#börsenbot" for ch, _ in texts)
    assert "Börsenschluss 10.11.: DAX 100" in texts[0][1] and "DAX Top:" in "".join(t for _, t in texts)
