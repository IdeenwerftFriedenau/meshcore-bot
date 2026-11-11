"""#börsenbot key figures: SEC filings for US companies, Wikidata for the rest, routing and caching."""

import httpx
import pytest

from meshcore_ai_bot import fundamentals, markets
from meshcore_ai_bot.fundamentals import fundamentals as key_figures
from meshcore_ai_bot.fundamentals import money, trailing
from meshcore_ai_bot.markets import market_request
from tests.test_markets import RISING, chart


@pytest.fixture(autouse=True)
def fresh_caches():
    fundamentals._CACHE.clear()
    fundamentals._TICKERS = (0.0, {})
    markets._LAST.clear()
    yield
    fundamentals._CACHE.clear()
    markets._LAST.clear()


def row(start, end, val, form="10-Q", filed=None):
    return {"start": start, "end": end, "val": val, "form": form, "filed": filed or end, "fy": int(end[:4])}


def instant(end, val, form="10-Q"):
    return {"end": end, "val": val, "form": form, "filed": end}


NVDA = {
    "Revenues": {"USD": [
        row("2023-01-30", "2024-01-28", 60.9e9, "10-K"),
        row("2024-01-29", "2025-01-26", 130.5e9, "10-K"),
        row("2024-01-29", "2024-10-27", 91.2e9),
        row("2025-01-27", "2025-10-26", 147.8e9),
        row("2025-07-28", "2025-10-26", 57.0e9),  # the quarter alone must not replace the nine months
    ]},
    "NetIncomeLoss": {"USD": [
        row("2024-01-29", "2025-01-26", 72.9e9, "10-K"),
        row("2024-01-29", "2024-10-27", 50.8e9),
        row("2025-01-27", "2025-10-26", 86.6e9),
    ]},
    "EarningsPerShareDiluted": {"USD/shares": [
        row("2024-01-29", "2025-01-26", 2.94, "10-K"),
        row("2024-01-29", "2024-10-27", 2.04),
        row("2025-01-27", "2025-10-26", 3.51),
    ]},
    "CommonStockDividendsPerShareDeclared": {"USD/shares": [row("2024-01-29", "2025-01-26", 0.034, "10-K")]},
    "CashAndCashEquivalentsAtCarryingValue": {"USD": [instant("2025-01-26", 8.6e9, "10-K"),
                                                      instant("2025-10-26", 11.5e9)]},
    "LongTermDebt": {"USD": [instant("2025-10-26", 8.5e9)]},
    "EntityCommonStockSharesOutstanding": {"shares": [instant("2025-11-14", 24.3e9)]},
}

WIKI_SEARCH = {"search": [{"id": "Q1"}, {"id": "Q552581"}]}
WIKI_ENTITIES = {"entities": {
    "Q1": {"claims": {}},  # e.g. the band of the same name: no company figures
    "Q552581": {"claims": {
        "P2139": [
            {"rank": "normal", "mainsnak": {"datavalue": {"value": {
                "amount": "+31207000000", "unit": "http://www.wikidata.org/entity/Q4916"}}},
             "qualifiers": {"P585": [{"datavalue": {"value": {"time": "+2023-01-01T00:00:00Z"}}}]}},
            {"rank": "normal", "mainsnak": {"datavalue": {"value": {
                "amount": "+34176000000", "unit": "http://www.wikidata.org/entity/Q4916"}}},
             "qualifiers": {"P585": [{"datavalue": {"value": {"time": "+2024-01-01T00:00:00Z"}}}]}},
        ],
        "P2295": [{"rank": "normal", "mainsnak": {"datavalue": {"value": {
            "amount": "+3150000000", "unit": "http://www.wikidata.org/entity/Q4916"}}},
            "qualifiers": {"P585": [{"datavalue": {"value": {"time": "+2024-01-01T00:00:00Z"}}}]}}],
        "P1128": [{"rank": "normal", "mainsnak": {"datavalue": {"value": {"amount": "+109121", "unit": "1"}}},
                   "qualifiers": {"P585": [{"datavalue": {"value": {"time": "+2024-12-31T00:00:00Z"}}}]}}],
    }},
}}


