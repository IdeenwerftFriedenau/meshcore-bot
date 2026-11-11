"""#börsenbot insider trades: BaFin directors' dealings, SEC Form 4, routing."""

from datetime import datetime

import httpx
import pytest

from meshcore_ai_bot import fundamentals, insider
from meshcore_ai_bot.fundamentals import _current
from meshcore_ai_bot.insider import form4, insider_trades, money, short_company
from meshcore_ai_bot.markets import market_request

HEAD = ("﻿Emittent;Bafin-ID;ISIN;Meldepflichtiger;Position / Status;Art des Instruments;Art des Geschäfts;"
        "Durchschnittspreis;Aggregiertes Volumen;Mitteilungsdatum;Datum des Geschäfts;Ort des Geschäfts;"
        "Datum der Aktivierung\n")
WEEK = HEAD + (
    "Baader Bank Aktiengesellschaft;1;DE0005088108;Riedel, Oliver;Vorstand;Aktie;Kauf;6,90 EUR;34.500,00 EUR;"
    "11.11.2026;10.11.2026;XETRA;11.11.2026 09:35:46\n"
    "Haier Smart Home Co., Ltd.;2;CNE1000048K8;Huang, Xiaowu;Sonstige Führungsperson;Aktie;Verkauf;21,44 HKD;"
    "9.428.800,00 HKD;10.11.2026;10.11.2026;HKEX;11.11.2026 06:47:31\n"
    "Uzin Utz SE;3;DE0007551509;Utz, Dr. Werner;Aufsichtsrat;Aktie;Kauf;62,79 EUR;28.254,00 EUR;"
    "10.11.2026;09.11.2026;Tradegate;11.11.2026 06:52:48\n"
    "Uzin Utz SE;3;DE0007551509;Utz, Dr. Werner;Aufsichtsrat;Aktie;Kauf;63,66 EUR;15.915,00 EUR;"
    "10.11.2026;09.11.2026;Tradegate;11.11.2026 06:52:48\n"
    "ZEAL Network SE;4;DE000ZAL1111;Muster, Eva;Vorstand;Aktie;Verkauf;40,00 EUR;1.200.000,00 EUR;"
    "07.11.2026;06.11.2026;XETRA;10.11.2026 07:00:00\n")
RHEINMETALL = HEAD + (
    "Rheinmetall Aktiengesellschaft;5;DE0007030009;Papperger, Armin Theodor;Vorstand;Aktie;Kauf;804,40 EUR;"
    "200.295,60 EUR;06.11.2026;06.11.2026;Tradegate;09.11.2026 07:08:32\n"
    "Rheinmetall Aktiengesellschaft;5;DE0007030009;Papperger, Armin Theodor;Vorstand;Aktie;Kauf;858,95 EUR;"
    "502.484,40 EUR;06.11.2026;05.11.2026;XETRA;06.11.2026 12:59:40\n"
    "Rheinmetall Aktiengesellschaft;5;DE0007030009;Georgi, Sara;In enger Beziehung;Aktie;Verkauf;700,00 EUR;"
    "70.000,00 EUR;03.03.2025;01.03.2025;XETRA;03.03.2025 12:00:00\n")

