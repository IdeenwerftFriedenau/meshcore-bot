"""Company key figures for the #börsenbot channel: valuation, size, dividend, revenue, profit, balance sheet.

Two free sources without an API key, both meant for automated use:
- US companies: the SEC's XBRL API (data.sec.gov), straight from the 10-K/10-Q filings.
  KGV and market value use the live price from markets.py.
- Everyone else, e.g. DAX companies: Wikidata (yearly revenue, profit, staff, market value).
Figures change at most once a quarter, so every answer is kept for a day.
"""

from __future__ import annotations

import re
import time
from datetime import date

import httpx

from .http import read_json
from .markets import DISCLAIMER, number, quote, resolve

AGENT = {"User-Agent": "IdeenwerftFriedenau meshcore-bot (+https://github.com/IdeenwerftFriedenau/meshcore-bot)",
         "Accept": "application/json"}
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
# Earnings per share of these refer to another share class: no KGV from them.
NO_PE = {"BRK-B", "BRK-A"}

REVENUE = ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
           "RevenueFromContractWithCustomerIncludingAssessedTax")
DEBT = ("LongTermDebt", "LongTermDebtNoncurrent")
CASH = ("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents")

UNITS = {"Q4916": "€", "Q4917": "$", "Q25224": "£", "Q25344": "CHF", "Q8146": "Yen"}

_CACHE: dict[str, tuple[float, dict | None]] = {}
_TICKERS: tuple[float, dict[str, int]] = (0.0, {})


async def _json(client: httpx.AsyncClient, url: str, params: dict | None = None):
    return await read_json(client, "GET", url, params=params, headers=AGENT, timeout=12, limit=4_000_000)


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
    return _rows(data, unit)


async def first(client, number_, names, unit, pick):
    """The first concept name with data, preferring the one with the newest period."""
    found = None
    for name in names:
        value = pick(await concept(client, number_, name, unit))
        if value and (not found or value[1] > found[1]):
            found = value
    return found


async def sec_figures(client: httpx.AsyncClient, ticker: str) -> dict | None:
    number_ = await cik(client, ticker)
    if not number_:
        return None
    revenue = await first(client, number_, REVENUE, "USD", trailing)
    profit = await first(client, number_, ("NetIncomeLoss",), "USD", trailing)
    if not revenue and not profit:
        return None  # foreign filer (IFRS) or a fund: Wikidata knows more
    eps = await first(client, number_, ("EarningsPerShareDiluted", "EarningsPerShareBasic"), "USD/shares", trailing)
    dividend = await first(client, number_, ("CommonStockDividendsPerShareDeclared",), "USD/shares", trailing)
    cash = await first(client, number_, CASH, "USD", latest)
    debt = await first(client, number_, DEBT, "USD", latest)
    shares = latest(await concept(client, number_, "EntityCommonStockSharesOutstanding", "shares", "dei"))
    return {"source": "sec", "cik": number_, "currency": "$", "revenue": revenue, "profit": profit, "eps": eps,
            "dividend": dividend, "cash": cash, "debt": debt, "shares": shares}


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
        if figures["revenue"] or figures["profit"]:
            return {"source": "wikidata", "item": item, **figures}
    return None


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


