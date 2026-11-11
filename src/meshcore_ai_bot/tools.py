"""Read-only tool boundary: no radio commands, shell, file writes or analyzer scraping."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .alltag import (
    ALLTAG_TOOLS,
    air_quality,
    disruptions,
    football,
    fuel_prices,
    holidays,
    pollen,
    space_weather,
    warnings,
)
from .config import Config
from .fundamentals import fundamentals, wiki
from .http import read_json
from .insider import insider_trades
from .markets import (
    SPLIT,
    crypto_mood,
    dax_movers,
    headlines,
    market_answer,
    market_overview,
    number,
    percent,
    quote,
    resolve,
    unit,
)
from .research import Research, ber_flights, grill_stands, planes_overhead
from .store import Store


def norm(text: str) -> str:
    text = text.casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text).replace("ae", "a").replace("oe", "o").replace("ue", "u")
    return "".join(c for c in text if c.isalnum())


def tool(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


SCHEMAS = [
    tool(
        "get_weather",
        "Aktuelles Wetter oder Vorhersage lesen. Kein Wetter erfinden.",
        {
            "location": {"type": "string"},
            "period": {
                "type": "string",
                "description": "now, today, tomorrow, day_after_tomorrow oder YYYY-MM-DD",
            },
        },
        ["location", "period"],
    ),
    tool(
        "get_repeater_status",
        "Lokale Daten eines benannten Repeaters lesen; alte Adverts beweisen keinen Ausfall.",
        {"name": {"type": "string"}},
        ["name"],
    ),
    tool(
        "list_repeaters",
        "Bekannte Repeater nach Namen/Ort filtern; höchstens fünf Beispiele, keine Online-Garantie.",
        {"query": {"type": "string"}},
        [],
    ),
    tool(
        "find_local_places",
        "Live-Suche nach lokalen Orten. Nur bei Empfehlungen oder konkreten Orten nutzen, nie Ergebnisse erfinden.",
        {"query": {"type": "string"}, "location": {"type": "string"}},
        ["query", "location"],
    ),
]

RESEARCH_SCHEMAS = {
    "web_search": tool(
        "web_search",
        "Allgemeine Websuche (lokale Metasuche). Für Fakten, Termine, Preise, Produkte, Vereine, Öffnungszeiten "
        "und alles Aktuelle. Kurze Suchbegriffe auf Deutsch.",
        {"query": {"type": "string"}},
        ["query"],
    ),
    "search_news": tool(
        "search_news",
        "Aktuelle Nachrichten von tagesschau und rbb24 suchen. Leere Suche liefert die Top-Meldungen; "
        "'Berlin' oder 'Brandenburg' liefert regionale Meldungen.",
        {"query": {"type": "string"}},
        [],
    ),
    "search_wikipedia": tool(
        "search_wikipedia",
        "Wikipedia-Artikel suchen und die Kurzfassung lesen. Für Begriffe, Personen, Orte, Geschichte und Technik.",
        {"query": {"type": "string"}},
        ["query"],
    ),
    "get_departures": tool(
        "get_departures",
        "Nächste Abfahrten (Echtzeit) an einer Haltestelle oder einem Bahnhof in Berlin/Brandenburg, auch DB-Züge "
        "(ICE, IC, RE, RB). Für Fragen nach Zügen trains='ja' setzen, dann nur Bahn mit Gleis.",
        {
            "stop": {"type": "string", "description": "Haltestellenname, z.B. 'S Friedenau' oder 'Berlin Hbf'"},
            "trains": {"type": "string", "description": "'ja' für nur Züge (Fern- und Regionalverkehr)"},
        },
        ["stop"],
    ),
    "get_journey": tool(
        "get_journey",
        "Verbindungen mit Bus und Bahn in Berlin/Brandenburg von A nach B (wie eine Fahrplan-App), Echtzeit.",
        {"origin": {"type": "string", "description": "Start-Haltestelle"},
         "destination": {"type": "string", "description": "Ziel-Haltestelle"},
         "when": {"type": "string", "description": "optional Ortszeit YYYY-MM-DDTHH:MM"},
         "arrive": {"type": "string", "description": "'ja', wenn when die gewünschte Ankunftszeit ist"},
         "only": {"type": "string", "description": "optional nur ein Verkehrsmittel: bus, tram, subway, suburban"},
         "direct": {"type": "string", "description": "'ja' für Verbindungen ohne Umsteigen"}},
        ["origin", "destination"],
    ),
    "get_flights": tool(
        "get_flights",
        "Live-Abflüge oder -Ankünfte am Flughafen BER, optional gefiltert nach Flugnummer, Ziel oder Airline.",
        {
            "direction": {"type": "string", "description": "'abflug' oder 'ankunft'"},
            "query": {"type": "string", "description": "Flugnummer, Stadt oder Airline, leer für die nächsten Flüge"},
        },
        [],
    ),
    "get_planes_overhead": tool(
        "get_planes_overhead",
        "Welche Flugzeuge gerade in der Nähe der Station (oder mit area='berlin' über ganz Berlin) in der Luft sind.",
        {"area": {"type": "string", "description": "'berlin' für ganz Berlin, 'rettung' nur Rettungshubschrauber, leer für die Station"}},
        [],
    ),
    "get_grill_stands": tool(
        "get_grill_stands",
        "Wo und wann die GaumenSchmaus-Hähnchenwagen (Grillhähnchen) stehen, nächste zuerst, live vom Hähnchen-Radar.",
        {"day": {"type": "string", "description": "heute, morgen, übermorgen, ein Wochentag oder leer für den nächsten Termin"},
         "place": {"type": "string", "description": "optional Markt, Straße oder PLZ, z.B. 'Schlangenbader'"},
         "near": {"type": "string", "description": "optional Ortsteil oder Ort, um den gesucht wird, z.B. 'Steglitz'"}},
        [],
    ),
    "get_quote": tool(
        "get_quote",
        "Live-Kurs mit Tageshoch/-tief, 52-Wochen-Spanne, 1-Jahres-Entwicklung und regelbasierter Einschätzung "
        "für Aktien, ETFs, Indizes, Krypto, Rohstoffe und Währungen. Leer für einen Marktüberblick.",
        {"asset": {"type": "string", "description": "Name, Kürzel oder ISIN, bis zu drei mit Komma, z.B. 'SAP, Bitcoin'"}},
        [],
    ),
    "get_fundamentals": tool(
        "get_fundamentals",
        "Kennzahlen einer Firma: Börsenwert, KGV, Dividende (mit letzter Zahlung), Umsatz mit Wachstum, Gewinn, "
        "Cash und Schulden. US-Firmen aus den SEC-Berichten (letzte 12 Monate), andere wie DAX-Werte als "
        "Jahreszahlen aus Wikidata. Mit about='ja' stattdessen das Firmenprofil: Chef, Sitz, Branche, "
        "Gründungsjahr, ISIN, Webseite.",
        {"asset": {"type": "string", "description": "Name, Kürzel oder ISIN, bis zu zwei mit Komma, z.B. 'Nvidia'"},
         "about": {"type": "string", "description": "'ja' für das Firmenprofil statt der Kennzahlen"}},
        ["asset"],
    ),
    "get_insider_trades": tool(
        "get_insider_trades",
        "Insider-Geschäfte (Directors' Dealings): Käufe und Verkäufe von Vorstand und Aufsichtsrat. Ohne asset die "
        "heute bei der BaFin veröffentlichten Meldungen, mit asset die letzten Geschäfte dieser Firma "
        "(deutsche Firmen BaFin, US-Firmen SEC Form 4).",
        {"asset": {"type": "string", "description": "Name, Kürzel oder ISIN einer Firma, leer für alle von heute"}},
        [],
    ),
    "get_warnings": tool(
        "get_warnings",
        "Amtliche Warnungen für Berlin jetzt und angekündigt: Unwetter, Frost, Glätte, Hitze (DWD) sowie "
        "Katastrophen- und Hochwasserwarnungen (NINA).",
        {},
        [],
    ),
    "get_transit_disruptions": tool(
        "get_transit_disruptions",
        "Störungen, Bauarbeiten und Ausfälle bei S-Bahn, U-Bahn, Tram und Bus in Berlin (BVG/VBB). "
        "Mit line nur diese Linie, z.B. 'S1', 'U7', 'M41'; ohne line alle betroffenen Linien.",
        {"line": {"type": "string", "description": "Linie, z.B. 'S1'; leer für alle"}},
        [],
    ),
    "get_space_weather": tool(
        "get_space_weather",
        "Weltraumwetter für Funk und Polarlicht: Kp-Index jetzt und Vorhersage, NOAA-Skalen (G/R/S), solarer Flux "
        "und was das für die Kurzwelle heißt.",
        {},
        [],
    ),
    "get_football": tool(
        "get_football",
        "Fußball: Tabellenplatz, letztes Ergebnis und nächstes Spiel eines Vereins (Bundesliga, 2. und 3. Liga), "
        "ohne team die Tabellenspitze und den aktuellen Spieltag.",
        {"team": {"type": "string", "description": "Verein, z.B. 'Union', 'Hertha'; leer für die Liga"},
         "league": {"type": "string", "description": "'bl1', 'bl2' oder 'bl3'; leer für automatisch"}},
        [],
    ),
    "get_air_quality": tool(
        "get_air_quality",
        "Luftqualität: Europäischer Luftqualitätsindex, Feinstaub, Stickstoffdioxid und Ozon (Modellwerte).",
        {"place": {"type": "string", "description": "Ort, Ortsteil oder PLZ; leer für hier"}},
        [],
    ),
    "get_pollen": tool(
        "get_pollen",
        "Pollenflug in Berlin und Brandenburg für heute bis übermorgen (DWD-Gefahrenindex).",
        {},
        [],
    ),
    "get_holidays": tool(
        "get_holidays",
        "Gesetzliche Feiertage und Schulferien in Berlin.",
        {"kind": {"type": "string", "description": "'feiertag', 'ferien' oder leer für beides"},
         "day": {"type": "string", "description": "'heute', 'morgen' oder 'uebermorgen' für Ist-heute-Feiertag-Fragen"}},
        [],
    ),
    "get_fuel_prices": tool(
        "get_fuel_prices",
        "Aktuelle Spritpreise der günstigsten offenen Tankstellen in der Nähe (Tankerkönig).",
        {"fuel": {"type": "string", "description": "'diesel', 'e5', 'e10' oder leer für alle"},
         "place": {"type": "string", "description": "Ort, Ortsteil oder PLZ in Deutschland; leer für hier"}},
        [],
    ),
    "get_market_news": tool(
        "get_market_news",
        "Aktuelle Schlagzeilen zu einer Aktie, einem Coin oder Rohstoff (tagesschau, Yahoo Finance) plus Kurs. "
        "Für Fragen wie 'Warum fällt Rheinmetall?'.",
        {"asset": {"type": "string", "description": "Name, Kürzel oder ISIN, z.B. 'Rheinmetall'"}},
        ["asset"],
    ),
    "read_webpage": tool(
        "read_webpage",
        "Text einer Webseite lesen, deren HTTPS-Adresse aus einem Suchergebnis oder der Frage stammt.",
        {"url": {"type": "string"}},
        ["url"],
    ),
}


def schemas(config: Config) -> list[dict]:
    """The tools the model may call with this configuration."""
    result = list(SCHEMAS)
    research = config.research
    if research.enabled:
        if research.searxng_url:
            result.append(RESEARCH_SCHEMAS["web_search"])
        result += [RESEARCH_SCHEMAS["search_news"], RESEARCH_SCHEMAS["search_wikipedia"]]
        if research.transit_url:
            result += [RESEARCH_SCHEMAS["get_departures"], RESEARCH_SCHEMAS["get_journey"]]
        result += [RESEARCH_SCHEMAS["get_flights"], RESEARCH_SCHEMAS["get_planes_overhead"],
                   RESEARCH_SCHEMAS["get_grill_stands"], RESEARCH_SCHEMAS["get_quote"],
                   RESEARCH_SCHEMAS["get_fundamentals"], RESEARCH_SCHEMAS["get_insider_trades"],
                   RESEARCH_SCHEMAS["get_market_news"]]
        result += [RESEARCH_SCHEMAS[name] for name in ALLTAG_TOOLS
                   if name != "get_transit_disruptions" or research.transit_url]
        if research.read_pages:
            result.append(RESEARCH_SCHEMAS["read_webpage"])
    return result


class Tools:
    def __init__(self, config: Config, store: Store, client: httpx.AsyncClient, *, offline: bool = False):
        self.config, self.store, self.client, self.offline = config, store, client, offline
        self.cache: dict[str, tuple[float, dict]] = {}
        self.research = Research(config, client)
        self.last_good: dict[str, tuple[float, dict]] = {}

    def last_known(self, kind: str, max_age: float = 12 * 3600) -> dict | None:
        """Last successful news or weather result, for answers while the Internet is gone."""
        known = self.last_good.get(kind)
        if not known or datetime.now(UTC).timestamp() - known[0] > max_age:
            return None
        stamp = datetime.fromtimestamp(known[0], ZoneInfo(self.config.station.timezone)).strftime("%H:%M")
        return {"text": f"Stand {stamp}: " + known[1]["text"], "sources": known[1].get("sources", [])}

    async def _remember(self, kind: str, fetch) -> dict:
        try:
            result = await fetch()
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            result = None
        # News sources report an outage as text without sources instead of raising.
        if result is None or (kind == "news" and not result.get("sources")):
            stale = self.last_known(kind)
            if stale is None:
                if result is None:
                    raise httpx.TransportError("Source unreachable")
                return result
            stale["text"] = "Gerade keine Verbindung. " + stale["text"]
            return stale
        if result.get("sources"):
            self.last_good[kind] = (datetime.now(UTC).timestamp(), result)
        return result

    async def market_news(self, asset: str) -> dict:
        tz = self.config.station.timezone
        found = await resolve(self.client, asset)
        if not found:
            return {"text": f"Zu „{asset}“ finde ich keinen Wert.", "sources": []}
        symbol, label = found
        try:
            data = await quote(self.client, symbol, label)
            line = f"{label} {number(data['price'], data['kind'])}{unit(data)} {percent(data['change'])}.".replace(" .", ".")
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            line = ""
        news = self.research.news if self.config.research.enabled and not self.offline else None
        lines = await headlines(self.client, symbol, label, tz, news)
        text = (line + " Schlagzeilen: " + " | ".join(lines)) if lines else (
            line + f" Keine aktuellen Schlagzeilen zu {label} gefunden.")
        return {"text": text.strip(), "sources": ["https://finance.yahoo.com/quote/" + symbol], "headlines": lines,
                "label": label}

    async def _place(self, name: str, *, anywhere: bool = False) -> tuple[float, float, str] | None:
        """Postcode or place name -> coordinates; Berlin/Brandenburg first, with anywhere=True all of Germany."""
        postcode = re.fullmatch(r"\s*(?:plz\s*)?(\d{5})\s*", name, re.I)
        if postcode:
            try:
                data = await read_json(self.client, "GET", f"https://api.zippopotam.us/de/{postcode.group(1)}",
                                       timeout=10, limit=200_000)
            except httpx.HTTPStatusError as error:
                if error.response.status_code == 404:
                    return None
                raise
            row = (data.get("places") or [None])[0]
            if not row:
                return None
            return float(row["latitude"]), float(row["longitude"]), f"{postcode.group(1)} {row.get('place name', '')}"\
                .strip()
        if not anywhere:
            return await self._berlin_place(name)
        # All of Germany: the best-known place of that name wins (Open-Meteo ranks by size), e.g. München.
        data = await read_json(
            self.client, "GET", "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": name.strip(), "count": 1, "language": "de", "countryCode": "DE"}, timeout=10,
        )
        row = (data.get("results") or [None])[0]
        return (row["latitude"], row["longitude"], row.get("name") or name.strip()) if row else None

    async def _berlin_place(self, name: str) -> tuple[float, float, str] | None:
        """A district or town in Berlin/Brandenburg, e.g. 'Steglitz' -> its coordinates."""
        data = await read_json(
            self.client, "GET", "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": name.strip(), "count": 10, "language": "de", "countryCode": "DE"}, timeout=10,
        )
        for row in data.get("results") or []:
            if row.get("admin1") in {"Berlin", "Brandenburg", "Land Berlin"}:
                return row["latitude"], row["longitude"], row.get("name") or name.strip()
        return None

    async def run(self, name: str, arguments: dict, *, allowed_urls: set[str] | None = None) -> dict:
        allowed = {
            "get_weather": {"location", "period"},
            "get_repeater_status": {"name"},
            "list_repeaters": {"query"},
            "find_local_places": {"query", "location"},
        }
        if self.config.research.enabled and not self.offline:
            allowed.update(search_news={"query"}, search_wikipedia={"query"})
            if self.config.research.searxng_url:
                allowed["web_search"] = {"query"}
            if self.config.research.read_pages:
                allowed["read_webpage"] = {"url"}
            if self.config.research.transit_url:
                allowed["get_departures"] = {"stop", "trains"}
                allowed["get_journey"] = {"origin", "destination", "when", "arrive", "only", "direct"}
            allowed.update(get_flights={"direction", "query"}, get_planes_overhead={"area"},
                           get_grill_stands={"day", "place", "near"}, get_quote={"asset", "advice"},
                           get_fundamentals={"asset", "about"}, get_insider_trades={"asset"},
                           get_warnings=set(), get_transit_disruptions={"line"}, get_space_weather=set(),
                           get_football={"team", "league"}, get_air_quality={"place"}, get_pollen=set(),
                           get_holidays={"kind", "day"}, get_fuel_prices={"fuel", "place"}, get_market_news={"asset"}, get_market_movers=set(), get_market_mood=set())
        if name not in allowed or not isinstance(arguments, dict) or set(arguments) - allowed[name]:
            raise ValueError("Unsupported tool or arguments")
        limit = 600 if name == "read_webpage" else 160
        if any(not isinstance(v, str) or len(v) > limit for v in arguments.values()):
            raise ValueError("Invalid tool argument")
        research = self.research
        if name == "web_search":
            query = arguments["query"]
            return await research.cached(name, query, 1800, lambda: research.web_search(query))
        if name == "search_news":
            query = arguments.get("query", "")
            return await self._remember(
                "news", lambda: research.cached(name, query, 600, lambda: research.news(query))
            )
        if name == "search_wikipedia":
            query = arguments["query"]
            return await research.cached(name, query, 3600, lambda: research.wikipedia(query))
        if name == "get_departures":
            stop = arguments.get("stop", "")
            trains = arguments.get("trains", "").strip().casefold() in {"ja", "true", "1", "yes"}
            return await research.cached(
                name, f"{stop}|{trains}", 30, lambda: research.departures(stop, trains)
            )
        if name == "get_journey":
            origin, destination = arguments.get("origin", ""), arguments.get("destination", "")
            when, only = arguments.get("when", ""), arguments.get("only", "")
            arrive = arguments.get("arrive", "").strip().casefold() in {"ja", "true", "yes", "1"}
            direct = arguments.get("direct", "").strip().casefold() in {"ja", "true", "yes", "1"}
            return await research.cached(
                name, f"{origin}|{destination}|{when}|{arrive}|{only}|{direct}", 60,
                lambda: research.journey(origin, destination, when, arrive, only, direct),
            )
        if name == "get_flights":
            arrivals = arguments.get("direction", "").strip().casefold().startswith(("an", "arr"))
            query = arguments.get("query", "")
            return await research.cached(
                name, f"{arrivals}|{query}", 60,
                lambda: ber_flights(self.client, self.config.station.timezone, arrivals, query),
            )
        if name == "get_planes_overhead":
            weather = self.config.weather
            area = arguments.get("area", "").strip().casefold()
            wide, rescue = area == "berlin", area == "rettung"
            return await research.cached(
                name, area, 60,
                lambda: planes_overhead(
                    self.client, weather.latitude, weather.longitude, weather.location, wide, rescue),
            )
        if name == "get_grill_stands":
            weather = self.config.weather
            day, place, near = arguments.get("day", ""), arguments.get("place", ""), arguments.get("near", "")
            lat, lon, label = weather.latitude, weather.longitude, ""
            home = f"{norm(weather.location)} {norm(self.config.station.location)} hier berlin"
            if near.strip() and norm(near) not in home:
                found = await self._berlin_place(near)
                if not found:
                    return {"text": f"Den Ort „{near.strip()}“ finde ich in Berlin/Brandenburg nicht.", "sources": []}
                lat, lon, label = found
            return await research.cached(
                name, f"{day}|{place}|{lat:.3f}|{lon:.3f}", 300,
                lambda: grill_stands(self.client, lat, lon, self.config.station.timezone, day, place,
                                     near=label),
            )
        if name == "get_market_movers":
            return await research.cached(name, "", 300, lambda: dax_movers(self.client))
        if name == "get_market_mood":
            mood = await crypto_mood(self.client)
            if not mood:
                raise httpx.TransportError("No mood index")
            return {"text": mood + ". 0 = extreme Angst, 100 = extreme Gier; misst die Stimmung, keine Prognose.",
                    "sources": ["https://alternative.me/crypto/fear-and-greed-index/"]}
        if name == "get_market_news":
            asset = arguments.get("asset", "").strip()
            return await research.cached(name, asset, 600, lambda: self.market_news(asset))
        if name == "get_fundamentals":
            assets = [a.strip() for a in SPLIT.split(arguments.get("asset", "")) if a.strip()]
            if not assets:
                return {"text": "Für Kennzahlen brauche ich den Namen einer Firma.", "sources": []}
            about = arguments.get("about", "").strip().casefold() in {"ja", "yes", "true", "1"}
            try:
                return await research.cached(name, f"{'|'.join(assets)}|{about}", 24 * 3600,
                                             lambda: fundamentals(self.client, assets, about=about))
            except LookupError:
                what = "kein Firmenprofil" if about else "keine Kennzahlen"
                return {"text": f"Zu „{', '.join(assets)}“ finde ich {what}.", "sources": []}
        if name in ALLTAG_TOOLS:
            return await self.alltag(name, arguments)
        if name == "get_insider_trades":
            asset = arguments.get("asset", "").strip()

            async def isin_of(label: str) -> str:
                figures = await wiki(self.client, label)
                return ((figures or {}).get("profile") or {}).get("isin", "")

            try:
                return await research.cached(name, asset, 900, lambda: insider_trades(self.client, asset, isin_of))
            except LookupError:
                return {"text": f"Zu „{asset}“ finde ich keine Insider-Geschäfte.", "sources": []}
        if name == "get_quote":
            tz = self.config.station.timezone
            assets = [a.strip() for a in SPLIT.split(arguments.get("asset", "")) if a.strip()]
            if not assets:
                return await research.cached(name, "", 60, lambda: market_overview(self.client, tz))
            advice = arguments.get("advice", "ja").strip().casefold() not in {"nein", "no", "false", "0"}
            try:
                return await research.cached(
                    name, f"{'|'.join(assets)}|{advice}", 60,
                    lambda: market_answer(self.client, assets, advice=advice, tz=tz),
                )
            except LookupError:
                return {"text": f"Zu „{', '.join(assets)}“ finde ich keinen Kurs.", "sources": []}
        if name == "read_webpage":
            return await self.research.read_page(arguments["url"], allowed_urls or set())
        if name == "get_weather":
            local = norm(arguments.get("location", "")) in {
                "", "hier", norm(self.config.weather.location), norm(self.config.station.location)
            }
            if not local:
                return await self.weather(**arguments)
            return await self._remember("weather", lambda: self.weather(**arguments))
        if name == "get_repeater_status":
            return self.repeater(**arguments)
        if name == "find_local_places":
            return await self.places(**arguments)
        return self.catalogue(**arguments)

    async def alltag(self, name: str, arguments: dict) -> dict:
        """Everyday data from alltag.py, each kept as long as it stays fresh."""
        tz = self.config.station.timezone
        weather = self.config.weather
        place = weather.location or self.config.station.location
        lat, lon = weather.latitude, weather.longitude
        wanted = arguments.get("place", "").strip()
        home = f"{norm(weather.location)} {norm(self.config.station.location)} hier"
        if name in {"get_fuel_prices", "get_air_quality"} and wanted and norm(wanted) not in home:
            found = await self._place(wanted, anywhere=True)
            if not found:
                return {"text": f"Den Ort „{wanted}“ finde ich nicht. Versuch es mit Ortsteil, Stadt oder "
                                "Postleitzahl.", "sources": []}
            lat, lon, place = found
        jobs = {
            "get_warnings": (300, lambda: warnings(self.client, tz)),
            "get_transit_disruptions": (180, lambda: disruptions(
                self.client, self.config.research.transit_url, arguments.get("line", "").strip(), tz)),
            "get_space_weather": (900, lambda: space_weather(self.client, tz)),
            "get_football": (300, lambda: football(self.client, arguments.get("team", "").strip(),
                                                    arguments.get("league", "").strip().casefold(), tz)),
            "get_air_quality": (1800, lambda: air_quality(self.client, lat, lon, place)),
            "get_pollen": (3600, lambda: pollen(self.client)),
            "get_holidays": (3600, lambda: holidays(self.client, arguments.get("kind", "").strip().casefold(),
                                                    arguments.get("day", "").strip().casefold(), tz)),
            "get_fuel_prices": (600, lambda: fuel_prices(self.client, lat, lon,
                                                         arguments.get("fuel", "").strip().casefold(), place=place)),
        }
        ttl, fetch = jobs[name]
        key = "|".join(f"{k}={v}" for k, v in sorted(arguments.items()))
        return await self.research.cached(name, key, ttl, fetch)

    async def places(self, query: str, location: str) -> dict:
        """Search Google Places live; never cache or persist the provider response here."""
        if not self.config.places.enabled or self.offline:
            return {"text": "Die lokale Ortssuche ist in dieser Konfiguration deaktiviert.", "sources": []}
        if not self.config.places.api_key:
            return {"text": "Die lokale Ortssuche ist noch nicht eingerichtet.", "sources": []}
        query = query.strip()
        location = location.strip() or self.config.station.location
        if not query:
            return {"text": "Wonach soll ich in der Nähe suchen?", "sources": []}
        payload = {
            "textQuery": f"{query} in {location}",
            "languageCode": "de",
            "locationBias": {
                "circle": {
                    "center": {
                        "latitude": self.config.weather.latitude,
                        "longitude": self.config.weather.longitude,
                    },
                    "radius": 4000.0,
                }
            },
        }
        data = await read_json(
            self.client,
            "POST",
            "https://places.googleapis.com/v1/places:searchText",
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": self.config.places.api_key,
                "X-Goog-FieldMask": "places.id,places.displayName,places.shortFormattedAddress,places.googleMapsUri",
            },
            json=payload,
            timeout=12,
        )
        rows = data.get("places")
        if not isinstance(rows, list) or not rows:
            return {"text": f"Dazu finde ich gerade keine passenden Orte nahe {location}.", "sources": []}
        entries, sources = [], ["https://maps.google.com/"]
        for place in rows[: self.config.places.max_results]:
            if not isinstance(place, dict):
                continue
            display = place.get("displayName")
            name = display.get("text") if isinstance(display, dict) else None
            address = place.get("shortFormattedAddress")
            if not isinstance(name, str) or not name.strip():
                continue
            entries.append(name.strip() + (f" ({address.strip()})" if isinstance(address, str) and address.strip() else ""))
            uri = place.get("googleMapsUri")
            if isinstance(uri, str) and uri.startswith("https://"):
                sources.append(uri)
        if not entries:
            return {"text": f"Dazu finde ich gerade keine verwendbaren Orte nahe {location}.", "sources": []}
        return {
            "text": f"Google Maps, nahe {location}: " + "; ".join(entries) + ".",
            "sources": list(dict.fromkeys(sources))[: 1 + self.config.places.max_results],
        }

    async def weather(self, location: str, period: str) -> dict:
        if not self.config.weather.enabled or self.offline:
            return {"text": "Die Wetterabfrage ist in dieser Konfiguration deaktiviert.", "sources": []}
        if not location.strip():
            location = self.config.weather.location
        key = norm(location)
        now = datetime.now(UTC).timestamp()
        known = self.cache.get(key)
        if known and now - known[0] < 600:
            forecast = known[1]
            name = forecast["_name"]
        else:
            local = {norm(self.config.weather.location), norm(self.config.station.location), "hier"}
            if key in local:
                lat, lon, name = (
                    self.config.weather.latitude,
                    self.config.weather.longitude,
                    self.config.weather.location,
                )
            else:
                data = await read_json(
                    self.client,
                    "GET",
                    "https://geocoding-api.open-meteo.com/v1/search",
                    params={"name": location, "count": 10, "language": "de"},
                )
                if not data.get("results"):
                    return {"text": "Ort nicht gefunden. Nenne bitte Ort und Land.", "sources": []}
                results = data["results"]
                # Berlin Ortsteile ("Steglitz", "Schöneberg") before same-named places elsewhere.
                place = next((row for row in results if row.get("admin1") in {"Berlin", "Land Berlin"}), results[0])
                lat, lon, name = place["latitude"], place["longitude"], place["name"]
            forecast = await read_json(
                self.client,
                "GET",
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "timezone": self.config.station.timezone,
                    "forecast_days": 7,
                    "wind_speed_unit": "kmh",
                    "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,wind_gusts_10m",
                    "daily": "weather_code,temperature_2m_min,temperature_2m_max,precipitation_probability_max,precipitation_sum,wind_speed_10m_max,wind_gusts_10m_max,sunshine_duration",
                    "hourly": "sunshine_duration,precipitation,precipitation_probability",
                },
            )
            forecast["_name"] = name
            if len(self.cache) >= 64:
                self.cache.clear()
            self.cache[key] = (now, forecast)
        if period == "week":
            result = week_outlook(name, forecast["daily"], self.config.station.timezone, now)
            result["hours"] = hourly_rows(forecast.get("hourly") or {})
            return result
        if period == "now":
            current = forecast["current"]
            text = f"{name} jetzt: {current['temperature_2m']:g} °C, {weather_description(current['weather_code'])}."
            text += (
                f" Gefühlt {measurement(current, 'apparent_temperature', ' °C')}."
                f" Wind {measurement(current, 'wind_speed_10m', ' km/h')},"
                f" Böen {measurement(current, 'wind_gusts_10m', ' km/h')}."
                f" Luftfeuchte {measurement(current, 'relative_humidity_2m', ' %')}."
            )
        else:
            today = datetime.now(ZoneInfo(self.config.station.timezone)).date()
            offset = {"today": 0, "tomorrow": 1, "day_after_tomorrow": 2}.get(period)
            target = (today + timedelta(days=offset)).isoformat() if offset is not None else period
            try:
                datetime.fromisoformat(target)
            except ValueError:
                return {
                    "text": "Nenne heute, morgen, übermorgen oder ein Datum im Format JJJJ-MM-TT.",
                    "sources": [],
                }
            daily = forecast["daily"]
            if target not in daily["time"]:
                return {"text": "Für dieses Datum liegen mir keine Vorhersagedaten vor.", "sources": []}
            i = daily["time"].index(target)
            text = (
                f"{name} {target}: {daily['temperature_2m_min'][i]:g}–{daily['temperature_2m_max'][i]:g} °C, "
                f"{day_description(daily, i)}{sunshine(daily, i)}{windows(forecast, target, 'sun')}. Niederschlagsrisiko max. "
                f"{measurement(daily, 'precipitation_probability_max', ' %', i)}, "
                f"Niederschlagsmenge {measurement(daily, 'precipitation_sum', ' mm', i)} am Tag"
                f"{windows(forecast, target, 'rain')}. "
                f"Wind bis {measurement(daily, 'wind_speed_10m_max', ' km/h', i)}, "
                f"Böen bis {measurement(daily, 'wind_gusts_10m_max', ' km/h', i)}."
            )
        result = {"text": text, "sources": ["https://open-meteo.com/"], "checked_at": now}
        if period not in {"now", ""} and "daily" in forecast and "i" in locals():
            daily = forecast["daily"]

            def pick(key):
                values = daily.get(key)
                value = values[i] if isinstance(values, list) and i < len(values) else None
                return value if type(value) in {int, float} else None

            result["day"] = {
                "sun_h": None if pick("sunshine_duration") is None else pick("sunshine_duration") / 3600,
                "rain_prob": pick("precipitation_probability_max"),
                "rain_mm": pick("precipitation_sum"),
                "code": pick("weather_code"),
            }
        return result

    def catalogue(self, query: str = "") -> dict:
        if query.strip().casefold() in {"*", "alle", "all", "repeater", "repeaters"}:
            query = ""
        rows = [r for r in self.store.repeaters() if not query or norm(query) in norm(r["name"])]
        if not rows:
            return {
                "text": "Kein passender Repeater im lokalen Katalog. Das ist kein Ausfallbeweis.",
                "sources": [],
            }
        names = "; ".join(row["name"] for row in rows[:5])
        return {
            "text": f"{len(rows)} bekannte Repeater. Beispiele: {names}. "
            "Ein Katalogeintrag bedeutet nicht aktuell online. Nenne einen Namen oder öffentlichen Schlüssel.",
            "sources": [],
            "count": len(rows),
        }

    def repeater(self, name: str) -> dict:
        rows = self.store.repeaters()
        clean = name.strip(" ?!.„“\"'")
        exact = [r for r in rows if r["name"].casefold() == clean.casefold()]
        is_key = re.fullmatch(r"[a-f0-9]{12,64}", clean, re.I)
        normalized = norm(clean)
        matches = exact or [
            r
            for r in rows
            if (
                r["id"].startswith(clean.lower())
                if is_key
                else normalized
                and any(normalized in norm(alias) for alias in [r["name"], *json.loads(r["aliases"])])
            )
        ]
        if not matches:
            return {
                "text": "Kein eindeutiger Repeater-Eintrag. Nenne den Gerätenamen oder öffentlichen Schlüssel. "
                "Ein fehlender Eintrag beweist keinen Ausfall.",
                "sources": [],
            }
        if len(matches) > 1:
            return {
                "text": "Mehrere Repeater passen: "
                + "; ".join(f"{r['name']} ({r['id'][:12]})" for r in matches[:5])
                + ". Welchen meinst du?",
                "sources": [],
            }
        row = matches[0]
        stamp = row["advert"]
        date = (
            datetime.fromtimestamp(stamp, ZoneInfo(self.config.station.timezone)).strftime(
                "%d.%m.%Y %H:%M %Z"
            )
            if stamp
            else "nicht bekannt"
        )
        return {
            "text": f"{row['name']}: letzter lokal gespeicherter Advert {date}. "
            "Ein Ausfall ist damit nicht bestätigt; fehlende Adverts allein sind kein Beweis.",
            "sources": [],
            "public_key": row["id"],
            "advert_at": stamp,
            "catalogue_checked_at": row["checked"],
            "outage_confirmed": False,
        }


def measurement(data, key, unit, index=None):
    value = data.get(key)
    if index is not None:
        value = value[index] if isinstance(value, list) and index < len(value) else None
    return f"{value:g}{unit}" if type(value) in {int, float} else "k. A."


WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def week_outlook(name: str, daily: dict, timezone: str, checked_at: float) -> dict:
    """Seven days in short form plus per-day numbers, for 'wann scheint die Sonne wieder?'."""
    today = datetime.now(ZoneInfo(timezone)).date()
    days, parts = [], []
    for i, stamp in enumerate(daily.get("time") or []):
        try:
            date = datetime.fromisoformat(stamp).date()
        except ValueError:
            continue
        if date < today:
            continue

        def pick(key, i=i):
            values = daily.get(key)
            value = values[i] if isinstance(values, list) and i < len(values) else None
            return value if type(value) in {int, float} else None

        sun = pick("sunshine_duration")
        day = {
            "date": date, "sun_h": None if sun is None else sun / 3600, "rain_prob": pick("precipitation_probability_max"),
            "rain_mm": pick("precipitation_sum"), "code": pick("weather_code"),
        }
        days.append(day)
        label = "heute" if date == today else "morgen" if (date - today).days == 1 else WEEKDAYS[date.weekday()][:2]
        sun_text = "?" if day["sun_h"] is None else f"{round(day['sun_h']):g}"
        parts.append(f"{label} {sun_text}h")
    return {
        "text": f"{name}, Sonnenstunden: " + ", ".join(parts) + ".",
        "sources": ["https://open-meteo.com/"],
        "checked_at": checked_at,
        "days": days,
    }


def hourly_rows(hourly: dict) -> list[dict]:
    """Open-Meteo hourly arrays -> rows with local time, sunshine seconds and rain."""
    rows = []
    times = hourly.get("time") or []
    for n, stamp in enumerate(times):
        try:
            when = datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            continue

        def pick(key, n=n):
            values = hourly.get(key)
            value = values[n] if isinstance(values, list) and n < len(values) else None
            return value if type(value) in {int, float} else None

        rows.append({"time": when, "sun_s": pick("sunshine_duration"), "mm": pick("precipitation"),
                     "prob": pick("precipitation_probability")})
    return rows


def sunny_hour(row: dict) -> bool:
    return (row.get("sun_s") or 0) >= 1800


def rainy_hour(row: dict) -> bool:
    return (row.get("mm") or 0) >= 0.2 or ((row.get("prob") or 0) >= 70 and (row.get("mm") or 0) > 0)


def hour_windows(rows: list[dict], test) -> list[tuple[int, int]]:
    """Consecutive matching hours as (start, end) clock hours, end exclusive."""
    spans: list[list[int]] = []
    for row in rows:
        if not test(row):
            continue
        hour = row["time"].hour
        if spans and spans[-1][1] == hour:
            spans[-1][1] = hour + 1
        else:
            spans.append([hour, hour + 1])
    return [(a, b) for a, b in spans]


def windows(forecast: dict, target: str, kind: str) -> str:
    """' (10–13 Uhr)' for sun or ', Regen vor allem 15–18 Uhr' for rain on the requested day."""
    rows = [r for r in hourly_rows(forecast.get("hourly") or {}) if r["time"].date().isoformat() == target]
    spans = hour_windows(rows, sunny_hour if kind == "sun" else rainy_hour)
    if not spans:
        return ""
    spans = sorted(spans, key=lambda s: s[0] - s[1])[:2]
    text = " und ".join(f"{a}–{b}" for a, b in sorted(spans)) + " Uhr"
    return f" ({text})" if kind == "sun" else f", Regen vor allem {text}"


def sunshine(daily: dict, i: int) -> str:
    """Sunshine hours answer 'wird es sonnig?' – the weather code alone cannot."""
    values = daily.get("sunshine_duration")
    value = values[i] if isinstance(values, list) and i < len(values) else None
    if type(value) not in {int, float}:
        return ""
    hours = value / 3600
    return ", kaum Sonne" if hours < 0.5 else f", etwa {round(hours):g} Std. Sonne"


def day_description(daily: dict, i: int) -> str:
    """No 'Regen' when the same forecast says 0 mm and 0 % – that reads as a contradiction on air."""
    text = weather_description(daily["weather_code"][i])
    try:
        dry = not daily["precipitation_sum"][i] and not daily["precipitation_probability_max"][i]
    except (KeyError, IndexError, TypeError):
        dry = False
    if dry and text in {"Nieselregen", "Regen", "Schnee", "Gewitter"}:
        return "bewölkt, trocken"
    try:
        sunny = (daily["sunshine_duration"][i] or 0) >= 2 * 3600
    except (KeyError, IndexError, TypeError):
        sunny = False
    return "Sonne und Schauer" if sunny and text in {"Nieselregen", "Regen"} else text


def weather_description(code: int) -> str:
    if code == 0:
        return "klar"
    if code in (1, 2):
        return "leicht bewölkt"
    if code == 3:
        return "bedeckt"
    if code in (45, 48):
        return "Nebel"
    if code in (51, 53, 55, 56, 57):
        return "Nieselregen"
    if code in (61, 63, 65, 66, 67, 80, 81, 82):
        return "Regen"
    if code in (71, 73, 75, 77, 85, 86):
        return "Schnee"
    if code in (95, 96, 99):
        return "Gewitter"
    return "wechselhaft"