def sources(seen=None, sec=NVDA, price=180.0):
    def respond(request):
        if seen is not None:
            seen.append(str(request.url))
        host, path = request.url.host, request.url.path
        assert "IdeenwerftFriedenau" in request.headers["user-agent"] or host.endswith("yahoo.com")
        if host == "www.sec.gov" and path == "/files/company_tickers.json":
            return httpx.Response(200, json={"0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
                                             "1": {"cik_str": 1, "ticker": "ASML", "title": "ASML"}})
        if host == "data.sec.gov":
            concept = path.rsplit("/", 1)[-1].removesuffix(".json")
            if "CIK0001045810" in path and concept in sec:
                return httpx.Response(200, json={"units": sec[concept]})
            return httpx.Response(404)
        if host == "www.wikidata.org":
            action = request.url.params["action"]
            return httpx.Response(200, json=WIKI_SEARCH if action == "wbsearchentities" else WIKI_ENTITIES)
        if path.startswith("/v8/finance/chart/NVDA"):
            return httpx.Response(200, json=chart("NVDA", price, RISING, currency="USD", name="NVIDIA"))
        return httpx.Response(404)

    return respond


def test_trailing_twelve_months_from_year_and_year_to_date():
    value, end = trailing(fundamentals._rows({"units": NVDA["Revenues"]}, "USD"))
    assert round(value / 1e9, 1) == 187.1  # 130,5 + 147,8 - 91,2
    assert end == "2025-10-26"


def test_trailing_falls_back_to_the_last_year():
    value, end = trailing(fundamentals._rows({"units": {"USD": [row("2024-01-01", "2024-12-31", 5e9, "10-K")]}}, "USD"))
    assert (value, end) == (5e9, "2024-12-31")


def test_restated_values_win():
    rows = [row("2024-01-01", "2024-12-31", 5e9, "10-K", "2025-02-01"),
            row("2024-01-01", "2024-12-31", 5.2e9, "10-K", "2026-02-01")]
    assert trailing(fundamentals._rows({"units": {"USD": rows}}, "USD"))[0] == 5.2e9


def test_money_is_short_and_german():
    assert money(187.1e9, "$") == "187 Mrd. $"
    assert money(34.176e9, "€") == "34,2 Mrd. €"
    assert money(4.37e12, "$") == "4,4 Bio. $"
    assert money(810e6, "€") == "810 Mio. €"
    assert money(2e9, "€") == "2 Mrd. €"


async def test_us_company_from_sec_filings_with_live_kgv():
    async with httpx.AsyncClient(transport=httpx.MockTransport(sources())) as client:
        result = await key_figures(client, ["Nvidia"])
    text = result["text"]
    assert text.startswith("Nvidia: Börsenwert 4,4 Bio. $")  # 180 $ x 24,3 Mrd. Aktien
    assert "KGV 40,8" in text  # 180 / (2,94 + 3,51 - 2,04)
    assert "Dividende 0,03 $/Aktie (unter 0,1 %)" in text
    assert "Umsatz 187 Mrd. $" in text and "Gewinn 109 Mrd. $ (Marge 58" in text
    assert "Cash 11,5 Mrd. $ / Schulden 8,5 Mrd. $" in text
    assert "Letzte 12 Monate bis 10/2025, SEC-Berichte" in text and "keine Anlageberatung" in text
    assert result["sources"][0].startswith("https://www.sec.gov/")


async def test_german_company_from_wikidata_with_year():
    async with httpx.AsyncClient(transport=httpx.MockTransport(sources())) as client:
        result = await key_figures(client, ["SAP"])
    text = result["text"]
    assert text.startswith("SAP: Umsatz 34,2 Mrd. € (2024), Gewinn 3,1 Mrd. € (2024), 109.121 Mitarbeitende (2024)")
    assert "Wikidata" in text and "KGV habe ich dafür nicht" in text
    assert result["sources"] == ["https://www.wikidata.org/wiki/Q552581"]


async def test_comparison_of_us_and_german_company():
    async with httpx.AsyncClient(transport=httpx.MockTransport(sources())) as client:
        result = await key_figures(client, ["Nvidia", "SAP"])
    assert result["text"].index("Nvidia:") < result["text"].index("SAP:")
    assert len(result["sources"]) == 2


