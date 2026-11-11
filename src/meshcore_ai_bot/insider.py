"""Insider trades (directors' dealings) for the #börsenbot channel.

Free official sources meant for automated use:
- BaFin database of managers' transactions under Art. 19 MAR (portal.mvp.bafin.de), which offers a CSV export:
  every share traded in Germany, including the trades published today.
- US companies: the SEC's Form 4 filings (EDGAR), only with a contact address set (see fundamentals.sec_contact).
"""

from __future__ import annotations

import csv
import io
import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from .fundamentals import ADR, AGENT, HOME, cik, sec_contact
from .http import read_bytes, read_json
from .markets import resolve

BAFIN = "https://portal.mvp.bafin.de/database/DealingsInfo/sucheForm.do"
BAFIN_PAGE = "https://portal.mvp.bafin.de/database/DealingsInfo/"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
TZ = ZoneInfo("Europe/Berlin")
ROLES = {"vorstand": "Vorstand", "aufsichtsrat": "Aufsichtsrat", "sonstige führungsperson": "Führungskraft",
         "in enger beziehung": "nahestehend"}
# Form 4 transaction codes worth naming; awards, tax withholding and option exercises are routine.
CODES = {"P": "Kauf", "S": "Verkauf"}
SUFFIX = re.compile(r"[\s,]+(aktiengesellschaft|ag|se|kgaa|gmbh|co\.?|& co\.?|kg|n\.?v\.?|s\.?a\.?|plc|inc\.?|"
                    r"ltd\.?|limited|corp\.?|corporation|holding|holdings|group|vz\.?)\b\.?", re.I)

log = logging.getLogger(__name__)


def _number(text: str) -> float:
    """'502.484,40 EUR' -> 502484.4"""
    match = re.search(r"-?[\d.]+(?:,\d+)?", text or "")
    return float(match.group(0).replace(".", "").replace(",", ".")) if match else 0.0


def _currency(text: str) -> str:
    code = (text or "").strip().split(" ")[-1].upper()
    return {"EUR": "€", "USD": "$", "GBP": "£"}.get(code, code if code.isalpha() else "")


def money(value: float, sign: str) -> str:
    if value >= 1e9:
        text = f"{value / 1e9:.1f} Mrd.".replace(".", ",", 1)
    elif value >= 1e6:
        text = f"{value / 1e6:.1f} Mio.".replace(".", ",", 1)
    elif value >= 1e4:
        text = f"{value / 1e3:.0f} Tsd."
    else:
        text = f"{value:,.0f}".replace(",", ".")
    return f"{text.replace(',0 ', ' ')} {sign}".strip()


def short_company(name: str) -> str:
    name = re.sub(r"\s*\(.*?\)", "", name or "")
    for _ in range(3):
        name = SUFFIX.sub("", name).strip(" ,.")
    return name or "?"


def surname(person: str) -> str:
    return (person or "?").split(",")[0].strip() or "?"


def role(text: str) -> str:
    folded = (text or "").casefold()
    return next((v for k, v in ROLES.items() if k in folded), (text or "").strip())


def _day(text: str) -> date | None:
    try:
        return datetime.strptime(text.strip()[:10], "%d.%m.%Y").date()
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------- BaFin (Germany)

async def bafin(client: httpx.AsyncClient, *, isin: str = "", name: str = "", period: int = 1) -> list[dict]:
    """Managers' transactions from the BaFin CSV export, newest first. period: 0 all, 1 last week, 2 last month."""
    params = {"d-4000784-e": "1", "6578706f7274": "1", "emittentButton": "Suche Emittent", "zeitraum": str(period),
              "emittentName": name, "emittentIsin": isin, "meldepflichtigerName": "", "zeitraumVon": "",
              "zeitraumBis": ""}
    data = await read_bytes(client, "GET", BAFIN, params=params, headers=AGENT, timeout=20, limit=3_000_000)
    rows = []
    for row in csv.DictReader(io.StringIO(data.decode("utf-8-sig", "replace")), delimiter=";"):
        volume = row.get("Aggregiertes Volumen") or ""
        rows.append({
            "company": short_company(row.get("Emittent", "")), "isin": (row.get("ISIN") or "").strip(),
            "person": surname(row.get("Meldepflichtiger", "")), "role": role(row.get("Position / Status", "")),
            "instrument": (row.get("Art des Instruments") or "").strip(),
            "kind": (row.get("Art des Geschäfts") or "").strip(),
            "price": _number(row.get("Durchschnittspreis", "")), "volume": _number(volume),
            "sign": _currency(volume), "traded": _day(row.get("Datum des Geschäfts", "")),
            "published": _day(row.get("Datum der Aktivierung", "")),
        })
    return rows