def sec_line(label: str, f: dict, price: float | None) -> str:
    parts, cur = [], f["currency"]
    if price and f["shares"]:
        parts.append("Börsenwert " + money(price * f["shares"][0], cur))
    if price and f["eps"] and f["eps"][0] > 0 and f.get("ticker") not in NO_PE:
        pe = price / f["eps"][0]
        if pe < 1000:
            parts.append("KGV " + ratio(pe))
    elif f["eps"] and f["eps"][0] < 0:
        parts.append("KGV entfällt (Verlust)")
    if f["dividend"] and f["dividend"][0] > 0:
        per_share = f"{f['dividend'][0]:.2f}".replace(".", ",")
        text = f"Dividende {per_share} {cur}/Aktie"
        if price:
            rate = f["dividend"][0] / price * 100
            text += f" ({ratio(rate)} %)" if rate >= 0.1 else " (unter 0,1 %)"
        parts.append(text)
    if f["revenue"]:
        parts.append("Umsatz " + money(f["revenue"][0], cur))
    if f["profit"]:
        word = "Gewinn" if f["profit"][0] >= 0 else "Verlust"
        text = f"{word} {money(abs(f['profit'][0]), cur)}"
        if f["revenue"] and f["revenue"][0] > 0 and f["profit"][0] > 0:
            text += f" (Marge {ratio(f['profit'][0] / f['revenue'][0] * 100)} %)"
        parts.append(text)
    balance = []
    if f["cash"]:
        balance.append("Cash " + money(f["cash"][0], cur))
    if f["debt"]:
        balance.append("Schulden " + money(f["debt"][0], cur))
    if balance:
        parts.append(" / ".join(balance))
    if not parts:
        return ""
    end = (f["revenue"] or f["profit"])[1]
    return f"{label}: " + ", ".join(parts) + f". Letzte 12 Monate bis {months(end)}, SEC-Berichte."


def wikidata_line(label: str, f: dict) -> str:
    parts = []
    if f["market_cap"] and f["market_cap"][2] >= str(date.today().year - 1):
        parts.append(f"Börsenwert {money(f['market_cap'][0], f['market_cap'][1])} ({f['market_cap'][2]})")
    for key, word in (("revenue", "Umsatz"), ("profit", "Gewinn")):
        if f[key]:
            value, cur, year = f[key]
            if key == "profit" and value < 0:
                word, value = "Verlust", -value
            parts.append(f"{word} {money(value, cur)} ({year})")
    if f["staff"]:
        parts.append(f"{number(f['staff'][0])} Mitarbeitende ({f['staff'][2]})")
    if not parts:
        return ""
    return f"{label}: " + ", ".join(parts) + ". Jahreszahlen laut Wikidata, KGV habe ich dafür nicht."


# ---------------------------------------------------------------- entry point

async def company(client: httpx.AsyncClient, symbol: str, label: str) -> tuple[str, str] | None:
    """(text, source URL) for one resolved asset, or None."""
    home = HOME.get(symbol, symbol)
    if re.fullmatch(r"[A-Z]{1,5}(-[A-Z])?", home):  # a US ticker: filings at the SEC
        hit, figures = _cached("sec:" + home)
        if not hit:
            figures = await sec_figures(client, home)
            _keep("sec:" + home, figures)
        if figures:
            figures = {**figures, "ticker": home}
            price = None
            try:
                data = await quote(client, home, label)
                if data.get("currency") == "USD":
                    price = data["price"]
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                price = None
            text = sec_line(label, figures, price)
            if text:
                url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={figures['cik']:010d}"
                return text, url
    key = "wd:" + label.casefold()
    hit, figures = _cached(key)
    if not hit:
        figures = await wikidata_figures(client, label)
        _keep(key, figures)
    if figures:
        text = wikidata_line(label, figures)
        if text:
            return text, f"https://www.wikidata.org/wiki/{figures['item']}"
    return None


async def fundamentals(client: httpx.AsyncClient, assets: list[str]) -> dict:
    """Key figures for up to two assets; LookupError when nothing is known."""
    lines, sources, missing = [], [], []
    for name in assets[:2]:
        resolved = await resolve(client, name)
        if not resolved or resolved[0].startswith("^") or resolved[0].endswith(("-EUR", "=X", "=F")):
            missing.append(resolved[1] if resolved else name)
            continue
        found = await company(client, *resolved)
        if not found:
            missing.append(resolved[1])
            continue
        lines.append(found[0])
        sources.append(found[1])
    if not lines:
        raise LookupError(", ".join(missing) or "nothing")
    text = " ".join(lines) + f" ({DISCLAIMER})"
    if missing:
        text += " Keine Kennzahlen zu: " + ", ".join(missing) + "."
    return {"text": text, "sources": sources}