FORM4 = b"""<?xml version="1.0"?>
<ownershipDocument>
  <aff10b5One>1</aff10b5One>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>HUANG JEN HSUN</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>1</isDirector><isOfficer>1</isOfficer>
      <officerTitle>President and CEO</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-10-20</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>50000</value></transactionShares>
        <transactionPricePerShare><value>180.5</value></transactionPricePerShare></transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-10-20</value></transactionDate>
      <transactionCoding><transactionCode>F</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>900</value></transactionShares>
        <transactionPricePerShare><value>180</value></transactionPricePerShare></transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""


@pytest.fixture(autouse=True)
def contact(monkeypatch, tmp_path):
    monkeypatch.setenv("MESHCORE_SEC_CONTACT", "bot@example.org")
    monkeypatch.setattr(fundamentals, "ENV_FILE", str(tmp_path / "missing.env"))
    fundamentals._TICKERS = (0.0, {})


def transport(seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        url = str(request.url)
        if "bafin" in url:
            params = request.url.params
            body = RHEINMETALL if params.get("emittentIsin") == "DE0007030009" or "Rheinmetall" in params.get(
                "emittentName", "") else WEEK if params.get("zeitraum") == "1" else HEAD
            return httpx.Response(200, content=body.encode(), headers={"content-type": "text/csv"})
        if "company_tickers" in url:
            return httpx.Response(200, json={"0": {"ticker": "NVDA", "cik_str": 1045810}})
        if "submissions" in url:
            return httpx.Response(200, json={"filings": {"recent": {
                "form": ["8-K", "4", "4"], "accessionNumber": ["x", "0001-26-000001", "0001-26-000002"],
                "filingDate": ["2026-11-01", "2026-10-22", "2026-10-01"],
                "primaryDocument": ["a.htm", "xslF345X05/wk-form4_1.xml", "xslF345X05/wk-form4_2.xml"]}}})
        if "Archives" in url:
            return httpx.Response(200, content=FORM4)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def client(seen):
    return httpx.AsyncClient(transport=transport(seen))


def test_helpers():
    assert short_company("Rheinmetall Aktiengesellschaft") == "Rheinmetall"
    assert short_company("Haier Smart Home Co., Ltd.") == "Haier Smart Home"
    assert money(502484.4, "€") == "502 Tsd. €"
    assert money(1_200_000, "€") == "1,2 Mio. €"
    assert money(3450, "€") == "3.450 €"


@pytest.mark.asyncio
async def test_today_lists_biggest_euro_trades_first(monkeypatch):
    seen = []
    real = insider.today

    async def fixed(c):
        return await real(c, datetime(2026, 11, 11, 17, 0, tzinfo=insider.TZ))
    monkeypatch.setattr(insider, "today", fixed)
    async with client(seen) as c:
        result = await insider_trades(c, "")
    text = result["text"]
    assert text.startswith("Heute 3 Insider-Meldungen bei der BaFin (2 Käufe, 1 Verkauf)")
    assert text.index("Uzin Utz: Utz (Aufsichtsrat) Kauf 44 Tsd. €") < text.index("Baader Bank")
    assert text.index("Baader Bank") < text.index("Haier")  # euro first, then other currencies
    assert "ZEAL" not in text  # published yesterday
    assert ".." not in text


@pytest.mark.asyncio
async def test_today_falls_back_to_last_day():
    seen = []
    async with client(seen) as c:
        result = await insider.today(c, datetime(2026, 11, 15, 9, 0, tzinfo=insider.TZ))
    assert result["text"].startswith("Heute noch nichts; zuletzt am 11.11.")


@pytest.mark.asyncio
async def test_german_company_by_isin():
    seen = []

    async def isin_of(label):
        return "DE0007030009"
    async with client(seen) as c:
        result = await insider_trades(c, "Rheinmetall", isin_of)
    text = result["text"]
    assert text.startswith("Rheinmetall Insider (BaFin), zuletzt: Papperger (Vorstand) Kauf 200 Tsd. € am 06.11.;")
    assert "Georgi (nahestehend) Verkauf" in text
    assert "In 12 Monaten: 2 Käufe, 0 Verkäufe." in text  # the 2025 sale is older than a year
    assert any(r.url.params.get("emittentIsin") == "DE0007030009" for r in seen)


@pytest.mark.asyncio
async def test_us_company_from_form4():
    seen = []
    async with client(seen) as c:
        result = await insider_trades(c, "Nvidia")
    text = result["text"]
    assert text.startswith("Nvidia Insider (SEC Form 4), zuletzt: Huang (President and CEO) Verkauf 18,1 Mio. $ am 20.10. (Plan).")
    assert "(Plan)" in text and "Verkaufsplan" in text
    assert "Seit 01.10.2026: 0 Käufe, 1 Verkauf." in text  # both filings hold the same sale: merged
    assert all("bot@example.org" in r.headers["user-agent"] for r in seen if "sec.gov" in str(r.url))
    assert any(str(r.url).endswith("/wk-form4_1.xml") and "xslF345" not in str(r.url) for r in seen)


@pytest.mark.asyncio
async def test_us_company_without_contact(monkeypatch):
    monkeypatch.delenv("MESHCORE_SEC_CONTACT")
    seen = []
    async with client(seen) as c:
        result = await insider_trades(c, "Nvidia")
    assert "MESHCORE_SEC_CONTACT" in result["text"]
    assert not any("sec.gov" in str(r.url) for r in seen)


@pytest.mark.asyncio
async def test_no_insider_for_coins():
    async with client([]) as c:
        with pytest.raises(LookupError):
            await insider_trades(c, "Bitcoin")


def test_form4_skips_routine_codes():
    trades = form4(FORM4)
    assert [t["kind"] for t in trades] == ["Verkauf"]
    assert trades[0]["person"] == "Huang" and trades[0]["volume"] == 50000 * 180.5


@pytest.mark.parametrize("question,assets", [
    ("Insider Trades von heute", []),
    ("Insider-Käufe heute?", []),
    ("Welche Insider haben heute gekauft?", []),
    ("Insider Rheinmetall", ["Rheinmetall"]),
    ("Insidergeschäfte bei SAP", ["SAP"]),
    ("Hat der Chef von Rheinmetall Aktien gekauft?", ["Rheinmetall"]),
    ("Directors Dealings Siemens", ["Siemens"]),
    ("Kauft der Vorstand von Allianz?", ["Allianz"]),
])
def test_insider_questions_are_routed(question, assets):
    found = market_request(question, channel=True)
    assert found and found.get("insider") and found["assets"] == assets
    assert market_request(question) == found  # #fragdenbot too


@pytest.mark.parametrize("question", ["Wer ist Chef von SAP?", "KGV SAP", "Rheinmetall", "Insider-Tipp fürs Abendessen"])
def test_other_questions_stay(question):
    found = market_request(question, channel=False)
    assert not (found or {}).get("insider")


def test_newest_ceo_wins_without_end_dates():
    def ceo(item, start=None, rank="normal"):
        qualifiers = {"P580": [{"datavalue": {"value": {"time": start}}}]} if start else {}
        return {"rank": rank, "mainsnak": {"datavalue": {"value": {"id": item}}}, "qualifiers": qualifiers}
    claims = [ceo("Q_appel", "+2008-02-18T00:00:00Z"), ceo("Q_meyer", "+2023-05-04T00:00:00Z"), ceo("Q_old")]
    assert _current(claims)[0]["mainsnak"]["datavalue"]["value"]["id"] == "Q_meyer"
    claims.append(ceo("Q_pref", rank="preferred"))
    assert _current(claims)[0]["mainsnak"]["datavalue"]["value"]["id"] == "Q_pref"