def merge(rows: list[dict]) -> list[dict]:
    """One line per person, company, kind and day: BaFin lists partial fills one by one."""
    merged: dict[tuple, dict] = {}
    for row in rows:
        key = (row["company"], row["person"], row["kind"], row["traded"], row["sign"])
        if key in merged:
            merged[key]["volume"] += row["volume"]
        else:
            merged[key] = dict(row)
    return list(merged.values())


def kind(row: dict) -> str:
    text = row["kind"]
    folded = text.casefold()
    if "kauf" in folded and "verkauf" not in folded:
        return "Kauf"
    if "verkauf" in folded:
        return "Verkauf"
    return "sonstiges Geschäft" if folded.startswith("sonstig") else text.split(" ")[0][:20] or "Geschäft"


def trade_line(row: dict, *, company: bool = True, when: bool = True) -> str:
    parts = [row["company"] + ":"] if company else []
    parts.append(f"{row['person']} ({row['role']})" if row["role"] else row["person"])
    parts.append(kind(row))
    if row["volume"]:
        parts.append(money(row["volume"], row["sign"]))
    if when and row["traded"]:
        parts.append(f"am {row['traded']:%d.%m.}")
    return " ".join(parts)


def _counts(rows: list[dict]) -> str:
    buys = sum(kind(r) == "Kauf" for r in rows)
    sells = sum(kind(r) == "Verkauf" for r in rows)
    other = len(rows) - buys - sells
    parts = [f"{buys} {'Kauf' if buys == 1 else 'Käufe'}", f"{sells} {'Verkauf' if sells == 1 else 'Verkäufe'}"]
    if other:
        parts.append(f"{other} sonstige")
    return ", ".join(parts)


async def today(client: httpx.AsyncClient, now: datetime | None = None) -> dict:
    """Insider trades published by the BaFin today (or on the last day with any), biggest first."""
    now = now or datetime.now(TZ)
    rows = merge(await bafin(client, period=1))
    days = sorted({r["published"] for r in rows if r["published"]}, reverse=True)
    if not days:
        return {"text": "Die BaFin hat in der letzten Woche keine Insider-Geschäfte veröffentlicht.",
                "sources": [BAFIN_PAGE]}
    day = now.date() if now.date() in days else days[0]
    chosen = sorted((r for r in rows if r["published"] == day), key=lambda r: (r["sign"] == "€", r["volume"]), reverse=True)
    label = "Heute" if day == now.date() else f"Heute noch nichts; zuletzt am {day:%d.%m.}"
    head = f"{label} {len(chosen)} Insider-Meldungen bei der BaFin ({_counts(chosen)})"
    lines = [trade_line(r) for r in chosen[:4]]
    text = f"{head}, größte: " + "; ".join(lines).rstrip(".") + "."
    if len(chosen) > 4:
        text += f" Und {len(chosen) - 4} weitere."
    return {"text": text + " Für eine Firma frag z.B. „Insider Rheinmetall“.", "sources": [BAFIN_PAGE]}


async def german(client: httpx.AsyncClient, label: str, isin: str) -> dict | None:
    """The latest insider trades of one company at the BaFin, or None when it has none."""
    rows = merge(await bafin(client, isin=isin, name="" if isin else short_company(label), period=0))
    rows = [r for r in rows if r["traded"]]
    if not rows:
        return None
    rows.sort(key=lambda r: r["traded"], reverse=True)
    newest = rows[0]["traded"]
    year = [r for r in rows if (newest - r["traded"]).days <= 365]
    name = rows[0]["company"]
    # Purchases and sales say more than share plans and awards ('Sonstiges'): those first, newest first.
    shown = [r for r in rows if kind(r) in {"Kauf", "Verkauf"}][:4] or rows[:4]
    text = (f"{name} Insider (BaFin), zuletzt: " + "; ".join(trade_line(r, company=False) for r in shown).rstrip(".")
            + f". In 12 Monaten: {_counts(year)}.")
    return {"text": text, "sources": [BAFIN_PAGE]}


# ---------------------------------------------------------------- SEC Form 4 (US)

def _sec_headers() -> dict:
    return {**AGENT, "User-Agent": f"IdeenwerftFriedenau meshcore-bot {sec_contact()}"}


def _value(node: ET.Element | None, path: str) -> str:
    found = node.find(path) if node is not None else None
    return (found.text or "").strip() if found is not None and found.text else ""


