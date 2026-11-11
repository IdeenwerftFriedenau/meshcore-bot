"""Stock, ETF, crypto, commodity and currency quotes for the #börsenbot channel.

Free Yahoo Finance endpoints without a key: chart (price, day and 52-week range,
one year of daily closes) and search (name, ticker or ISIN to a symbol). The
assessment is a plain rule-based reading of trend and range, never a price target.
"""

from __future__ import annotations

import asyncio
import difflib
import math
import re
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx

from .http import read_json

HEADERS = {"User-Agent": "MeshCoreAIBot/0.4 (+https://github.com/IdeenwerftFriedenau/meshcore-bot)"}
HOSTS = ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com")
DISCLAIMER = "keine Anlageberatung"


def fold(text: str) -> str:
    """Lower case, umlauts spelled out, punctuation as spaces; '&' stays for S&P."""
    text = text.casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return " ".join(re.sub(r"[^a-z0-9&.^=-]+", " ", text).replace("-", " ").split())


# Folded patterns -> Yahoo symbol and the short name said on air. First match wins, so specific before general.
ALIASES: list[tuple[str, str, str]] = [
    (r"nasdaq ?100", "^NDX", "Nasdaq 100"),
    (r"nasdaq", "^IXIC", "Nasdaq"),
    (r"s ?& ?p( ?500)?|sp ?500|s und p", "^GSPC", "S&P 500"),
    (r"dow( jones)?", "^DJI", "Dow Jones"),
    (r"mdax", "^MDAXI", "MDAX"),
    (r"sdax", "^SDAXI", "SDAX"),
    (r"tecdax", "^TECDAX", "TecDAX"),
    (r"dax", "^GDAXI", "DAX"),
    (r"euro ?stoxx( 50)?", "^STOXX50E", "Euro Stoxx 50"),
    (r"nikkei", "^N225", "Nikkei"),
    (r"ftse ?100", "^FTSE", "FTSE 100"),
    (r"hang ?seng", "^HSI", "Hang Seng"),
    (r"smi", "^SSMI", "SMI"),
    (r"atx", "^ATX", "ATX"),
    (r"cac ?40", "^FCHI", "CAC 40"),
    (r"msci world|weltportfolio|welt etf", "EUNL.DE", "MSCI World (iShares-ETF)"),
    (r"(ftse |vanguard )?all ?world", "VWCE.DE", "FTSE All-World (Vanguard-ETF)"),
    (r"msci em|emerging markets?|schwellenlaender", "IS3N.DE", "MSCI EM IMI (iShares-ETF)"),
    (r"bitcoin|btc", "BTC-EUR", "Bitcoin"),
    (r"ethereum|ether|eth", "ETH-EUR", "Ethereum"),
    (r"solana", "SOL-EUR", "Solana"),
    (r"xrp|ripple", "XRP-EUR", "XRP"),
    (r"dogecoin|doge", "DOGE-EUR", "Dogecoin"),
    (r"cardano", "ADA-EUR", "Cardano"),
    (r"litecoin", "LTC-EUR", "Litecoin"),
    (r"tether|usdt", "USDT-EUR", "Tether"),
    (r"gold(preis)?", "GC=F", "Gold (Feinunze)"),
    (r"silber(preis)?", "SI=F", "Silber (Feinunze)"),
    (r"platin", "PL=F", "Platin (Feinunze)"),
    (r"palladium", "PA=F", "Palladium (Feinunze)"),
    (r"brent|oel(preis)?|rohoel", "BZ=F", "Brent-Öl (Barrel)"),
    (r"wti", "CL=F", "WTI-Öl (Barrel)"),
    (r"erdgas(preis)?", "NG=F", "Erdgas US (MMBtu)"),
    (r"kupfer", "HG=F", "Kupfer (Pfund)"),
    (r"weizen", "ZW=F", "Weizen (Scheffel)"),
    (r"kaffee", "KC=F", "Kaffee (Pfund)"),
    (r"kakao", "CC=F", "Kakao (Tonne)"),
    (r"euro ?(in |zum |gegen |vs )?dollar|eur ?usd|dollar ?kurs|dollar", "EURUSD=X", "Euro in Dollar"),
    (r"franken|eur ?chf", "EURCHF=X", "Euro in Franken"),
    (r"pfund|eur ?gbp", "EURGBP=X", "Euro in Pfund"),
    (r"yen|eur ?jpy", "EURJPY=X", "Euro in Yen"),
]
_ALIAS_RE = [(re.compile(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])"), symbol, label) for pattern, symbol, label in ALIASES]

STOPWORDS = set("""
wie was wo wer wann warum wieso weshalb welche welcher welches steht stehen stand standen ist sind war der die das
den dem des ein eine einen einem einer aktie aktien aktienkurs kurs kurse kursen boersenkurs aktuell aktuelle
aktueller aktuellen heute gerade jetzt momentan derzeit macht machen von vom zu zum zur fuer mit im in an am
bei preis preise wert hoch hochs tief tiefs tageshoch tagestief jahreshoch jahrestief 52 wochen wochenhoch
wochentief einschaetzung einschaetzungen meinung kaufen kauf verkaufen verkauf halten lohnt lohnen sich soll
sollte sollen ich man noch gut gute schlecht prognose tipp tipps ratschlag rat chart trend boerse stand gibt es
mal bitte zeig zeige sag sage sagst mir sagen kannst du dazu davon denkst haeltst findest steigt faellt
gestiegen gefallen entwicklung performance info infos daten zahlen etf etfs fonds coin coins krypto kurz und
oder vs versus gegen vergleich vergleiche aus sicht deiner deine dein meine mein uns unser ihr nach seit lage
investieren investiert einsteigen nachkaufen aussicht aussichten empfehlung empfiehlst bewertung so viel hallo hi
moin servus danke super toll ok okay ja nein bot boersenbot hey na kostet kosten euro dollar laeuft lief
geht los welt markt maerkte marktueberblick fallen steigen faellt steigt gefallen gestiegen
abgestuerzt crash rally ueberblick uebersicht marktlage boersentag bist bin heisst hast machst gehts wars lieber eigentlich denn doch schon auch
nicht kein keine wieder mehr weniger genau ungefaehr etwa wirklich sehr
hat haben hatte sitzt wer letzte letztes letzten letzter jahr jahres jahresbericht quartal gross hoehe zahlt zahlen ausgeschuettet
""".split())
# Popular shares, checked against Yahoo: no search request, clean names, typo matching.
# Folded spellings separated by "|" -> XETRA symbol (euro) and the name said on air.
STOCKS: list[tuple[str, str, str]] = [
    ("adidas", "ADS.DE", "Adidas"), ("airbus", "AIR.DE", "Airbus"), ("allianz", "ALV.DE", "Allianz"),
    ("basf", "BAS.DE", "BASF"), ("bayer", "BAYN.DE", "Bayer"), ("beiersdorf|nivea", "BEI.DE", "Beiersdorf"),
    ("bmw", "BMW.DE", "BMW"), ("brenntag", "BNR.DE", "Brenntag"), ("commerzbank", "CBK.DE", "Commerzbank"),
    ("continental", "CON.DE", "Continental"), ("daimler truck", "DTG.DE", "Daimler Truck"),
    ("deutsche bank", "DBK.DE", "Deutsche Bank"), ("deutsche boerse", "DB1.DE", "Deutsche Börse"),
    ("dhl|deutsche post|post", "DHL.DE", "DHL Group"), ("telekom|deutsche telekom", "DTE.DE", "Deutsche Telekom"),
    ("eon|e.on", "EOAN.DE", "E.ON"), ("fresenius medical care|fmc", "FME.DE", "Fresenius Medical Care"),
    ("fresenius", "FRE.DE", "Fresenius"), ("hannover rueck|hannover re", "HNR1.DE", "Hannover Rück"),
    ("heidelberg materials|heidelbergcement", "HEI.DE", "Heidelberg Materials"), ("henkel", "HEN3.DE", "Henkel"),
    ("infineon", "IFX.DE", "Infineon"), ("mercedes|mercedes benz|daimler", "MBG.DE", "Mercedes-Benz"),
    ("merck", "MRK.DE", "Merck KGaA"), ("mtu|mtu aero engines", "MTX.DE", "MTU Aero Engines"),
    ("muenchener rueck|muenchner rueck|munich re|muenchener rueckversicherung", "MUV2.DE", "Munich Re"),
    ("porsche se|porsche holding", "PAH3.DE", "Porsche SE"), ("porsche|porsche ag", "P911.DE", "Porsche AG"),
    ("qiagen", "QIA.DE", "Qiagen"), ("rheinmetall", "RHM.DE", "Rheinmetall"), ("rwe", "RWE.DE", "RWE"),
    ("sap", "SAP.DE", "SAP"), ("sartorius", "SRT3.DE", "Sartorius"), ("siemens energy", "ENR.DE", "Siemens Energy"),
    ("siemens healthineers|healthineers", "SHL.DE", "Siemens Healthineers"), ("siemens", "SIE.DE", "Siemens"),
    ("symrise", "SY1.DE", "Symrise"), ("volkswagen|vw", "VOW3.DE", "Volkswagen"), ("vonovia", "VNA.DE", "Vonovia"),
    ("zalando", "ZAL.DE", "Zalando"), ("puma", "PUM.DE", "Puma"), ("hensoldt", "HAG.DE", "Hensoldt"),
    ("renk", "R3NK.DE", "Renk"), ("lufthansa", "LHA.DE", "Lufthansa"), ("tui", "TUI1.DE", "TUI"),
    ("thyssenkrupp|thyssen", "TKA.DE", "Thyssenkrupp"), ("hellofresh", "HFG.DE", "HelloFresh"),
    ("delivery hero", "DHER.DE", "Delivery Hero"), ("nordex", "NDX1.DE", "Nordex"), ("evotec", "EVT.DE", "Evotec"),
    ("apple", "APC.DE", "Apple"), ("microsoft", "MSF.DE", "Microsoft"), ("nvidia", "NVD.DE", "Nvidia"),
    ("amazon", "AMZ.DE", "Amazon"), ("alphabet|google", "ABEA.DE", "Alphabet (Google)"),
    ("meta|facebook|meta platforms", "FB2A.DE", "Meta"), ("tesla", "TL0.DE", "Tesla"), ("netflix", "NFC.DE", "Netflix"),
    ("amd", "AMD.DE", "AMD"), ("palantir", "PTX.DE", "Palantir"), ("coca cola|cocacola|coke", "CCC3.DE", "Coca-Cola"),
    ("asml", "ASME.DE", "ASML"), ("berkshire|berkshire hathaway", "BRYN.DE", "Berkshire Hathaway"),
    ("broadcom", "1YD.DE", "Broadcom"), ("paypal", "2PP.DE", "PayPal"),
]
_STOCK_KEYS: dict[str, tuple[str, str]] = {
    key: (symbol, label) for keys, symbol, label in STOCKS for key in keys.split("|")
}
_STOCK_RE = [
    (re.compile(rf"(?<![a-z0-9])(?:{keys.replace('.', chr(92) + '.')})(?![a-z0-9])"), symbol, label)
    for keys, symbol, label in sorted(STOCKS, key=lambda row: -max(len(k) for k in row[0].split("|")))
]
# Words Yahoo would also match ('Wer bist du' -> BIST 100) and short words that are no asset.
_ALIAS_WORDS = sorted({w for pattern, _, _ in ALIASES for w in re.findall(r"[a-z]{4,}", pattern)})


def known_asset(text: str) -> tuple[str, str] | None:
    """A known index, coin, commodity, currency or popular share named in the text."""
    found = alias(text)
    if found:
        return found
    folded = fold(text)
    for pattern, symbol, label in _STOCK_RE:
        if pattern.search(folded):
            return symbol, label
    return None


def corrected(name: str) -> tuple[str, str] | None:
    """Typos on tiny radio keyboards: 'rheinmetal', 'bitcon', 'siemns' -> the known asset."""
    word = fold(name)
    if len(word) < 4:
        return None
    candidates = list(_STOCK_KEYS) + _ALIAS_WORDS
    match = difflib.get_close_matches(word, candidates, n=1, cutoff=0.8)
    if not match:
        return None
    return _STOCK_KEYS.get(match[0]) or alias(match[0])


ADVICE = re.compile(
    r"einschaetz|meinung|kaufen|kauf\b|verkauf|halten|lohnt|ratschlag|\brat\b|tipp|prognose|empfehl|investier|"
    r"einsteig|nachkauf|aussicht|bewert|denkst|haeltst|sinnvoll|zukunft|chance|risik", re.I)
EXPLAIN = re.compile(
    r"^(was (ist|sind|bedeutet|bedeuten|heisst|macht man)|erklaer|wie funktionier|warum|wieso|weshalb|"
    r"was passiert|was ist der unterschied|unterschied)", re.I)
OVERVIEW = re.compile(r"\b(markt|maerkte|boerse|ueberblick|uebersicht|indizes|lage|aktienmarkt)", re.I)
SPLIT = re.compile(r"\s*(?:,|;|\bund\b|\boder\b|\bvs\.?|\bversus\b|\bgegen(?:ueber)?\b|\bmit\b)\s*", re.I)
MORE_PREFIX = "Erzähl mir bitte weitere"
# Key figure questions ('KGV von SAP', 'Umsatz Nvidia') go to fundamentals.py, not to the price.
FACTS = re.compile(
    r"\b(kgv|kbv|eps|kennzahl\w*|fundamental\w*|bilanz\w*|marktkap\w*|marktwert|boersenwert|unternehmenswert|"
    r"umsatz\w*|gewinn|gewinne|jahresgewinn|nettogewinn|reingewinn|verlust|verluste|dividende\w*|ausschuettung|"
    r"schulden|verschuld\w*|mitarbeiter\w*|beschaeftigte|kurs gewinn verhaeltnis|geschaeftszahlen|quartalszahlen|"
    r"jahreszahlen)\b")
# Profile questions ('Wer ist Chef von SAP?', 'ISIN Siemens') take the same way with about=True.
PROFILE = re.compile(
    r"\b(chef\w*|ceo|boss|vorstandschef\w*|vorstandsvorsitz\w*|firmenchef\w*|konzernchef\w*|sitz|hauptsitz|"
    r"firmensitz|konzernsitz|wo sitzt|gegruendet|gruendung\w*|isin|branche|firmenprofil|unternehmensprofil|profil|"
    r"webseite|website|homepage|steckbrief)\b")
WORTH = re.compile(r"^(wie ?viel|was) (ist|sind) .{2,40} (wert|schwer)\b")


def alias(text: str) -> tuple[str, str] | None:
    folded = fold(text)
    for pattern, symbol, label in _ALIAS_RE:
        if pattern.search(folded):
            return symbol, label
    return None


def lookup_words(part: str) -> str:
    """What is left of a question part once the filler words are gone, e.g. 'Rheinmetall'."""
    words = [w for w in re.findall(r"[\wäöüÄÖÜß.&^=-]+", part) if fold(w) not in STOPWORDS and fold(w)]
    return " ".join(words)[:60].strip(" .-")


def market_request(question: str, *, channel: bool = False) -> dict | None:
    """Route a quote question: {'assets': [...], 'advice': bool, 'overview': bool}.

    channel=True is the #börsenbot channel, where almost every message is about markets.
    Elsewhere only clear quote questions ('Kurs', 'Aktie' or a known asset with price words) are taken.
    """
    text = question.strip()
    if not text or text.startswith(MORE_PREFIX):
        return None
    folded = fold(text)
    advice = bool(ADVICE.search(folded))
    known = known_asset(text)
    if re.search(r"\b(top|flop|tops|flops|gewinner|verlierer|renner|penner)\b", folded) and (
            channel or re.search(r"\bdax\b|\baktien?\b|\bboerse", folded)):
        return {"assets": [], "advice": False, "overview": False, "movers": True}
    if re.search(r"\b(angst|gier|stimmung|fear|greed|sentiment)\b", folded) and (
            channel or re.search(r"krypto|bitcoin|boerse|markt", folded)):
        return {"assets": [], "advice": False, "overview": False, "mood": True}
    about = bool(PROFILE.search(folded)) and not FACTS.search(folded)
    facts = FACTS.search(folded) or WORTH.search(folded) or about
    if facts and not re.match(r"(was (ist|sind|bedeutet|heisst)|erklaer)\w* (ein|eine|das|der|die)?\s*"
                              r"(kgv|kbv|eps|dividende|bilanz|kennzahl|marktkap|boersenwert)\w*\s*\??$", folded):
        rest = PROFILE.sub(" ", FACTS.sub(" ", folded))
        known = known_asset(rest) or known_asset(text)
        no_company = known and (known[0].startswith("^") or known[0].endswith(("-EUR", "=X", "=F")))
        if not no_company and (known or channel or re.search(r"\baktien?\b|\bfirma\b|\bkonzern", folded)):
            parts = []
            for part in SPLIT.split(rest):
                hit = known_asset(part)
                name = hit[1] if hit else lookup_words(part)
                if name and name.casefold() not in {p.casefold() for p in parts}:
                    parts.append(name)
            if parts:
                found = {"assets": parts[:2], "advice": False, "overview": False, "facts": True}
                return {**found, "about": True} if about else found
    why = re.match(r"(warum|wieso|weshalb)\b", folded) or re.search(r"\b(was ist (da )?los mit|grund)\b", folded)
    news = re.search(r"\b(news|nachrichten|neuigkeiten|meldungen|schlagzeilen|neues (von|zu|bei))\b", folded)
    moving = re.search(r"\b(faellt|fallt|steigt|gestiegen|gefallen|stuerzt|abgestuerzt|crash|rally|aktie|kurs|"
                       r"im minus|im plus|explodiert|bricht ein|eingebrochen)\b", folded)
    if (why and (known or (channel and moving))) or (news and (known or channel)):
        target = known[1] if known else lookup_words(re.sub(r"(?i)\b(news|nachrichten|neuigkeiten|meldungen|"
                                                                   r"schlagzeilen|neues|grund|los)\b", " ", text))
        if target and (known or channel):
            return {"assets": [target], "advice": False, "overview": False, "news": True, "why": bool(why)}
    if not channel:
        # #fragdenbot and friends: a named share/coin/index with a market word, or 'Aktie'/'Kurs' plus a name.
        pricey = re.search(r"\b(kurs\w*|\w+kurs|\w*preis|steht|stehen|notiert|wert|wie hoch|tageshoch|tagestief|"
                           r"52 wochen|jahreshoch|jahrestief|wie laeuft|wie lief|gestiegen|gefallen|steigt|faellt|"
                           r"im plus|im minus|chart|performance)\b", folded)
        stock = re.search(r"\baktien?(kurs)?\b|\bboersenkurs\b|\bbitcoin|\bkrypto", folded)
        market = re.search(r"\b(boerse|boersen|aktienmarkt|maerkte|marktueberblick|marktlage|dax)\b", folded)
        if EXPLAIN.match(folded):
            return None
        if market and not (known and known[0] != "^GDAXI") and OVERVIEW.search(folded) and not stock:
            return {"assets": [], "advice": advice, "overview": True}
        if not (stock or (known and (pricey or advice))):
            return None
    elif (EXPLAIN.match(folded) and not known) or re.match(r"(warum|wieso|weshalb)\b", folded):
        return None  # reasons need news: the model answers with get_quote and search_news
    if channel and OVERVIEW.search(folded) and not known:
        named = [lookup_words(p) for p in SPLIT.split(text)]
        if not any(named):
            return {"assets": [], "advice": advice, "overview": True}
    parts: list[str] = []
    for part in SPLIT.split(text):
        known = known_asset(part)
        name = known[1] if known else lookup_words(part)
        if name and name.casefold() not in {p.casefold() for p in parts}:
            parts.append(name)
    parts = parts[:3]
    if not parts:
        return None
    return {"assets": parts, "advice": advice or channel, "overview": False}


async def _get(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    error: Exception | None = None
    # Yahoo throttles bursts: the other host often still answers, and a short pause usually helps.
    for attempt, host in enumerate((*HOSTS, HOSTS[0])):
        if attempt:
            await asyncio.sleep(0.8 * attempt)
        try:
            return await read_json(client, "GET", host + path, params=params, headers=HEADERS, timeout=10)
        except (httpx.HTTPError, ValueError) as exc:
            error = exc
    raise error or httpx.TransportError("Yahoo Finance unreachable")


QUOTE_TYPES = {"EQUITY": 0, "ETF": 1, "INDEX": 2, "CRYPTOCURRENCY": 3, "MUTUALFUND": 4, "CURRENCY": 5}


_RESOLVED: dict[str, tuple[float, tuple[str, str] | None]] = {}
MAJOR = {"GER", "FRA", "NMS", "NYQ", "NGM", "NCM", "ASE", "PCX", "BTS", "CCC", "LSE", "PAR", "AMS", "EBS", "VIE",
         "MIL", "MCE", "CPH", "STO", "HEL", "OSL", "TOR", "TYO", "HKG", "BRU", "LIS", "DJI", "SNP", "NIM", "CXI"}


def pretty(label: str) -> str:
    """'Siemens Aktiengesellschaft' -> 'Siemens', 'RENK Group AG' -> 'Renk Group'."""
    label = " ".join(label.split())
    label = re.sub(r"^(The|Dr\. Ing\. h\.c\. F\.) ", "", label)
    label = re.sub(r"\s+(in München|Aktiengesellschaft.*)$", "", label)
    for _ in range(2):
        label = re.sub(r",? (Inc\.?|AG|SE|N\.V\.|S\.A\.|plc|Corporation|Corp\.?|Holdings?|Ltd\.?|Co\.?|KGaA|"
                       r"& Co\.|Limited|A/S|ASA|S\.p\.A\.)$", "", label)
    keep = {"BASF", "ASML", "MSCI", "FTSE", "NASDAQ", "IBM", "UCITS", "ETF", "USD", "EUR", "BYD"}
    words = [w.capitalize() if w.isupper() and len(w) >= 4 and w.isalpha() and w not in keep else w
             for w in label.split()]
    return " ".join(words)[:40]


async def resolve(client: httpx.AsyncClient, name: str) -> tuple[str, str] | None:
    """Name, ticker or ISIN -> (symbol, short name). Known names need no request."""
    known = known_asset(name)
    if known:
        return known
    key = fold(name)
    cached = _RESOLVED.get(key)
    if cached and time.monotonic() - cached[0] < 24 * 3600:
        return cached[1]
    try:
        found = await _search(client, name)
    except (httpx.HTTPError, ValueError):
        found = corrected(name)  # search throttled: a near-miss of a known name still answers
        if not found:
            raise
    if not found:
        found = corrected(name)
    if len(_RESOLVED) > 500:
        _RESOLVED.clear()
    _RESOLVED[key] = (time.monotonic(), found)
    return found


async def _search(client: httpx.AsyncClient, name: str) -> tuple[str, str] | None:
    typo = corrected(name)
    if typo and not re.fullmatch(r"[A-Z0-9.]{2,12}", name.strip()):
        return typo
    data = await _get(client, "/v1/finance/search",
                      {"q": name, "quotesCount": 10, "newsCount": 0, "lang": "de-DE", "region": "DE"})
    rows = [
        q for q in data.get("quotes") or []
        if q.get("quoteType") in QUOTE_TYPES and q.get("symbol") and q.get("exchange") not in {"PNK", "OTC"}
    ]
    rows = [q for q in rows if plausible(name, q)]
    rows.sort(key=lambda q: q.get("exchange") not in MAJOR)  # stable: Yahoo's order within each group
    if not rows:
        return None
    best = rows[0]
    if "." not in name and best.get("exchange") != "GER":
        # Radio users here think in euro: prefer the XETRA listing of the same company.
        first = fold(str(best.get("longname") or best.get("shortname") or "")).split()[:1]
        best = next((q for q in rows if q.get("exchange") == "GER"
                     and fold(str(q.get("longname") or q.get("shortname") or "")).split()[:1] == first), best)
    return best["symbol"], pretty(str(best.get("longname") or best.get("shortname") or best["symbol"]))


def plausible(query: str, row: dict) -> bool:
    """Yahoo finds *something* for any word ('Hallo'): keep hits that really carry the asked name."""
    raw = query.strip()
    symbol = str(row.get("symbol", "")).upper()
    if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", raw):  # ISIN
        return True
    if re.fullmatch(r"[A-Za-z0-9.^=-]{1,10}", raw) and symbol.split(".")[0] == raw.upper().split(".")[0]:
        return True
    names = fold(f"{row.get('longname') or ''} {row.get('shortname') or ''}")
    joined = names.replace(" ", "")
    words = [w for w in fold(raw).split() if len(w) >= 3]
    if not words or not all(w in joined for w in words):
        return False
    if row.get("quoteType") in {"CRYPTOCURRENCY", "INDEX"}:  # 'bitcon' -> BitCone, 'bist' -> BIST 100
        return all(w in names.split() for w in words) and len(words) > 1 or fold(raw) == names.split()[0]
    return True


def _closes(result: dict) -> list[tuple[int, float]]:
    stamps = result.get("timestamp") or []
    closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    return [(t, float(c)) for t, c in zip(stamps, closes, strict=False) if isinstance(c, int | float) and c > 0]


_LAST: dict[str, tuple[float, dict]] = {}


async def quote(client: httpx.AsyncClient, symbol: str, label: str = "") -> dict:
    try:
        data = await _get(client, f"/v8/finance/chart/{symbol}", {"range": "1y", "interval": "1d"})
        result = _parse(data, symbol, label)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        # Second source first: CoinGecko for coins, the ECB for currencies; then a price from the last two hours.
        result = None
        try:
            if symbol in COINGECKO:
                result = await coingecko_quote(client, symbol, label)
            elif symbol in ECB_PAIRS:
                result = await ecb_quote(client, symbol, label)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
            result = None
        if result is None:
            known = _LAST.get(symbol)
            if known and time.monotonic() - known[0] < 2 * 3600:
                return {**known[1], "name": label or known[1]["name"]}
            raise
    widen(result)
    if len(_LAST) > 300:
        _LAST.clear()
    _LAST[symbol] = (time.monotonic(), result)
    return result


def widen(data: dict) -> None:
    """Yahoo's day and 52-week ranges lag the live price now and then; a range must never exclude the price."""
    price = data.get("price")
    if not _ok(price):
        return
    for low, high in (("day_low", "day_high"), ("low52", "high52")):
        if _ok(data.get(low), data.get(high)):
            data[low], data[high] = min(data[low], price), max(data[high], price)


COINGECKO = {"BTC-EUR": "bitcoin", "ETH-EUR": "ethereum", "SOL-EUR": "solana", "XRP-EUR": "ripple",
             "DOGE-EUR": "dogecoin", "ADA-EUR": "cardano", "LTC-EUR": "litecoin", "USDT-EUR": "tether"}
ECB_PAIRS = {"EURUSD=X": "USD", "EURCHF=X": "CHF", "EURGBP=X": "GBP", "EURJPY=X": "JPY"}
ECB_URL = "https://api.frankfurter.dev/v1"


async def coingecko_quote(client: httpx.AsyncClient, symbol: str, label: str = "") -> dict:
    coin = COINGECKO[symbol]
    rows = await read_json(client, "GET", "https://api.coingecko.com/api/v3/coins/markets",
                           params={"vs_currency": "eur", "ids": coin}, headers=HEADERS, timeout=10)
    row = rows[0]
    chart = await read_json(client, "GET", f"https://api.coingecko.com/api/v3/coins/{coin}/market_chart",
                            params={"vs_currency": "eur", "days": 365, "interval": "daily"}, headers=HEADERS,
                            timeout=10)
    closes = [(int(t / 1000), float(p)) for t, p in chart.get("prices") or [] if p]
    values = [p for _, p in closes] or [row["current_price"]]
    return {
        "symbol": symbol, "name": label or row.get("name") or symbol, "kind": "CRYPTOCURRENCY", "currency": "EUR",
        "exchange": "CoinGecko", "price": float(row["current_price"]), "change": row.get("price_change_percentage_24h"),
        "day_high": row.get("high_24h"), "day_low": row.get("low_24h"), "high52": max(values), "low52": min(values),
        "time": int(time.time()), "closes": closes, "source": f"https://www.coingecko.com/de/munze/{coin}",
    }


async def ecb_quote(client: httpx.AsyncClient, symbol: str, label: str = "") -> dict:
    """ECB reference rates (once a day, about 16:00): no day range, but a whole year of closes."""
    target = ECB_PAIRS[symbol]
    start = (datetime.now(UTC).date().replace(day=1).replace(year=datetime.now(UTC).year - 1)).isoformat()
    data = await read_json(client, "GET", f"{ECB_URL}/{start}..", params={"base": "EUR", "symbols": target},
                           headers=HEADERS, timeout=10)
    rates = sorted((day, values[target]) for day, values in (data.get("rates") or {}).items() if target in values)
    closes = [(int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp()), float(v)) for day, v in rates]
    if not closes:
        raise ValueError("No ECB rates")
    values = [v for _, v in closes[-260:]]
    price = closes[-1][1]
    change = (price / closes[-2][1] - 1) * 100 if len(closes) >= 2 else None
    return {
        "symbol": symbol, "name": label or f"Euro in {target}", "kind": "CURRENCY", "currency": target,
        "exchange": "EZB", "price": price, "change": change, "day_high": None, "day_low": None,
        "high52": max(values), "low52": min(values), "time": closes[-1][0] + 16 * 3600, "closes": closes[-260:],
        "source": "https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/",
    }


async def ecb_rate(client: httpx.AsyncClient, symbol: str) -> float | None:
    if symbol not in ECB_PAIRS:
        return None
    try:
        data = await read_json(client, "GET", f"{ECB_URL}/latest", params={"base": "EUR", "symbols": ECB_PAIRS[symbol]},
                               headers=HEADERS, timeout=8)
        return float(data["rates"][ECB_PAIRS[symbol]])
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


async def crypto_mood(client: httpx.AsyncClient) -> str:
    """Crypto Fear & Greed index (alternative.me), e.g. 'Krypto-Stimmung: Angst (32/100)'."""
    try:
        data = await read_json(client, "GET", "https://api.alternative.me/fng/", params={"limit": 1},
                               headers=HEADERS, timeout=8)
        row = data["data"][0]
        value = int(row["value"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
        return ""
    words = {"Extreme Fear": "extreme Angst", "Fear": "Angst", "Neutral": "neutral", "Greed": "Gier",
             "Extreme Greed": "extreme Gier"}
    return f"Krypto-Stimmung: {words.get(row.get('value_classification'), row.get('value_classification', ''))} ({value}/100)"


def _parse(data: dict, symbol: str, label: str) -> dict:
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        raise ValueError("No quote")
    meta = result[0].get("meta") or {}
    price = meta.get("regularMarketPrice")
    if not isinstance(price, int | float) or price <= 0:
        raise ValueError("No price")
    closes = _closes(result[0])
    change = meta.get("regularMarketChangePercent")
    if not isinstance(change, int | float):
        previous = meta.get("previousClose") or (closes[-2][1] if len(closes) >= 2 else None)
        change = (price / previous - 1) * 100 if previous else None
    name = label or " ".join(str(meta.get("shortName") or meta.get("longName") or symbol).split())
    return {
        "symbol": meta.get("symbol") or symbol,
        "name": name,
        "kind": meta.get("instrumentType") or "",
        "currency": meta.get("currency") or "",
        "exchange": meta.get("fullExchangeName") or "",
        "price": float(price),
        "change": change,
        "day_high": meta.get("regularMarketDayHigh"),
        "day_low": meta.get("regularMarketDayLow"),
        "high52": meta.get("fiftyTwoWeekHigh"),
        "low52": meta.get("fiftyTwoWeekLow"),
        "time": meta.get("regularMarketTime"),
        "closes": closes,
    }


def number(value: float, kind: str = "") -> str:
    """German number format sized to the value: 26.420 / 209,55 / 1,1153 / 0,4521."""
    if kind == "CURRENCY":
        digits = 4 if value < 10 else 2
    else:
        digits = 0 if value >= 1000 else 2 if value >= 1 else 4
    text = f"{value:,.{digits}f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def unit(data: dict) -> str:
    if data["kind"] in {"INDEX"}:
        return " Pkt"
    if data["kind"] == "CURRENCY":
        return ""
    return {"EUR": " €", "USD": " $", "USX": " US-Cent", "GBp": " Pence", "GBP": " £", "CHF": " CHF", "JPY": " Yen"}.get(
        data["currency"], f" {data['currency']}" if data["currency"] else "")


def percent(value: float | None) -> str:
    if value is None:
        return ""
    value = round(value, 1) or 0.0  # no "-0,0 %"
    text = f"{value:+.1f}".replace(".", ",")
    return text + " %"


def _ok(*values) -> bool:
    return all(isinstance(v, int | float) and v > 0 for v in values)


def performance(data: dict, days: int) -> float | None:
    closes = data["closes"]
    if not closes:
        return None
    since = closes[-1][0] - days * 86400
    start = next((c for t, c in closes if t >= since), None)
    if not start or (closes[-1][0] - closes[0][0]) < days * 86400 * 0.8:
        return None
    return (data["price"] / start - 1) * 100


def stamp_note(data: dict, tz: str, now: datetime | None = None) -> str:
    """Name the time only when the price is old (weekend, holiday, closed exchange)."""
    when = data.get("time")
    if not isinstance(when, int):
        return ""
    now = now or datetime.now(UTC)
    if now.timestamp() - when < 3 * 3600:
        return ""
    local = datetime.fromtimestamp(when, ZoneInfo(tz))
    days = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    return f" (Stand {days[local.weekday()]} {local:%H:%M})"


def quote_line(data: dict, tz: str, *, short: bool = False, now: datetime | None = None) -> str:
    u, k = unit(data), data["kind"]
    line = f"{data['name']} {number(data['price'], k)}{u}"
    if data["change"] is not None:
        line += f" {percent(data['change'])}"
    line += stamp_note(data, tz, now)
    if short:
        if _ok(data["low52"], data["high52"]):
            line += f" (52W {number(data['low52'], k)}–{number(data['high52'], k)})"
        return line
    extra = []
    if _ok(data["day_low"], data["day_high"]):
        extra.append(f"Tag {number(data['day_low'], k)}–{number(data['day_high'], k)}")
    if _ok(data["low52"], data["high52"]):
        extra.append(f"52W {number(data['low52'], k)}–{number(data['high52'], k)}")
    year = performance(data, 365)
    if year is not None:
        extra.append(f"1J {percent(year)}")
    return line + (". " + ", ".join(extra) if extra else "") + "."


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def signals(data: dict) -> dict:
    closes = [c for _, c in data["closes"]]
    price = data["price"]
    sma50 = _mean(closes[-50:]) if len(closes) >= 50 else None
    sma200 = _mean(closes[-200:]) if len(closes) >= 200 else None
    if sma200 and sma50 and price > sma200 and sma50 > sma200:
        trend = "up"
    elif sma200 and sma50 and price < sma200 and sma50 < sma200:
        trend = "down"
    else:
        trend = "flat"
    position = None
    if _ok(data["low52"], data["high52"]) and data["high52"] > data["low52"]:
        position = (price - data["low52"]) / (data["high52"] - data["low52"])
    returns = [math.log(b / a) for a, b in zip(closes[-61:], closes[-60:], strict=False) if a > 0 and b > 0]
    volatility = None
    if len(returns) >= 20:
        avg = _mean(returns) or 0.0
        per_year = 365 if data["kind"] == "CRYPTOCURRENCY" else 252
        volatility = math.sqrt(sum((r - avg) ** 2 for r in returns) / (len(returns) - 1) * per_year)
    from_high = (price / data["high52"] - 1) * 100 if _ok(data["high52"]) else None
    return {"trend": trend, "position": position, "volatility": volatility, "from_high": from_high,
            "month": performance(data, 30)}


def assessment(data: dict, *, short: bool = False) -> str:
    s = signals(data)
    trend = {"up": "Aufwärtstrend", "down": "Abwärtstrend", "flat": "kein klarer Trend"}[s["trend"]]
    parts = [trend]
    position = s["position"]
    if position is not None and position >= 0.9:
        parts.append("nahe 52W-Hoch")
    elif position is not None and position <= 0.1:
        parts.append("nahe 52W-Tief")
    elif s["from_high"] is not None and s["from_high"] <= -15:
        parts.append(f"{abs(s['from_high']):.0f} % unter 52W-Hoch")
    if s["month"] is not None and abs(s["month"]) >= 5 and not short:
        parts.append(f"1M {percent(s['month'])}")
    if short:
        return ", ".join(parts)
    if s["trend"] == "up" and position is not None and position >= 0.9:
        hint = "Stark gelaufen, Rücksetzer sind jederzeit möglich."
    elif s["trend"] == "up" and s["month"] is not None and s["month"] <= -8:
        hint = "Gerade deutliche Korrektur, der längere Trend hält noch."
    elif s["trend"] == "up":
        hint = "Trend intakt, Rücksetzer einplanen."
    elif s["trend"] == "down" and position is not None and position <= 0.15:
        hint = "Noch kein Boden erkennbar, eher abwarten."
    elif s["trend"] == "down":
        hint = "Schwach, eine Trendwende abwarten."
    else:
        hint = "Abwarten, bis sich eine Richtung zeigt."
    if s["volatility"] is not None and s["volatility"] >= 0.45:
        hint += " Stark schwankend: nur Geld einsetzen, das du entbehren kannst."
    return ", ".join(parts) + ". " + hint


def source(symbol: str) -> str:
    return "https://finance.yahoo.com/quote/" + symbol


async def market_answer(client: httpx.AsyncClient, assets: list[str], *, advice: bool, tz: str,
                        now: datetime | None = None) -> dict:
    """Quotes for up to three assets with an optional rule-based assessment."""
    found: list[dict] = []
    missing: list[str] = []
    for name in assets[:3]:
        resolved = await resolve(client, name)
        if not resolved:
            missing.append(name)
            continue
        found.append(await quote(client, *resolved))
    if not found:
        raise LookupError(", ".join(missing) or "nothing")
    if len(found) == 1:
        data = found[0]
        text = quote_line(data, tz, now=now)
        if data["kind"] == "CURRENCY" and data.get("exchange") != "EZB":
            rate = await ecb_rate(client, data["symbol"])
            if rate:
                text += f" EZB-Referenzkurs {number(rate, 'CURRENCY')}."
        if advice:
            text += f" Einschätzung ({DISCLAIMER}): {assessment(data)}"
    else:
        lines = []
        for data in found:
            line = quote_line(data, tz, short=True, now=now)
            if advice:
                line += ", " + assessment(data, short=True)
            lines.append(line)
        text = ". ".join(lines) + "."
        if advice:
            text += f" ({DISCLAIMER})"
    if missing:
        text += " Nicht gefunden: " + ", ".join(missing) + "."
    return {"text": text, "sources": [d.get("source") or source(d["symbol"]) for d in found]}


OVERVIEW_ASSETS = [("^GDAXI", "DAX"), ("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("BTC-EUR", "Bitcoin"),
                   ("GC=F", "Gold"), ("SI=F", "Silber"), ("EURUSD=X", "Euro")]


async def market_overview(client: httpx.AsyncClient, tz: str, now: datetime | None = None) -> dict:
    parts, sources = [], []
    for symbol, label in OVERVIEW_ASSETS:
        try:
            data = await quote(client, symbol, label)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            continue
        sign = "" if data["kind"] == "INDEX" else unit(data)
        parts.append(f"{label} {number(data['price'], data['kind'])}{sign} {percent(data['change'])}".strip())
        sources.append(source(symbol))
    if not parts:
        raise httpx.TransportError("No market data")
    mood = await crypto_mood(client)
    if mood:
        sources.append("https://alternative.me/crypto/fear-and-greed-index/")
    return {"text": "Märkte: " + ", ".join(parts) + "." + (f" {mood}." if mood else ""), "sources": sources}


DAX = ["ADS.DE", "AIR.DE", "ALV.DE", "BAS.DE", "BAYN.DE", "BEI.DE", "BMW.DE", "BNR.DE", "CBK.DE", "CON.DE",
       "DTG.DE", "DBK.DE", "DB1.DE", "DHL.DE", "DTE.DE", "EOAN.DE", "FRE.DE", "FME.DE", "HNR1.DE", "HEI.DE",
       "HEN3.DE", "IFX.DE", "MBG.DE", "MRK.DE", "MTX.DE", "MUV2.DE", "P911.DE", "PAH3.DE", "QIA.DE", "RHM.DE",
       "RWE.DE", "SAP.DE", "SRT3.DE", "SIE.DE", "ENR.DE", "SHL.DE", "SY1.DE", "VOW3.DE", "VNA.DE", "ZAL.DE"]
_LABELS = {symbol: label for _, symbol, label in STOCKS}


def _spark_rows(data: dict) -> list[tuple[str, float, float]]:
    """Both spark layouts Yahoo serves: {'spark': {'result': [...]}} and {'SAP.DE': {...}}."""
    rows = []
    if isinstance(data.get("spark"), dict):
        for item in data["spark"].get("result") or []:
            response = (item.get("response") or [{}])[0]
            meta = response.get("meta") or {}
            closes = [c for c in ((response.get("indicators") or {}).get("quote") or [{}])[0].get("close") or [] if c]
            price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
            previous = meta.get("chartPreviousClose") or meta.get("previousClose")
            rows.append((item.get("symbol"), price, previous))
    else:
        for symbol, item in data.items():
            if isinstance(item, dict):
                closes = [c for c in item.get("close") or [] if c]
                rows.append((symbol, closes[-1] if closes else None,
                             item.get("chartPreviousClose") or item.get("previousClose")))
    return [(sym, float(p), float(prev)) for sym, p, prev in rows
            if sym and isinstance(p, int | float) and isinstance(prev, int | float) and prev > 0]


async def dax_movers(client: httpx.AsyncClient, count: int = 3) -> dict:
    """Best and worst DAX shares today from two spark requests (20 symbols each)."""
    moves: list[tuple[float, str]] = []
    for start in range(0, len(DAX), 20):
        data = await _get(client, "/v8/finance/spark",
                          {"symbols": ",".join(DAX[start:start + 20]), "range": "1d", "interval": "1d"})
        moves += [((p / prev - 1) * 100, sym) for sym, p, prev in _spark_rows(data)]
    if len(moves) < 10:
        raise ValueError("Too few DAX values")
    moves.sort(reverse=True)

    def name(sym):
        return _LABELS.get(sym, sym.split(".")[0])

    top = ", ".join(f"{name(s)} {percent(m)}" for m, s in moves[:count])
    flop = ", ".join(f"{name(s)} {percent(m)}" for m, s in moves[::-1][:count])
    return {"text": f"DAX Top: {top}. Flop: {flop}.", "sources": [source("^GDAXI")]}


async def headlines(client: httpx.AsyncClient, symbol: str, name: str, tz: str, news=None,
                    now: datetime | None = None) -> list[str]:
    """Recent headlines about one asset: tagesschau (German, via news()) and Yahoo Finance, newest first."""
    now = now or datetime.now(UTC)
    found: list[tuple[float, str]] = []
    if news is not None:
        try:
            result = await news(name.split(" (")[0])
            local = now.astimezone(ZoneInfo(tz))
            for line in str(result.get("text", "")).split(" | ") if result.get("sources") else []:
                stamp = re.match(r"(\d\d)\.(\d\d)\. (\d\d):(\d\d)", line.strip())
                if not stamp:
                    continue
                day, month, hour, minute = map(int, stamp.groups())
                year = local.year - (1 if month > local.month else 0)
                when = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz))
                if local.timestamp() - when.timestamp() < 7 * 86400:
                    found.append((when.timestamp(), line.strip()[:160]))
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            pass
    try:
        data = await _get(client, "/v1/finance/search", {"q": symbol, "quotesCount": 0, "newsCount": 6})
        for row in data.get("news") or []:
            stamp = row.get("providerPublishTime")
            if isinstance(stamp, int | float) and now.timestamp() - stamp < 7 * 86400 and row.get("title"):
                day = datetime.fromtimestamp(stamp, ZoneInfo(tz)).strftime("%d.%m.")
                found.append((float(stamp), f"{day} {row.get('publisher') or 'Yahoo'}: {row['title']}"[:160]))
    except (httpx.HTTPError, ValueError):
        pass
    seen, result = set(), []
    for _, line in sorted(found, reverse=True):
        key = fold(line)[:60]
        if key not in seen:
            seen.add(key)
            result.append(line)
    return result[:5]
