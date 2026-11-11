"""Everyday data for #fragdenbot: warnings, transit disruptions, space weather, football, air, pollen,
holidays and fuel prices.

Free official or community sources meant for automated use, no key except the optional Tankerkönig one:
- Warnings: Deutscher Wetterdienst (warnings.json) and the federal warning app NINA (warnung.bund.de).
- Disruptions: BVG/VBB departures with remarks (v6.bvg.transport.rest) at the big interchange stations.
- Space weather for radio: NOAA Space Weather Prediction Center (Kp index, NOAA scales, solar flux).
- Football: OpenLigaDB (Bundesliga, 2. and 3. Liga).
- Air: Open-Meteo air quality (CAMS); pollen: DWD Pollenflug-Gefahrenindex.
- Holidays: OpenHolidays API (public and school holidays in Berlin).
- Fuel: Tankerkönig (MTS-K), only with MESHCORE_TANKERKOENIG_KEY in the environment or /etc/meshcore-ai-bot/.env.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import unicodedata
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from dotenv import dotenv_values

from .http import read_bytes, read_json

AGENT = {"User-Agent": "IdeenwerftFriedenau meshcore-bot (+https://github.com/IdeenwerftFriedenau/meshcore-bot)",
         "Accept-Language": "de"}
ENV_FILE = "/etc/meshcore-ai-bot/.env"
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
log = logging.getLogger(__name__)


def env_value(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        try:
            value = dotenv_values(ENV_FILE).get(name) or ""
        except OSError:
            value = ""
    return value.strip()


def fold(text: str) -> str:
    """Lower case, umlauts spelled out ('störung' -> 'stoerung'), punctuation and dashes as spaces."""
    text = (text or "").casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9.]+", " ", text).split())


def comma(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


def when(moment: datetime, now: datetime) -> str:
    """'heute 20 Uhr', 'morgen 10:30 Uhr', 'Fr 13.11. 8 Uhr'"""
    clock = f"{moment.hour}" if moment.minute == 0 else f"{moment:%H:%M}"
    days = (moment.date() - now.date()).days
    day = {0: "heute", 1: "morgen", -1: "gestern"}.get(days) or f"{WEEKDAYS[moment.weekday()]} {moment:%d.%m.}"
    return f"{day} {clock} Uhr"


def day_name(day: date, today: date) -> str:
    days = (day - today).days
    named = {0: "heute", 1: "morgen", 2: "übermorgen"}.get(days)
    text = f"{WEEKDAYS[day.weekday()]} {day:%d.%m.%Y}"
    return f"{named}, {text}" if named else text


async def _json(client: httpx.AsyncClient, url: str, params: dict | None = None, *, limit: int = 3_000_000,
                headers: dict | None = None):
    return await read_json(client, "GET", url, params=params, headers=headers or AGENT, timeout=15, limit=limit)


# ---------------------------------------------------------------- official warnings

DWD_WARNINGS = "https://www.dwd.de/DWD/warnungen/warnapp/json/warnings.json"
DWD_PAGE = "https://www.dwd.de/DE/wetter/warnungen_gemeinden/warnWetter_node.html"
NINA = "https://warnung.bund.de/api31/dashboard/{ars}.json"
NINA_PAGE = "https://warnung.bund.de/meldungen"


async def dwd_rows(client: httpx.AsyncClient, state: str, now: datetime) -> list[dict]:
    raw = (await read_bytes(client, "GET", DWD_WARNINGS, headers=AGENT, timeout=15, limit=10_000_000)).decode()
    data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    tz = now.tzinfo
    found: dict[tuple, dict] = {}
    for rows in (data.get("warnings") or {}).values():
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("state") != state:
                continue
            end = datetime.fromtimestamp(float(row.get("end") or 0) / 1000, tz) if row.get("end") else None
            if end and end <= now:
                continue
            start = datetime.fromtimestamp(float(row["start"]) / 1000, tz) if row.get("start") else None
            key = (row.get("headline"), row.get("start"), row.get("end"), row.get("level"))
            item = found.setdefault(key, {
                "title": str(row.get("headline") or "Warnung").replace("Amtliche WARNUNG vor ", "Warnung vor "),
                "level": int(row.get("level") or 0), "start": start, "end": end,
                "text": " ".join(str(row.get("description") or "").split()), "areas": set(), "source": "DWD"})
            item["areas"].add(str(row.get("regionName") or ""))
    return list(found.values())


async def nina_rows(client: httpx.AsyncClient, ars: str) -> list[dict]:
    data = await _json(client, NINA.format(ars=ars), limit=2_000_000)
    rows = []
    for item in data if isinstance(data, list) else []:
        payload = ((item or {}).get("payload") or {}).get("data") or {}
        provider = str(payload.get("provider") or "").upper()
        if provider == "DWD" or payload.get("msgType") == "Cancel":
            continue  # weather warnings come from the DWD feed itself, with times and text
        title = ((item.get("i18nTitle") or {}).get("de") or payload.get("headline") or "").strip()
        if title:
            rows.append({"title": " ".join(title.split()), "level": {"Extreme": 4, "Severe": 3, "Moderate": 2}.get(
                payload.get("severity"), 1), "start": None, "end": None, "text": "", "areas": set(),
                "source": {"MOWAS": "BBK", "LHP": "Hochwasserzentrale", "POLICE": "Polizei"}.get(provider, provider
                                                                                                    or "NINA")})
    return rows


async def warnings(client: httpx.AsyncClient, tz: str = "Europe/Berlin", state: str = "Berlin",
                   ars: str = "110000000000", now: datetime | None = None) -> dict:
    """Current and announced official warnings for Berlin, most severe first."""
    now = now or datetime.now(ZoneInfo(tz))
    dwd, nina = await asyncio.gather(dwd_rows(client, state, now), nina_rows(client, ars), return_exceptions=True)
    failed = [name for name, rows in (("DWD", dwd), ("NINA", nina)) if isinstance(rows, BaseException)]
    for name, rows in (("DWD", dwd), ("NINA", nina)):
        if isinstance(rows, BaseException):
            log.warning("%s warnings failed: %s", name, rows)
    if len(failed) == 2:
        raise httpx.TransportError("No warning source reachable")
    rows = [r for group in (dwd, nina) if not isinstance(group, BaseException) for r in group]
    rows.sort(key=lambda r: (-r["level"], r["start"] or now))
    note = f" ({' und '.join(failed)} gerade nicht erreichbar)" if failed else ""
    sources = [DWD_PAGE, NINA_PAGE]
    if not rows:
        return {"text": f"Für {state} gibt es gerade keine amtlichen Warnungen (DWD und NINA){note}.",
                "sources": sources}
    parts = []
    for row in rows[:3]:
        level = f" (Stufe {row['level']})" if row["source"] == "DWD" and row["level"] else ""
        span = ""
        if row["start"] and row["start"] > now:
            span = f" ab {when(row['start'], now)}"
        if row["end"]:
            span += f" bis {when(row['end'], now)}"
        part = f"{row['title']}{level}{span}"
        if row["text"] and len(rows) == 1:
            part += f": {row['text'][:160]}"
        elif row["source"] != "DWD":
            part += f" ({row['source']})"
        parts.append(part)
    more = f" Dazu {len(rows) - 3} weitere." if len(rows) > 3 else ""
    return {"text": f"Amtliche Warnungen für {state}: " + "; ".join(parts) + f".{more}{note}", "sources": sources}


# ---------------------------------------------------------------- BVG / S-Bahn disruptions

# Interchanges that together see every S-Bahn and U-Bahn line and many trams and buses.
HUBS = {
    "900100003": "Alexanderplatz", "900003201": "Hauptbahnhof", "900058101": "Südkreuz", "900120003": "Ostkreuz",
    "900007102": "Gesundbrunnen", "900023201": "Zoo", "900100001": "Friedrichstraße",
    "900054105": "Innsbrucker Platz", "900078101": "Hermannplatz", "900056101": "Wittenbergplatz",
}
LINE = re.compile(r"\b(s ?\d{1,2}|u ?\d|m ?\d{1,2}|x ?\d{1,2}|n ?\d{1,3}|re ?\d{1,2}|rb ?\d{1,2}|bus ?\d{1,3}|"
                  r"tram ?\d{1,2}|fex)\b", re.I)


def line_name(text: str) -> str:
    """'s 1' -> 'S1', 'bus 248' -> '248', 'tram 12' -> '12'"""
    text = re.sub(r"\s+", "", text.strip()).upper()
    return re.sub(r"^(BUS|TRAM)", "", text)


def _remark(remark: dict, line: str, limit: int = 120) -> str:
    """The remark without its line prefix, cut after a whole sentence that fits the radio."""
    text = " ".join(str(remark.get("text") or remark.get("summary") or "").split())
    text = re.sub(rf"^(Bus |Tram )?{re.escape(line)}:\s*", "", text)
    text = re.sub(r"\s*(Bitte (benutzen|nutzen|beachten) Sie|Wir bitten um|Weitere Informationen)[^.]*\.?", "", text)
    if len(text) > limit:
        cut = text[:limit].rfind(". ")
        text = text[:cut] if cut > 30 else text[:limit].rsplit(" ", 1)[0] + " …"
    return text.rstrip(".")


def outages(count: int) -> str:
    return f"{count} Fahrt fällt aus" if count == 1 else f"{count} Fahrten fallen aus"


async def disruptions(client: httpx.AsyncClient, base: str, line: str = "", tz: str = "Europe/Berlin") -> dict:
    """Warnings and cancellations of one line, or the disrupted lines across Berlin."""
    base = base.rstrip("/")
    wanted = line_name(line) if line else ""

    async def board(stop: str):
        data = await _json(client, f"{base}/stops/{stop}/departures",
                           {"duration": 60, "results": 80, "remarks": "true", "language": "de"}, limit=3_000_000)
        return data.get("departures", []) if isinstance(data, dict) else data

    boards = await asyncio.gather(*(board(stop) for stop in HUBS), return_exceptions=True)
    if all(isinstance(b, BaseException) for b in boards):
        raise httpx.TransportError("BVG departures unreachable")
    notes: dict[str, dict[str, None]] = {}
    cancelled: dict[str, set] = {}
    seen: set[str] = set()
    for rows in boards:
        for row in rows if isinstance(rows, list) else []:
            info = (row or {}).get("line") or {}
            name = str(info.get("name") or "").strip()
            if info.get("product") in {"express", "national", "nationalExpress"}:
                continue  # long-distance trains are not BVG/S-Bahn
            if not name or (wanted and name.upper() != wanted):
                continue
            seen.add(name)
            if row.get("cancelled"):
                cancelled.setdefault(name, set()).add(str(row.get("tripId") or row.get("plannedWhen")))
            for remark in row.get("remarks") or []:
                if remark.get("type") == "warning" or (
                        remark.get("type") == "status" and "between" in str(remark.get("code") or "")):
                    text = _remark(remark, name)
                    if text:
                        notes.setdefault(name, {})[text] = None
    source = ["https://www.bvg.de/de/verbindungen/stoerungsmeldungen", "https://sbahn.berlin/fahren/bauen-stoerung/"]
    if wanted:
        if wanted not in {s.upper() for s in seen}:
            return {"text": f"Die Linie {wanted} sehe ich in der nächsten Stunde an den großen Umsteigebahnhöfen nicht. "
                            "Für Abfahrten an einer Haltestelle frag z.B. „Abfahrten Rathaus Steglitz“.",
                    "sources": source}
        texts = list((notes.get(next(iter(seen)), {}) if seen else {}).keys())
        gone = len(cancelled.get(next(iter(seen)), set()))
        if not texts and not gone:
            return {"text": f"{wanted}: keine Störung gemeldet, die nächste Stunde läuft laut BVG/VBB normal.",
                    "sources": source}
        parts = texts[:2] + ([outages(gone)] if gone else [])
        return {"text": f"{wanted}: " + ". ".join(parts) + ".", "sources": source}
    lines = sorted(set(notes) | set(cancelled), key=lambda n: (
        0 if n.startswith("S") else 1 if n.startswith("U") else 2, n not in notes, -len(cancelled.get(n, ())), n))
    if not lines:
        return {"text": "Bei S-Bahn, U-Bahn, Tram und Bus in Berlin ist gerade keine Störung gemeldet.",
                "sources": source}
    parts = []
    for name in lines[:4]:
        text = next(iter(notes.get(name, {})), "")
        gone = len(cancelled.get(name, ()))
        parts.append(f"{name}: {text}" if text else f"{name}: {outages(gone)}")
    more = f" Außerdem betroffen: {', '.join(lines[4:9])}." if len(lines) > 4 else ""
    return {"text": "Störungen jetzt: " + "; ".join(parts) + "." + more, "sources": source}


# ---------------------------------------------------------------- space weather (NOAA SWPC)

SWPC = "https://services.swpc.noaa.gov"


def kp_word(kp: float) -> str:
    if kp >= 7:
        return "starker geomagnetischer Sturm"
    if kp >= 5:
        return "geomagnetischer Sturm"
    if kp >= 4:
        return "unruhig"
    return "ruhig"


async def space_weather(client: httpx.AsyncClient, tz: str = "Europe/Berlin", now: datetime | None = None) -> dict:
    """Kp index now and ahead, NOAA scales and solar flux: what shortwave and aurora hunters need."""
    now = now or datetime.now(ZoneInfo(tz))
    forecast, scales, flux = await asyncio.gather(
        _json(client, f"{SWPC}/products/noaa-planetary-k-index-forecast.json"),
        _json(client, f"{SWPC}/products/noaa-scales.json"),
        _json(client, f"{SWPC}/products/summary/10cm-flux.json"), return_exceptions=True)
    if isinstance(forecast, BaseException):
        raise httpx.TransportError("No Kp data")
    rows = []
    for row in forecast if isinstance(forecast, list) else []:
        try:
            moment = datetime.fromisoformat(str(row["time_tag"])).replace(tzinfo=ZoneInfo("UTC"))
            rows.append((moment, float(row["kp"]), str(row.get("observed") or "")))
        except (KeyError, TypeError, ValueError):
            continue
    observed = [r for r in rows if r[2] in {"observed", "estimated"}]
    ahead = [r for r in rows if r[2] == "predicted" and r[0] > now]
    if not observed:
        raise ValueError("No observed Kp")
    current = observed[-1][1]
    day = max(r[1] for r in observed if r[0] >= observed[-1][0] - timedelta(hours=24))
    text = f"Weltraumwetter: Kp jetzt {comma(current)} ({kp_word(current)}), letzte 24 Stunden max. {comma(day)}"
    if ahead:
        peak = max(ahead[:16], key=lambda r: r[1])
        text += f", Vorhersage bis {WEEKDAYS[ahead[:16][-1][0].astimezone(now.tzinfo).weekday()]} max. " \
                f"{comma(peak[1])} ({when(peak[0].astimezone(now.tzinfo), now)})"
    text += "."
    if isinstance(scales, dict) and isinstance(scales.get("0"), dict):
        today = scales["0"]
        g, r, s = ((today.get(k) or {}).get("Scale") or "0" for k in ("G", "R", "S"))
        text += f" NOAA-Skalen G{g} R{r} S{s}"
        tomorrow = scales.get("1") or {}
        minor = (tomorrow.get("R") or {}).get("MinorProb")
        text += f", Funkausfall-Risiko (R1) morgen {minor} %" if minor else ""
        text += "."
    if isinstance(flux, list) and flux and isinstance(flux[0], dict) and flux[0].get("flux"):
        sfi = float(flux[0]["flux"])
        bands = "gut für 10–15 m" if sfi >= 150 else "ordentlich für 15–20 m" if sfi >= 100 else "eher 20–40 m"
        text += f" Solarer Flux {sfi:.0f}, Kurzwelle {bands}."
    if max([current] + [r[1] for r in ahead[:8]]) >= 6:
        text += " Polarlicht in Berlin ist ab Kp 7 möglich, Blick nach Norden."
    return {"text": text, "sources": ["https://www.swpc.noaa.gov/products/planetary-k-index"]}


# ---------------------------------------------------------------- football (OpenLigaDB)

OPENLIGA = "https://api.openligadb.de"
LEAGUES = {"bl1": "Bundesliga", "bl2": "2. Bundesliga", "bl3": "3. Liga"}
TEAMS = {  # spoken names -> a word of the OpenLigaDB team name
    "union": "Union Berlin", "eisern": "Union Berlin", "hertha": "Hertha BSC", "bayern": "Bayern",
    "dortmund": "Dortmund", "bvb": "Dortmund", "schalke": "Schalke", "hsv": "Hamburger SV", "gladbach": "Gladbach",
    "leverkusen": "Leverkusen", "werder": "Werder", "bremen": "Werder", "koeln": "Köln", "fc koeln": "Köln",
    "st. pauli": "St. Pauli", "pauli": "St. Pauli", "frankfurt": "Frankfurt", "eintracht": "Frankfurt",
    "stuttgart": "Stuttgart", "vfb": "Stuttgart", "wolfsburg": "Wolfsburg", "freiburg": "Freiburg",
    "leipzig": "Leipzig", "rb": "Leipzig", "mainz": "Mainz", "augsburg": "Augsburg", "hoffenheim": "Hoffenheim",
    "heidenheim": "Heidenheim", "energie": "Cottbus", "cottbus": "Cottbus", "babelsberg": "Babelsberg",
}


def season(today: date) -> int:
    return today.year if today.month >= 7 else today.year - 1


def _result(match: dict) -> tuple[int, int] | None:
    results = match.get("matchResults") or []
    final = next((r for r in results if r.get("resultTypeID") == 2), None) or (results[-1] if results else None)
    if not final:
        return None
    return int(final.get("pointsTeam1") or 0), int(final.get("pointsTeam2") or 0)


def _kickoff(match: dict, tz: ZoneInfo) -> datetime | None:
    try:
        return datetime.fromisoformat(str(match["matchDateTimeUTC"]).replace("Z", "+00:00")).astimezone(tz)
    except (KeyError, ValueError):
        return None


def _team(match: dict, side: str) -> str:
    team = match.get(side) or {}
    return str(team.get("shortName") or team.get("teamName") or "?")


async def football(client: httpx.AsyncClient, team: str = "", league: str = "", tz: str = "Europe/Berlin",
                   now: datetime | None = None) -> dict:
    """Table position, last result and next match of a team, or the table top and the current matchday."""
    zone = ZoneInfo(tz)
    now = now or datetime.now(zone)
    year = season(now.date())
    leagues = [league] if league in LEAGUES else ["bl1", "bl2", "bl3"]
    wanted = TEAMS.get(fold(team), team.strip()) if team else ""
    if wanted:
        tables = await asyncio.gather(*(_json(client, f"{OPENLIGA}/getbltable/{code}/{year}") for code in leagues),
                                      return_exceptions=True)
        for code, table in zip(leagues, tables, strict=True):
            if isinstance(table, BaseException) or not isinstance(table, list):
                continue
            for place, row in enumerate(table, 1):
                names = fold(f"{row.get('teamName', '')} {row.get('shortName', '')}")
                if fold(wanted) in names:
                    return await _team_text(client, row, place, code, year, zone, now)
        return {"text": f"„{team}“ finde ich in Bundesliga, 2. Liga und 3. Liga nicht.", "sources": []}
    code = leagues[0]
    table, matches = await asyncio.gather(_json(client, f"{OPENLIGA}/getbltable/{code}/{year}"),
                                          _json(client, f"{OPENLIGA}/getmatchdata/{code}"))
    top = ", ".join(f"{i}. {r.get('shortName') or r.get('teamName')} {r.get('points')}" for i, r in
                    enumerate(table[:5], 1)) if isinstance(table, list) else ""
    played, coming = [], []
    for match in matches if isinstance(matches, list) else []:
        score = _result(match)
        kickoff = _kickoff(match, zone)
        if match.get("matchIsFinished") and score:
            played.append(f"{_team(match, 'team1')}–{_team(match, 'team2')} {score[0]}:{score[1]}")
        elif kickoff:
            live = f" läuft {score[0]}:{score[1]}" if score and kickoff <= now else ""
            coming.append(f"{_team(match, 'team1')}–{_team(match, 'team2')} {when(kickoff, now)}{live}")
    group = ((matches or [{}])[0].get("group") or {}).get("groupName", "") if isinstance(matches, list) else ""
    text = f"{LEAGUES[code]}, Tabelle: {top}."
    if played:
        text += f" {group}: " + ", ".join(played[:4]) + (" und mehr" if len(played) > 4 else "") + "."
    if coming:
        text += " Noch offen: " + ", ".join(coming[:3]) + "."
    return {"text": text, "sources": [f"https://www.openligadb.de/{code}/{year}"]}


async def _team_text(client, row: dict, place: int, code: str, year: int, zone: ZoneInfo, now: datetime) -> dict:
    name = row.get("shortName") or row.get("teamName")
    text = (f"{row.get('teamName') or name}: Platz {place} in der {LEAGUES[code]}, {row.get('points')} Punkte aus "
            f"{row.get('matches')} Spielen ({row.get('won')} S, {row.get('draw')} U, {row.get('lost')} N, "
            f"Tore {row.get('goals')}:{row.get('opponentGoals')}).")
    try:
        matches = await _json(client, f"{OPENLIGA}/getmatchesbyteamid/{int(row['teamInfoId'])}/3/3")
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        matches = []
    matches = [m for m in matches if isinstance(m, dict)] if isinstance(matches, list) else []
    matches.sort(key=lambda m: str(m.get("matchDateTimeUTC") or ""))
    done = [m for m in matches if m.get("matchIsFinished") and _result(m)]
    # Unfinished entries from the past are stale data, not the next match.
    ahead = [m for m in matches if not m.get("matchIsFinished") and _kickoff(m, zone)
             and _kickoff(m, zone) > now - timedelta(hours=3)]
    if done:
        last = done[-1]
        score = _result(last)
        text += (f" Zuletzt {_team(last, 'team1')}–{_team(last, 'team2')} {score[0]}:{score[1]}"
                 f" ({_kickoff(last, zone):%d.%m.}).")
    if ahead:
        nxt = ahead[0]
        kickoff = _kickoff(nxt, zone)
        live = _result(nxt)
        status = f" läuft gerade, {live[0]}:{live[1]}" if live and kickoff <= now else f" {when(kickoff, now)}"
        text += f" Nächstes Spiel: {_team(nxt, 'team1')}–{_team(nxt, 'team2')}{status}."
    return {"text": text, "sources": [f"https://www.openligadb.de/{code}/{year}"]}


# ---------------------------------------------------------------- air quality and pollen

AIR = "https://air-quality-api.open-meteo.com/v1/air-quality"
POLLEN = "https://opendata.dwd.de/climate_environment/health/alerts/s31fg.json"
POLLEN_NAMES = {"Erle": "Erle", "Hasel": "Hasel", "Birke": "Birke", "Graeser": "Gräser", "Beifuss": "Beifuß",
                "Ambrosia": "Ambrosia", "Roggen": "Roggen", "Esche": "Esche"}
POLLEN_LEVELS = {"0": 0, "0-1": 0.5, "1": 1, "1-2": 1.5, "2": 2, "2-3": 2.5, "3": 3}
POLLEN_WORDS = {0.5: "kaum", 1: "gering", 1.5: "gering bis mittel", 2: "mittel", 2.5: "mittel bis hoch", 3: "hoch"}


def aqi_word(aqi: float) -> str:
    for limit, word in ((20, "gut"), (40, "ordentlich"), (60, "mäßig"), (80, "schlecht"), (100, "sehr schlecht")):
        if aqi <= limit:
            return word
    return "extrem schlecht"


async def air_quality(client: httpx.AsyncClient, lat: float, lon: float, place: str = "Berlin") -> dict:
    data = await _json(client, AIR, {"latitude": lat, "longitude": lon, "timezone": "Europe/Berlin",
                                     "current": "european_aqi,pm10,pm2_5,nitrogen_dioxide,ozone",
                                     "hourly": "european_aqi", "forecast_days": 2})
    current = data.get("current") or {}
    aqi = current.get("european_aqi")
    if aqi is None:
        raise ValueError("No air quality value")
    text = (f"Luft in {place}: {aqi_word(float(aqi))} (Europäischer Luftqualitätsindex {aqi:.0f}), "
            f"Feinstaub PM10 {current.get('pm10', 0):.0f} und PM2,5 {current.get('pm2_5', 0):.0f} µg/m³, "
            f"Stickstoffdioxid {current.get('nitrogen_dioxide', 0):.0f} µg/m³, Ozon {current.get('ozone', 0):.0f} µg/m³.")
    hourly = data.get("hourly") or {}
    values = [v for v in (hourly.get("european_aqi") or [])[:48] if isinstance(v, (int, float))]
    if values and max(values) > max(40, float(aqi) + 15):
        text += f" In den nächsten 48 Stunden bis zu {max(values):.0f} ({aqi_word(max(values))})."
    return {"text": text + " Modellwerte (CAMS), keine Messstation.", "sources": ["https://open-meteo.com/en/docs/air-quality-api"]}


async def pollen(client: httpx.AsyncClient, region: int = 50) -> dict:
    data = await _json(client, POLLEN)
    row = next((c for c in data.get("content") or [] if c.get("region_id") == region and c.get("partregion_id") in
                (-1, None)), None) or next((c for c in data.get("content") or [] if c.get("region_id") == region), None)
    if not row:
        raise ValueError("No pollen region")
    days = {"today": "heute", "tomorrow": "morgen", "dayafter_to": "übermorgen"}
    parts = []
    for key, word in days.items():
        active = sorted(((POLLEN_LEVELS.get(str(v.get(key)), 0), POLLEN_NAMES.get(name, name))
                         for name, v in (row.get("Pollen") or {}).items()), reverse=True)
        active = [(level, name) for level, name in active if level > 0]
        if active:
            parts.append(f"{word} " + ", ".join(f"{name} {POLLEN_WORDS.get(level, '')}" for level, name in active[:4]))
    region_name = row.get("partregion_name") or row.get("region_name") or "Berlin"
    if not parts:
        return {"text": f"Pollenflug {region_name}: heute bis übermorgen keine Belastung (DWD).",
                "sources": ["https://www.dwd.de/pollenflug"]}
    return {"text": f"Pollenflug {region_name} (DWD): " + "; ".join(parts) + ".",
            "sources": ["https://www.dwd.de/pollenflug"]}


# ---------------------------------------------------------------- holidays

HOLIDAYS = "https://openholidaysapi.org/{kind}"


async def holidays(client: httpx.AsyncClient, kind: str = "", day: str = "", tz: str = "Europe/Berlin",
                   today: date | None = None) -> dict:
    """Public holidays and school holidays in Berlin. kind: 'feiertag', 'ferien' or both; day: 'heute'/'morgen'."""
    today = today or datetime.now(ZoneInfo(tz)).date()
    params = {"countryIsoCode": "DE", "subdivisionCode": "DE-BE", "languageIsoCode": "DE",
              "validFrom": f"{today - timedelta(days=40):%Y-%m-%d}", "validTo": f"{today + timedelta(days=400):%Y-%m-%d}"}
    want_public = kind != "ferien"
    want_school = kind != "feiertag"
    public, school = await asyncio.gather(
        _json(client, HOLIDAYS.format(kind="PublicHolidays"), params) if want_public else asyncio.sleep(0, []),
        _json(client, HOLIDAYS.format(kind="SchoolHolidays"), params) if want_school else asyncio.sleep(0, []))

    def rows(items):
        result = []
        for item in items if isinstance(items, list) else []:
            try:
                name = next((n["text"] for n in item.get("name") or [] if n.get("language") == "DE"), "")
                result.append((date.fromisoformat(item["startDate"]), date.fromisoformat(item["endDate"]), name))
            except (KeyError, ValueError, TypeError):
                continue
        return sorted(result)

    parts = []
    if want_public:
        free = rows(public)
        target = today + timedelta(days={"heute": 0, "morgen": 1, "uebermorgen": 2, "übermorgen": 2}.get(day, 0))
        if day:
            hit = next((n for s, e, n in free if s <= target <= e), "")
            parts.append(f"{day_name(target, today).split(',')[0].capitalize()} ist in Berlin "
                         + (f"Feiertag: {hit}." if hit else "kein Feiertag."))
        upcoming = [(s, n) for s, _, n in free if s > (target if day else today - timedelta(days=1))]
        if upcoming:
            first = upcoming[0]
            gap = (first[0] - today).days
            text = f"Nächster Feiertag in Berlin: {first[1]}, {day_name(first[0], today)}"
            text += f" (in {gap} Tagen)" if gap > 2 else ""
            if len(upcoming) > 1:
                text += f", danach {upcoming[1][1]} ({upcoming[1][0]:%d.%m.})"
            parts.append(text + ".")
    if want_school:
        breaks = [(s, e, n) for s, e, n in rows(school) if e >= today]
        now_on = next(((s, e, n) for s, e, n in breaks if s <= today <= e), None)
        if now_on:
            parts.append(f"Gerade sind {now_on[2]} in Berlin, bis {now_on[1]:%d.%m.%Y}.")
        nxt = [(s, e, n) for s, e, n in breaks if s > today and "unterrichtsfrei" not in n.casefold()]
        if nxt:
            s, e, n = nxt[0]
            span = f"am {s:%d.%m.%Y}" if s == e else f"vom {s:%d.%m.} bis {e:%d.%m.%Y}"
            parts.append(f"Nächste Schulferien: {n} {span}.")
    if not parts:
        raise ValueError("No holiday data")
    return {"text": " ".join(parts), "sources": ["https://www.openholidaysapi.org/"]}


# ---------------------------------------------------------------- fuel prices (Tankerkönig)

TANKERKOENIG = "https://creativecommons.tankerkoenig.de/json/list.php"
FUEL_KEY = "MESHCORE_TANKERKOENIG_KEY"
FUELS = {"diesel": "Diesel", "e5": "Super E5", "e10": "Super E10"}


def euro(price: float) -> str:
    return f"{price:.3f} €".replace(".", ",")


async def fuel_prices(client: httpx.AsyncClient, lat: float, lon: float, fuel: str = "", radius: float = 4.0,
                      place: str = "") -> dict:
    key = env_value(FUEL_KEY)
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", key):
        return {"text": "Spritpreise sind noch nicht eingerichtet: Dafür braucht der Pi einen kostenlosen "
                        "Tankerkönig-Schlüssel (creativecommons.tankerkoenig.de) in MESHCORE_TANKERKOENIG_KEY.",
                "sources": []}
    kinds = [fuel] if fuel in FUELS else list(FUELS)
    data = await _json(client, TANKERKOENIG, {"lat": f"{lat:.4f}", "lng": f"{lon:.4f}", "rad": radius,
                                              "sort": "dist", "type": "all", "apikey": key})
    if not data.get("ok"):
        raise ValueError(str(data.get("message") or "Tankerkönig error"))
    stations = [s for s in data.get("stations") or [] if s.get("isOpen")]
    if not stations:
        return {"text": f"Im Umkreis von {radius:.0f} km hat gerade keine Tankstelle geöffnet.", "sources": []}
    parts = []
    for kind in kinds:
        priced = sorted((s for s in stations if isinstance(s.get(kind), (int, float)) and s[kind] > 0),
                        key=lambda s: (s[kind], s.get("dist", 99)))
        if not priced:
            continue
        best = priced[0]
        name = " ".join(str(best.get(k) or "") for k in ("brand", "street", "houseNumber")).strip()
        cheapest = f"{FUELS[kind]} ab {euro(best[kind])}"
        if len(kinds) == 1:
            others = "; ".join(f"{str(s.get('brand') or s.get('name'))} {s.get('street', '')} {euro(s[kind])}"
                               for s in priced[1:3])
            parts.append(f"{cheapest} bei {name} ({comma(best.get('dist', 0))} km)" + (f"; {others}" if others else ""))
        else:
            parts.append(f"{cheapest} ({name}, {comma(best.get('dist', 0))} km)")
    if not parts:
        return {"text": "Für diese Sorte meldet gerade keine offene Tankstelle in der Nähe einen Preis.", "sources": []}
    where = f" bei {place}" if place else ""
    return {"text": f"Spritpreise{where}, günstigste offene Tankstellen im Umkreis {radius:.0f} km: "
                    + ". ".join(parts) + ".", "sources": ["https://creativecommons.tankerkoenig.de"]}


# ---------------------------------------------------------------- routing

ALLTAG_TOOLS = ("get_warnings", "get_transit_disruptions", "get_space_weather", "get_football", "get_air_quality",
                "get_pollen", "get_holidays", "get_fuel_prices")

WARN = re.compile(r"\b(unwetter\w*|warnung\w*|wetterwarnung\w*|amtliche warnung\w*|katastrophen\w*|nina|"
                  r"gewitterwarnung|sturmwarnung|glaettewarnung|hitzewarnung|frostwarnung|hochwasser\w*|"
                  r"gibt es (eine |irgendwelche )?warnung\w*|warnt (der )?dwd)\b")
DISRUPT = re.compile(r"\b(stoerung\w*|gestoert|baustell\w*|bauarbeiten|ersatzverkehr|sev|faellt aus|fallen aus|faellt .{0,25}\baus|fallen .{0,25}\baus|"
                     r"ausfall\w*|verspaetung\w*|unterbrochen|sperrung|gesperrt|streik\w*)\b")
RUNS = re.compile(r"^(faehrt|fahren|geht|laeuft|kommt) (die |der |heute |gerade |jetzt )*("
                  r"s ?\d{1,2}|u ?\d|m ?\d{1,2}|ringbahn|s ?bahn|u ?bahn|tram|bus)\b")
SPACE = re.compile(r"\b(kp[ -]?index|kp[ -]?wert|weltraumwetter|space ?weather|sonnensturm|sonnenstuerme|"
                   r"geomagnetisch\w*|magnetsturm|polarlicht\w*|nordlicht\w*|aurora|sonnenaktivitaet|sonnenwind|"
                   r"solar ?flux|sfi|sonnenflecken|flares?|funkausfall|band ?bedingungen|bandbedingungen|"
                   r"kurzwellen?(bedingungen|ausbreitung)|condx)\b")
FOOT = re.compile(r"\b(bundesliga|2\. liga|zweite liga|3\. liga|dritte liga|tabelle\w*|spieltag|fussball\w*|"
                  r"fuszball|tor(e|stand)|anstoss|spielt (union|hertha|bayern|der bvb|dortmund)|"
                  r"(union|hertha|bayern|bvb|dortmund|schalke|hsv|werder|gladbach|leverkusen|cottbus)( berlin| bsc)?"
                  r" (gespielt|gewonnen|verloren|ergebnis|spiel\w*|tabelle|platz)|"
                  r"(wie hat|hat|wann spielt) (der |die )?(union|hertha|bayern|bvb|dortmund|schalke|hsv|werder|"
                  r"gladbach|leverkusen|eintracht|stuttgart|cottbus))\b")
AIRQ = re.compile(r"\b(luftqualitaet|luftguete|feinstaub|smog|stickstoffdioxid|stickoxid\w*|ozon(werte?|wert)?|"
                  r"luft (gut|schlecht|sauber|dreckig)|wie ist die luft|luftverschmutzung|pm ?10|pm ?2.?5)\b")
POLL = re.compile(r"\b(pollen\w*|heuschnupfen|pollenflug|birkenpollen|graeserpollen|allergie\w*)\b")
HOLI = re.compile(r"\b(feiertag\w*|ferien|schulferien|herbstferien|winterferien|osterferien|sommerferien|"
                  r"pfingstferien|weihnachtsferien|brueckentag\w*|schulfrei)\b")
FUEL = re.compile(r"\b(sprit\w*|benzin\w*|diesel\w*|tanken|tankstelle\w*|super ?e ?(5|10)|e10|e5)\b")


def alltag_request(question: str) -> tuple[str, dict] | None:
    """(tool, args) for everyday questions the data answers directly, else None."""
    folded = fold(question)
    # Explanations ('Was ist ein Kp-Index?') belong to the model, not to today's numbers.
    if not folded or re.match(r"(was (ist|sind|bedeutet|bedeuten|heisst) (ein|eine|der|die|das)?\b|erklaer|"
                              r"wie (funktioniert|entsteh|entstand)|warum|wieso|weshalb)", folded):
        return None
    if SPACE.search(folded):
        return "get_space_weather", {}
    if POLL.search(folded):
        return "get_pollen", {}
    if AIRQ.search(folded):
        return "get_air_quality", {}
    if FUEL.search(folded) and not re.search(r"\b(aktie|kurs|oelpreis|brent|wti)\b", folded):
        fuel = "diesel" if "diesel" in folded else "e10" if "e10" in folded else "e5" if re.search(
            r"\be5\b|super(?! ?e ?10)|benzin", folded) else ""
        return "get_fuel_prices", {"fuel": fuel}
    if HOLI.search(folded):
        kind = "ferien" if re.search(r"ferien|schulfrei", folded) and "feiertag" not in folded else (
            "feiertag" if re.search(r"feiertag|brueckentag", folded) else "")
        day = next((d for d in ("uebermorgen", "morgen", "heute") if re.search(rf"\b{d}\b", folded)), "")
        return "get_holidays", {"kind": kind, "day": day if kind != "ferien" else ""}
    team = next((name for name in TEAMS if re.search(rf"\b{re.escape(name)}\b", folded)), "")
    played = re.search(r"\b(wann spielt|wie (hat|haben|spielt|spielen|steht)|gespielt|gewonnen|verloren|unentschieden|"
                       r"ergebnis|tabellenplatz|naechstes spiel|letztes spiel)\b", folded)
    if FOOT.search(folded) or (team and played):
        league = "bl2" if re.search(r"2\. liga|zweite liga|2\. bundesliga", folded) else (
            "bl3" if re.search(r"3\. liga|dritte liga", folded) else "")
        return "get_football", {"team": team, "league": league}
    if WARN.search(folded) and not re.search(r"\b(kursalarm|alarm \w+ (unter|ueber))\b", folded):
        return "get_warnings", {}
    line = LINE.search(question)
    if DISRUPT.search(folded) or RUNS.search(folded):
        if line or re.search(r"\b(bvg|s ?bahn|u ?bahn|tram|bus|bahn|oepnv|nahverkehr|ringbahn|linie)\b", folded):
            name = line_name(line.group(0)) if line else ("S41" if "ringbahn" in folded else "")
            return "get_transit_disruptions", {"line": name}
    return None