def form4(xml: bytes) -> list[dict]:
    """Open-market purchases and sales from one Form 4 document."""
    root = ET.fromstring(xml)
    owner = _value(root, "reportingOwner/reportingOwnerId/rptOwnerName")
    relation = root.find("reportingOwner/reportingOwnerRelationship")
    title = _value(relation, "officerTitle")
    if not title:
        title = "Direktor" if _value(relation, "isDirector") in {"1", "true"} else (
            "Großaktionär" if _value(relation, "isTenPercentOwner") in {"1", "true"} else "")
    plan = _value(root, "aff10b5One") in {"1", "true"}
    trades = []
    for row in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _value(row, "transactionCoding/transactionCode")
        if code not in CODES:
            continue
        shares = float(_value(row, "transactionAmounts/transactionShares/value") or 0)
        price = float(_value(row, "transactionAmounts/transactionPricePerShare/value") or 0)
        try:
            traded = date.fromisoformat(_value(row, "transactionDate/value")[:10])
        except ValueError:
            traded = None
        trades.append({"person": _owner(owner), "role": title[:30], "kind": CODES[code], "volume": shares * price,
                       "sign": "$", "traded": traded, "plan": plan, "company": ""})
    return trades


def _owner(name: str) -> str:
    """SEC names read 'Huang Jen Hsun' (surname first): keep the surname, nicely cased."""
    first = (name or "?").replace(",", " ").split()[0] if (name or "").strip() else "?"
    return first.capitalize() if first.isupper() else first


async def american(client: httpx.AsyncClient, ticker: str, label: str, filings: int = 8) -> dict | None:
    number = await cik(client, ticker)
    if not number:
        return None
    data = await read_json(client, "GET", SUBMISSIONS.format(cik=number), headers=_sec_headers(), timeout=12,
                           limit=6_000_000)
    recent = ((data or {}).get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    wanted = [i for i, form in enumerate(forms) if form == "4"][:filings]
    if not wanted:
        return None
    trades: list[dict] = []
    for i in wanted:
        accession = str(recent["accessionNumber"][i]).replace("-", "")
        document = str(recent["primaryDocument"][i]).split("/")[-1]
        url = ARCHIVE.format(cik=number, accession=accession, document=document)
        try:
            trades += form4(await read_bytes(client, "GET", url, headers=_sec_headers(), timeout=12, limit=1_000_000))
        except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
            log.warning("Form 4 %s failed: %s", url, exc)
    first = recent["filingDate"][wanted[-1]]
    trades = merge(trades)
    if not trades:
        return {"text": f"{label} Insider (SEC): seit {_date(first)} nur Zuteilungen, Optionen und Steuereinbehalte, "
                        "keine Käufe oder Verkäufe an der Börse.",
                "sources": [f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={number:010d}&type=4"]}
    trades.sort(key=lambda r: r["traded"] or date.min, reverse=True)
    lines = [trade_line(r, company=False) + (" (Plan)" if r["plan"] else "") for r in trades[:4]]
    text = f"{label} Insider (SEC Form 4), zuletzt: " + "; ".join(lines).rstrip(".") + f". Seit {_date(first)}: {_counts(trades)}."
    if any(r["plan"] for r in trades[:4]):
        text += " Plan = lange vorher festgelegter Verkaufsplan."
    return {"text": text,
            "sources": [f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={number:010d}&type=4"]}


def _date(text: str) -> str:
    try:
        return f"{date.fromisoformat(text[:10]):%d.%m.%Y}"
    except ValueError:
        return text


# ---------------------------------------------------------------- entry point

async def insider_trades(client: httpx.AsyncClient, asset: str = "", isin_of=None) -> dict:
    """Today's insider trades in Germany, or the latest ones of one company. LookupError when unknown."""
    asset = asset.strip()
    if not asset:
        return await today(client)
    if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", asset.upper()):
        found = await german(client, asset, asset.upper())
        if found:
            return found
        raise LookupError(asset)
    resolved = await resolve(client, asset)
    symbol, label = resolved if resolved else ("", asset)
    if symbol.startswith("^") or symbol.endswith(("-EUR", "=X", "=F")):
        raise LookupError(label)
    home = HOME.get(symbol, symbol)
    # German firms with a US listing (ADR) report their insiders to the BaFin, not on Form 4.
    filer = "" if symbol in ADR else home if re.fullmatch(r"[A-Z]{1,5}(-[A-Z])?", home) else ""
    if filer:
        if not sec_contact():
            return {"text": f"Für US-Firmen wie {label} lese ich Insider-Geschäfte aus den SEC-Meldungen, dafür fehlt "
                            "auf dem Pi die Kontaktadresse MESHCORE_SEC_CONTACT.", "sources": []}
        found = await american(client, filer, label)
        if found:
            return found
        raise LookupError(label)
    isin = ""
    if isin_of is not None:
        try:
            isin = await isin_of(label) or ""
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            isin = ""
    found = await german(client, label, isin)
    if found:
        return found
    return {"text": f"Zu {label} finde ich bei der BaFin keine gemeldeten Insider-Geschäfte.", "sources": [BAFIN_PAGE]}
