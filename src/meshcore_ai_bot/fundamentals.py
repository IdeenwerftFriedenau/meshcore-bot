"""Company key figures for the #börsenbot channel: valuation, size, dividend, revenue, profit, balance sheet.

Free sources without an API key, all meant for automated use:
- US companies: the SEC's XBRL API (data.sec.gov), straight from the 10-K/10-Q filings.
  KGV and market value use the live price from markets.py.
- Everyone else, e.g. DAX companies: Wikidata (yearly revenue, profit, staff, market value).
- Dividends of every share: the dividend events of the Yahoo chart endpoint markets.py already uses.
- Company profile (CEO, seat, industry, founding year, ISIN, website): Wikidata.
Figures change at most once a quarter, so every answer is kept for a day.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import date

import httpx
from dotenv import dotenv_values

from .http import read_json
from .markets import DISCLAIMER, _get, number, quote, resolve

AGENT = {"User-Agent": "IdeenwerftFriedenau meshcore-bot (+https://github.com/IdeenwerftFriedenau/meshcore-bot)",
         "Accept": "application/json"}
# The SEC answers only clients that name a contact e-mail (www.sec.gov/os/accessing-edgar-data).
# Set MESHCORE_SEC_CONTACT in the environment or in /etc/meshcore-ai-bot/.env; without it US firms use Wikidata.
CONTACT_ENV = "MESHCORE_SEC_CONTACT"
ENV_FILE = "/etc/meshcore-ai-bot/.env"
SEC = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/{taxonomy}/{concept}.json"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
WIKIDATA = "https://www.wikidata.org/w/api.php"
DAY = 24 * 3600

# Frankfurt listings in markets.py -> US home ticker, whose filings sit at the SEC.
HOME = {
    "APC.DE": "AAPL", "MSF.DE": "MSFT", "NVD.DE": "NVDA", "AMZ.DE": "AMZN", "ABEA.DE": "GOOGL",
    "FB2A.DE": "META", "TL0.DE": "TSLA", "NFC.DE": "NFLX", "AMD.DE": "AMD", "PTX.DE": "PLTR",
    "CCC3.DE": "KO", "1YD.DE": "AVGO", "2PP.DE": "PYPL", "BRYN.DE": "BRK-B",
}
# German and Dutch companies that file their annual report (20-F, IFRS) with the SEC as well:
# fresher than Wikidata. Their figures are per ordinary share, so price and KGV use the home listing.
ADR = {"SAP.DE": "SAP", "DBK.DE": "DB", "FME.DE": "FMS", "ASME.DE": "ASML"}
IFRS = {
    "revenue": ("Revenue", "RevenueFromContractsWithCustomers"),
    "profit": ("ProfitLossAttributableToOwnersOfParent", "ProfitLoss"),
    "eps": ("DilutedEarningsLossPerShare", "BasicEarningsLossPerShare"),
    "cash": ("CashAndCashEquivalents",),
}
SIGNS = {"USD": "$", "EUR": "€", "GBP": "£", "CHF": "CHF", "JPY": "Yen"}
# Earnings per share of these refer to another share class: no KGV from them.
NO_PE = {"BRK-B", "BRK-A"}

REVENUE = ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
           "RevenueFromContractWithCustomerIncludingAssessedTax")
DEBT = ("LongTermDebt", "LongTermDebtNoncurrent")
CASH = ("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents")

log = logging.getLogger(__name__)
UNITS = {"Q4916": "€", "Q4917": "$", "Q25224": "£", "Q25344": "CHF", "Q8146": "Yen"}

_CACHE: dict[str, tuple[float, dict | None]] = {}
_TICKERS: tuple[float, dict[str, int]] = (0.0, {})


def sec_contact() -> str:
    contact = os.environ.get(CONTACT_ENV, "")
    if not contact:
        try:
            contact = dotenv_values(ENV_FILE).get(CONTACT_ENV) or ""
        except OSError:
            contact = ""
    contact = contact.strip()
    return contact if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", contact) else ""


async def _json(client: httpx.AsyncClient, url: str, params: dict | None = None):
    headers = AGENT
    if "sec.gov" in url:
        headers = {**AGENT, "User-Agent": f"IdeenwerftFriedenau meshcore-bot {sec_contact()}"}
    return await read_json(client, "GET", url, params=params, headers=headers, timeout=12, limit=4_000_000)


def _cached(key: str) -> tuple[bool, dict | None]:
    known = _CACHE.get(key)
    if known and time.monotonic() - known[0] < DAY:
        return True, known[1]
    return False, None


def _keep(key: str, value: dict | None) -> None:
    if len(_CACHE) > 300:
        _CACHE.clear()
    _CACHE[key] = (time.monotonic(), value)


# ---------------------------------------------------------------- SEC (US filings)

async def cik(client: httpx.AsyncClient, ticker: str) -> int | None:
    global _TICKERS
    if not _TICKERS[1] or time.monotonic() - _TICKERS[0] > 7 * DAY:
        rows = await _json(client, SEC_TICKERS)
        table = {str(r["ticker"]).upper(): int(r["cik_str"]) for r in (rows or {}).values()
                 if isinstance(r, dict) and r.get("ticker") and r.get("cik_str")}
        if table:
            _TICKERS = (time.monotonic(), table)
    return _TICKERS[1].get(ticker.upper()) or _TICKERS[1].get(ticker.upper().replace("-", "."))


def _days(row: dict) -> int | None:
    try:
        return (date.fromisoformat(row["end"]) - date.fromisoformat(row["start"])).days
    except (KeyError, ValueError, TypeError):
        return None


def _rows(data: dict, unit: str) -> list[dict]:
    rows = [r for r in ((data.get("units") or {}).get(unit) or [])
            if isinstance(r, dict) and isinstance(r.get("val"), int | float) and r.get("end")
            and str(r.get("form", "")).startswith(("10-K", "10-Q", "20-F", "40-F"))]
    # Later filings restate earlier periods: the last filed value of a period wins.
    best: dict[tuple, dict] = {}
    for row in sorted(rows, key=lambda r: r.get("filed", "")):
        best[(row.get("start"), row["end"])] = row
    return sorted(best.values(), key=lambda r: r["end"])


def trailing(rows: list[dict]) -> tuple[float, str] | None:
    """Last twelve months of a flow item: last fiscal year + this year so far - same span a year earlier."""
    years = [r for r in rows if (_days(r) or 0) in range(350, 380)]
    if not years:
        return None
    year = years[-1]
    later = [r for r in rows if r["end"] > year["end"] and r.get("start") and (_days(r) or 0) in range(80, 300)
             and r["start"] <= _shift(year["end"], 10)]
    if later:
        now = max(later, key=lambda r: (r["end"], _days(r) or 0))
        span = _days(now) or 0
        before = [r for r in rows if r.get("start") and abs(_gap(r["end"], now["end"]) - 365) <= 10
                  and abs((_days(r) or 0) - span) <= 10]
        if before:
            return year["val"] + now["val"] - before[-1]["val"], now["end"]
    return float(year["val"]), year["end"]


def growth(rows: list[dict]) -> float | None:
    """Change in percent: this year so far against the same span a year earlier, else year on year."""
    spans = [r for r in rows if r.get("start") and (_days(r) or 0) in range(80, 380)]
    for row in sorted(spans, key=lambda r: (r["end"], _days(r) or 0), reverse=True):
        span = _days(row) or 0
        before = [r for r in spans if abs(_gap(r["end"], row["end"]) - 365) <= 10
                  and abs((_days(r) or 0) - span) <= 10 and r["val"]]
        if before and before[-1]["val"] > 0:
            return (row["val"] / before[-1]["val"] - 1) * 100
        break
    return None


def trailing_growth(rows: list[dict]) -> tuple[float, str, float | None] | None:
    found = trailing(rows)
    return (*found, growth(rows)) if found else None


def _shift(day: str, days: int) -> str:
    return date.fromordinal(date.fromisoformat(day).toordinal() + days).isoformat()


def _gap(earlier: str, later: str) -> int:
    return (date.fromisoformat(later) - date.fromisoformat(earlier)).days


def latest(rows: list[dict]) -> tuple[float, str] | None:
    """Newest value of a balance sheet item (summed over share classes on the same day)."""
    if not rows:
        return None
    end = rows[-1]["end"]
    return float(sum(r["val"] for r in rows if r["end"] == end)), end


async def concept(client: httpx.AsyncClient, number_: int, name: str, unit: str,
                  taxonomy: str = "us-gaap") -> list[dict]:
    try:
        data = await _json(client, SEC.format(cik=number_, taxonomy=taxonomy, concept=name))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return []
        raise
    if unit.startswith("*"):  # any currency, e.g. '*' -> EUR, '*/shares' -> EUR/shares
        tail = unit[1:]
        units = [u for u in (data.get("units") or {}) if re.fullmatch(r"[A-Z]{3}" + re.escape(tail), u)]
        unit = units[0] if units else unit
    return _rows(data, unit)


async def first(client, number_, names, unit, pick, taxonomy: str = "us-gaap"):
    """The concept name with data for the newest period; all names are asked at once."""
    found = None
    results = await asyncio.gather(*(concept(client, number_, name, unit, taxonomy) for name in names))
    for rows in results:
        value = pick(rows)
        if value and (not found or value[1] > found[1]):
            found = value
    return found


async def _currency(client, number_) -> str:
    """Reporting currency of an IFRS filer, read from its revenue concept."""
    for name in IFRS["revenue"]:
        try:
            data = await _json(client, SEC.format(cik=number_, taxonomy="ifrs-full", concept=name))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                continue
            raise
        units = [u for u in (data.get("units") or {}) if re.fullmatch(r"[A-Z]{3}", u)]
        if units:
            return units[0]
    return ""


async def ifrs_figures(client: httpx.AsyncClient, number_: int) -> dict | None:
    """Annual 20-F figures of a foreign filer in its own currency (no quarterly XBRL for those)."""
    code = await _currency(client, number_)
    if not code:
        return None
    revenue, profit, eps, cash, shares = await asyncio.gather(
        first(client, number_, IFRS["revenue"], code, trailing_growth, "ifrs-full"),
        first(client, number_, IFRS["profit"], code, trailing, "ifrs-full"),
        first(client, number_, IFRS["eps"], f"{code}/shares", trailing, "ifrs-full"),
        first(client, number_, IFRS["cash"], code, latest, "ifrs-full"),
        concept(client, number_, "EntityCommonStockSharesOutstanding", "shares", "dei"),
    )
    if not revenue and not profit:
        return None
    return {"source": "sec", "annual": True, "cik": number_, "currency": SIGNS.get(code, code), "code": code,
            "revenue": revenue, "profit": profit, "eps": eps, "dividend": None, "cash": cash, "debt": None,
            "shares": latest(shares)}


async def sec_figures(client: httpx.AsyncClient, ticker: str) -> dict | None:
    number_ = await cik(client, ticker)
    if not number_:
        return None
    revenue, profit = await asyncio.gather(first(client, number_, REVENUE, "USD", trailing_growth),
                                           first(client, number_, ("NetIncomeLoss",), "USD", trailing))
    if not revenue and not profit:
        return await ifrs_figures(client, number_)  # foreign filer (20-F, IFRS) or a fund
    eps, dividend, cash, debt, shares = await asyncio.gather(
        first(client, number_, ("EarningsPerShareDiluted", "EarningsPerShareBasic"), "USD/shares", trailing),
        first(client, number_, ("CommonStockDividendsPerShareDeclared",), "USD/shares", trailing),
        first(client, number_, CASH, "USD", latest),
        first(client, number_, DEBT, "USD", latest),
        concept(client, number_, "EntityCommonStockSharesOutstanding", "shares", "dei"),
    )
    return {"source": "sec", "cik": number_, "currency": "$", "code": "USD", "revenue": revenue, "profit": profit,
            "eps": eps, "dividend": dividend, "cash": cash, "debt": debt, "shares": latest(shares)}


# ---------------------------------------------------------------- Wikidata (everyone else)

def _amount(claim: dict) -> tuple[float, str, str] | None:
    """(value, currency or '', year) of one statement; deprecated ones never count."""
    if claim.get("rank") == "deprecated":
        return None
    value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}
    try:
        amount = float(value["amount"])
    except (KeyError, TypeError, ValueError):
        return None
    unit = UNITS.get(str(value.get("unit", "")).rsplit("/", 1)[-1], "")
    when = ""
    for snak in (claim.get("qualifiers") or {}).get("P585") or []:
        when = str(((snak.get("datavalue") or {}).get("value") or {}).get("time", ""))[1:5]
    return amount, unit, when


def newest(claims: dict, prop: str, *, money: bool = True) -> tuple[float, str, str] | None:
    found = [a for a in map(_amount, claims.get(prop) or []) if a and (a[1] or not money) and a[2]]
    if not found:
        return None
    year = max(a[2] for a in found)
    preferred = [c for c in claims.get(prop) or [] if c.get("rank") == "preferred"]
    for claim in preferred:
        best = _amount(claim)
        if best and best[2] == year and (best[1] or not money):
            return best
    return max(found, key=lambda a: a[2])


async def wikidata_figures(client: httpx.AsyncClient, label: str) -> dict | None:
    hits = await _json(client, WIKIDATA, {"action": "wbsearchentities", "search": label, "language": "de",
                                          "uselang": "de", "type": "item", "limit": 5, "format": "json"})
    ids = [h["id"] for h in (hits or {}).get("search") or [] if h.get("id")]
    if not ids:
        return None
    data = await _json(client, WIKIDATA, {"action": "wbgetentities", "ids": "|".join(ids), "props": "claims",
                                          "format": "json"})
    entities = (data or {}).get("entities") or {}
    # Listed companies (stock exchange or ISIN) before namesakes, otherwise search order.
    ids.sort(key=lambda i: not ({"P414", "P946"} & set((entities.get(i) or {}).get("claims") or {})))
    for item in ids:
        claims = (entities.get(item) or {}).get("claims") or {}
        figures = {"revenue": newest(claims, "P2139"), "profit": newest(claims, "P2295"),
                   "staff": newest(claims, "P1128", money=False), "market_cap": newest(claims, "P2226")}
        if figures["revenue"]:
            figures["revenue_before"] = previous(claims, "P2139", figures["revenue"])
        if figures["revenue"] or figures["profit"] or {"P414", "P946"} & set(claims):
            return {"source": "wikidata", "item": item, "profile": await profile(client, claims), **figures}
    return None


def previous(claims: dict, prop: str, now: tuple[float, str, str]) -> float | None:
    """The value of the year before 'now' in the same currency, for a growth rate."""
    year = str(int(now[2]) - 1)
    values = [a for a in map(_amount, claims.get(prop) or []) if a and a[2] == year and a[1] == now[1]]
    return values[0][0] if values else None


def _item(claim: dict) -> str:
    value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
    return value.get("id", "") if isinstance(value, dict) else ""


def _text(claim: dict) -> str:
    value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
    if isinstance(value, dict) and "time" in value:
        return str(value["time"])[1:5]
    return value if isinstance(value, str) else ""


def _current(claims: list[dict]) -> list[dict]:
    """Statements still valid: preferred first, none with an end date (P582)."""
    alive = [c for c in claims if c.get("rank") != "deprecated" and not (c.get("qualifiers") or {}).get("P582")]
    return sorted(alive, key=lambda c: c.get("rank") != "preferred")


async def profile(client: httpx.AsyncClient, claims: dict) -> dict:
    """CEO, seat, industry, founding year, ISIN and website; item names in German where known."""
    found: dict[str, str] = {}
    refs = {}
    for prop, key in (("P169", "ceo"), ("P159", "seat"), ("P452", "industry")):
        current = _current(claims.get(prop) or [])
        if current and _item(current[0]):
            refs[key] = _item(current[0])
    for prop, key in (("P571", "founded"), ("P946", "isin"), ("P856", "website")):
        current = _current(claims.get(prop) or [])
        if current and _text(current[0]):
            found[key] = _text(current[0])
    if refs:
        try:
            data = await _json(client, WIKIDATA, {"action": "wbgetentities", "ids": "|".join(sorted(set(refs.values()))),
                                                  "props": "labels", "languages": "de|en", "format": "json"})
        except (httpx.HTTPError, ValueError):
            data = {}
        entities = (data or {}).get("entities") or {}
        for key, item in refs.items():
            labels = (entities.get(item) or {}).get("labels") or {}
            name = (labels.get("de") or labels.get("en") or {}).get("value")
            if name:
                found[key] = name
    return found


# ---------------------------------------------------------------- dividends (Yahoo chart events)

async def dividends(client: httpx.AsyncClient, symbol: str) -> dict | None:
    """Dividends paid in the last twelve months: sum, yield, the last payment and its ex-date."""
    hit, known = _cached("div:" + symbol)
    if hit:
        return known
    try:
        data = await _get(client, f"/v8/finance/chart/{symbol}", {"range": "2y", "interval": "1mo", "events": "div"})
        result = ((data.get("chart") or {}).get("result") or [None])[0] or {}
    except (httpx.HTTPError, ValueError, AttributeError):
        return None  # not kept: the next question tries again
    meta = result.get("meta") or {}
    events = ((result.get("events") or {}).get("dividends") or {}).values()
    paid = sorted((int(e["date"]), float(e["amount"])) for e in events
                  if isinstance(e, dict) and isinstance(e.get("amount"), int | float) and e.get("date"))
    found = None
    price = meta.get("regularMarketPrice")
    if paid and isinstance(price, int | float) and price > 0:
        recent = [a for t, a in paid if t >= time.time() - 366 * 86400]
        found = {"sum": sum(recent), "yield": sum(recent) / price * 100 if recent else 0.0,
                 "last": paid[-1][1], "last_date": date.fromtimestamp(paid[-1][0]).strftime("%d.%m.%Y"),
                 "count": len(recent), "currency": meta.get("currency") or ""}
    elif isinstance(price, int | float):
        found = {"sum": 0.0, "yield": 0.0, "last": None, "count": 0, "currency": meta.get("currency") or ""}
    _keep("div:" + symbol, found)
    return found


def dividend_text(d: dict | None, *, short: bool = False) -> str:
    if not d:
        return ""
    if not d["count"]:
        return "keine Dividende in den letzten 12 Monaten"
    cur = {"EUR": "€", "USD": "$", "GBp": "Pence", "GBP": "£", "CHF": "CHF"}.get(d["currency"], d["currency"])
    rate = f"{ratio(d['yield'])} %" if d["yield"] >= 0.1 else "unter 0,1 %"
    total = f"{d['sum']:.2f}".replace(".", ",")
    text = f"Dividende {total} {cur}/Aktie ({rate})"
    if short:
        return text
    if d["count"] > 1:
        text += f", zuletzt {d['last']:.2f}".replace(".", ",") + f" {cur} am {d['last_date']}"
    else:
        text += f", gezahlt am {d['last_date']}"
    return text


# ---------------------------------------------------------------- text

def money(value: float, currency: str) -> str:
    """1.23e11 -> '123 Mrd. $', 4.5e9 -> '4,5 Mrd. €', 8.1e8 -> '810 Mio. €'."""
    sign = "-" if value < 0 else ""
    value = abs(value)
    for size, word in ((1e12, "Bio."), (1e9, "Mrd."), (1e6, "Mio.")):
        if value >= size:
            short = value / size
            text = f"{short:.0f}" if short >= 100 else f"{short:.1f}".replace(".", ",").removesuffix(",0")
            return f"{sign}{text} {word} {currency}".strip()
    return f"{sign}{number(value)} {currency}".strip()


def ratio(value: float) -> str:
    return f"{value:.1f}".replace(".", ",").removesuffix(",0")


def months(end: str) -> str:
    """'2025-09-27' -> '09/2025'."""
    return f"{end[5:7]}/{end[:4]}"


def change(value: float | None) -> str:
    if value is None or abs(value) > 1000:
        return ""
    rounded = round(value)
    return f" ({rounded:+d} %)" if rounded else " (unverändert)"


def sec_line(label: str, f: dict, price: float | None, paid: dict | None = None, *, short: bool = False) -> str:
    parts, cur = [], f["currency"]
    if price and f["shares"]:
        parts.append("Börsenwert " + money(price * f["shares"][0], cur))
    if price and f["eps"] and f["eps"][0] > 0 and f.get("ticker") not in NO_PE:
        pe = price / f["eps"][0]
        if pe < 1000:
            parts.append("KGV " + ratio(pe))
    elif f["eps"] and f["eps"][0] < 0:
        parts.append("KGV entfällt (Verlust)")
    elif price and f["shares"] and f["profit"] and f["profit"][0] > 0 and f.get("ticker") not in NO_PE:
        pe = price * f["shares"][0] / f["profit"][0]  # no earnings per share filed: market value / profit
        if pe < 1000:
            parts.append("KGV " + ratio(pe))
    if f["revenue"]:
        parts.append("Umsatz " + money(f["revenue"][0], cur) + change(f["revenue"][2] if len(f["revenue"]) > 2 else None))
    if f["profit"]:
        word = "Gewinn" if f["profit"][0] >= 0 else "Verlust"
        text = f"{word} {money(abs(f['profit'][0]), cur)}"
        if f["revenue"] and f["revenue"][0] > 0 and f["profit"][0] > 0 and not short:
            text += f" (Marge {ratio(f['profit'][0] / f['revenue'][0] * 100)} %)"
        parts.append(text)
    if paid and paid.get("currency") == f.get("code", "USD"):
        if paid["count"]:
            parts.append(dividend_text(paid, short=short))
    elif f["dividend"] and f["dividend"][0] > 0:
        per_share = f"{f['dividend'][0]:.2f}".replace(".", ",")
        text = f"Dividende {per_share} {cur}/Aktie"
        if price:
            rate = f["dividend"][0] / price * 100
            text += f" ({ratio(rate)} %)" if rate >= 0.1 else " (unter 0,1 %)"
        parts.append(text)
    balance = []
    if f["cash"] and not short:
        balance.append("Cash " + money(f["cash"][0], cur))
    if f["debt"] and not short:
        balance.append("Schulden " + money(f["debt"][0], cur))
    if balance:
        parts.append(" / ".join(balance))
    if not parts:
        return ""
    end = (f["revenue"] or f["profit"])[1]
    span = "Geschäftsjahr" if f.get("annual") else "12 Monate"
    if short:
        return f"{label} ({span} bis {months(end)}): " + ", ".join(parts) + "."
    span = "Geschäftsjahr" if f.get("annual") else "Letzte 12 Monate"
    report = "Jahresbericht bei der SEC" if f.get("annual") else "SEC-Berichte"
    return f"{label}: " + ", ".join(parts) + f". {span} bis {months(end)}, {report}."


def wikidata_line(label: str, f: dict, paid: dict | None = None, *, short: bool = False) -> str:
    parts = []
    cap, profit = f["market_cap"], f["profit"]
    fresh_cap = cap and cap[2] >= str(date.today().year - 1)
    if fresh_cap:
        parts.append(f"Börsenwert {money(cap[0], cap[1])} ({cap[2]})")
    pe = False
    if fresh_cap and profit and profit[0] > 0 and profit[1] == cap[1] and cap[0] / profit[0] < 1000:
        parts.append(f"KGV ca. {ratio(cap[0] / profit[0])}")
        pe = True
    for key, word in (("revenue", "Umsatz"), ("profit", "Gewinn")):
        if f[key]:
            value, cur, year = f[key]
            if key == "profit" and value < 0:
                word, value = "Verlust", -value
            text = f"{word} {money(value, cur)} ({year}"
            before = f.get("revenue_before") if key == "revenue" and not short else None
            if before and before > 0:
                text += change((value / before - 1) * 100).replace(" (", ", ", 1).removesuffix(")")
            parts.append(text + ")")
    if paid:
        parts.append(dividend_text(paid, short=short))
    if f["staff"] and not short:
        parts.append(f"{number(f['staff'][0])} Mitarbeitende ({f['staff'][2]})")
    if not parts or not (f["revenue"] or profit or paid):
        return ""
    if short:
        return f"{label}: " + ", ".join(parts) + "."
    tail = "Jahreszahlen laut Wikidata" + ("." if pe else ", ein aktuelles KGV habe ich dafür nicht.")
    return f"{label}: " + ", ".join(parts) + ". " + tail


def profile_line(label: str, p: dict) -> str:
    parts = []
    if p.get("ceo"):
        parts.append("Chef " + p["ceo"])
    if p.get("seat"):
        parts.append("Sitz " + p["seat"])
    if p.get("industry"):
        parts.append("Branche " + p["industry"])
    if p.get("founded"):
        parts.append("gegründet " + p["founded"])
    if p.get("isin"):
        parts.append("ISIN " + p["isin"])
    if p.get("website"):
        parts.append(p["website"].removeprefix("https://").removeprefix("http://").removeprefix("www.").rstrip("/"))
    return f"{label}: " + ", ".join(parts) + "." if parts else ""


# ---------------------------------------------------------------- entry point

async def wiki(client: httpx.AsyncClient, label: str) -> dict | None:
    key = "wd:" + label.casefold()
    hit, figures = _cached(key)
    if not hit:
        figures = await wikidata_figures(client, label)
        _keep(key, figures)
    return figures


async def company(client: httpx.AsyncClient, symbol: str, label: str, *,
                  about: bool = False, short: bool = False) -> tuple[str, str] | None:
    """(text, source URL) for one resolved asset, or None. about=True: the company profile instead."""
    home = HOME.get(symbol, symbol)
    if about:
        figures = await wiki(client, label)
        text = profile_line(label, (figures or {}).get("profile") or {})
        return (text, f"https://www.wikidata.org/wiki/{figures['item']}") if text else None
    paid = await dividends(client, home)
    filer = ADR.get(symbol) or (home if re.fullmatch(r"[A-Z]{1,5}(-[A-Z])?", home) else "")
    if filer and sec_contact():  # filings at the SEC: US companies and a few 20-F filers
        hit, figures = _cached("sec:" + filer)
        if not hit:
            try:
                figures = await sec_figures(client, filer)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                log.warning("SEC figures for %s failed: %s", filer, exc)
                figures = None  # Wikidata below still answers; the next question tries the SEC again
            else:
                _keep("sec:" + filer, figures)
        if figures:
            figures = {**figures, "ticker": filer}
            price = None
            try:
                data = await quote(client, home, label)  # home listing: ordinary shares in the report currency
                if data.get("currency") == figures.get("code", "USD"):
                    price = data["price"]
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                price = None
            text = sec_line(label, figures, price, paid, short=short)
            if text:
                url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={figures['cik']:010d}"
                return text, url
    figures = await wiki(client, label)
    if figures:
        text = wikidata_line(label, figures, paid, short=short)
        if text:
            return text, f"https://www.wikidata.org/wiki/{figures['item']}"
    if paid and paid["count"]:
        return f"{label}: {dividend_text(paid, short=short)}.", f"https://finance.yahoo.com/quote/{home}/history"
    return None


async def fundamentals(client: httpx.AsyncClient, assets: list[str], *, about: bool = False) -> dict:
    """Key figures (or with about=True the profile) for up to two assets; LookupError when nothing is known."""
    lines, sources, missing = [], [], []
    for name in assets[:2]:
        resolved = await resolve(client, name)
        if not resolved or resolved[0].startswith("^") or resolved[0].endswith(("-EUR", "=X", "=F")):
            missing.append(resolved[1] if resolved else name)
            continue
        found = await company(client, *resolved, about=about, short=len(assets) > 1)
        if not found:
            missing.append(resolved[1])
            continue
        lines.append(found[0])
        sources.append(found[1])
    if not lines:
        raise LookupError(", ".join(missing) or "nothing")
    text = " ".join(lines)
    if len(lines) > 1 and not about:
        named = ["SEC-Berichte" if "sec.gov" in u else "Wikidata" if "wikidata" in u else "Yahoo" for u in sources]
        text += f" Quellen: {', '.join(dict.fromkeys(named))}."
    text += "" if about else f" ({DISCLAIMER})"
    if missing:
        text += (" Kein Profil zu: " if about else " Keine Kennzahlen zu: ") + ", ".join(missing) + "."
    return {"text": text, "sources": sources}