async def test_foreign_filer_without_us_gaap_falls_back_to_wikidata():
    async with httpx.AsyncClient(transport=httpx.MockTransport(sources())) as client:
        result = await key_figures(client, ["ASML"])  # ASME.DE -> resolves, no home ticker: Wikidata
    assert "Wikidata" in result["text"]


async def test_figures_are_cached_for_a_day():
    seen: list[str] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(sources(seen))) as client:
        await key_figures(client, ["Nvidia"])
        first = len([u for u in seen if "sec.gov" in u])
        await key_figures(client, ["Nvidia"])
    assert first and len([u for u in seen if "sec.gov" in u]) == first


async def test_no_kgv_without_price_but_other_figures_stay():
    def respond(request):
        if request.url.host.endswith("yahoo.com"):
            return httpx.Response(503)
        return sources()(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        text = (await key_figures(client, ["Nvidia"]))["text"]
    assert "KGV" not in text and "Börsenwert" not in text and "Umsatz 187 Mrd. $" in text


async def test_loss_makers_have_no_kgv():
    loss = {**NVDA, "NetIncomeLoss": {"USD": [row("2024-01-01", "2024-12-31", -2e9, "10-K")]},
            "EarningsPerShareDiluted": {"USD/shares": [row("2024-01-01", "2024-12-31", -0.8, "10-K")]}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(sources(sec=loss))) as client:
        text = (await key_figures(client, ["Nvidia"]))["text"]
    assert "KGV entfällt (Verlust)" in text and "Verlust 2 Mrd. $" in text


async def test_indices_and_coins_have_no_key_figures():
    async with httpx.AsyncClient(transport=httpx.MockTransport(sources())) as client:
        with pytest.raises(LookupError):
            await key_figures(client, ["DAX"])


@pytest.mark.parametrize(
    "question,channel,expected",
    [
        ("KGV von SAP", True, ["SAP"]),
        ("Wie hoch ist der Umsatz von Nvidia?", False, ["Nvidia"]),
        ("Apple Dividende", True, ["Apple"]),
        ("Umsatz und Gewinn von Rheinmetall", True, ["Rheinmetall"]),
        ("Wie viel ist Apple wert", False, ["Apple"]),
        ("Vergleich KGV SAP und Siemens", True, ["SAP", "Siemens"]),
        ("Wie viele Mitarbeiter hat Siemens?", False, ["Siemens"]),
        ("Kennzahlen Hensoldt letztes Jahr", True, ["Hensoldt"]),
    ],
)
def test_key_figure_questions_are_routed(question, channel, expected):
    found = market_request(question, channel=channel)
    assert found and found.get("facts") and found["assets"] == expected


@pytest.mark.parametrize("question,channel", [
    ("Was ist ein KGV?", True), ("Was hat der Bäcker heute für Umsatz", False), ("Gewinner im DAX", True),
    ("Wieviel ist ein Bitcoin wert", True), ("Wie steht Rheinmetall?", True),
])
def test_other_questions_are_not_key_figures(question, channel):
    found = market_request(question, channel=channel)
    assert not (found and found.get("facts"))


async def test_channel_question_is_answered_from_the_data_without_the_model():
    from meshcore_ai_bot.assistant import Assistant
    from meshcore_ai_bot.config import Config
    from meshcore_ai_bot.store import Store

    config = Config()
    config.ai._api_key = "synthetic"
    called: list[str] = []
    data = sources()

    def respond(request):
        if request.url.host == "openrouter.ai":
            called.append("ai")
            return httpx.Response(200, json={"choices": [{"message": {"content": "Modell"}}]})
        return data(request)

    store = Store(":memory:")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        answer = await Assistant(config, store, client).answer("KGV von Nvidia?", topic="boerse")
        tools = await Assistant(config, store, client).tools.run("get_fundamentals", {"asset": "SAP"})
    store.close()
    assert answer.text.startswith("Nvidia: Börsenwert") and "KGV 40,8" in answer.text
    assert answer.sources and not called
    assert tools["text"].startswith("SAP: Umsatz")
