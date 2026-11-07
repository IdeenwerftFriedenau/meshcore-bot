"""Read-only tool boundary: no radio commands, shell, file writes or analyzer scraping."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .config import Config
from .http import read_json
from .research import Research
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
        "Nächste Abfahrten (Echtzeit) von Bus, Tram, U- und S-Bahn an einer Haltestelle in Berlin/Brandenburg.",
        {"stop": {"type": "string", "description": "Haltestellenname, z.B. 'S Friedenau' oder 'Breslauer Platz'"}},
        ["stop"],
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
            result.append(RESEARCH_SCHEMAS["get_departures"])
        if research.read_pages:
            result.append(RESEARCH_SCHEMAS["read_webpage"])
    return result


class Tools:
    def __init__(self, config: Config, store: Store, client: httpx.AsyncClient, *, offline: bool = False):
        self.config, self.store, self.client, self.offline = config, store, client, offline
        self.cache: dict[str, tuple[float, dict]] = {}
        self.research = Research(config, client)

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
                allowed["get_departures"] = {"stop"}
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
            return await research.cached(name, query, 600, lambda: research.news(query))
        if name == "search_wikipedia":
            query = arguments["query"]
            return await research.cached(name, query, 3600, lambda: research.wikipedia(query))
        if name == "get_departures":
            stop = arguments["stop"]
            return await research.cached(name, stop, 30, lambda: research.departures(stop))
        if name == "read_webpage":
            return await self.research.read_page(arguments["url"], allowed_urls or set())
        if name == "get_weather":
            return await self.weather(**arguments)
        if name == "get_repeater_status":
            return self.repeater(**arguments)
        if name == "find_local_places":
            return await self.places(**arguments)
        return self.catalogue(**arguments)

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
                    params={"name": location, "count": 1, "language": "de"},
                )
                if not data.get("results"):
                    return {"text": "Ort nicht gefunden. Nenne bitte Ort und Land.", "sources": []}
                place = data["results"][0]
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
                    "daily": "weather_code,temperature_2m_min,temperature_2m_max,precipitation_probability_max,precipitation_sum,wind_speed_10m_max,wind_gusts_10m_max",
                },
            )
            forecast["_name"] = name
            if len(self.cache) >= 64:
                self.cache.clear()
            self.cache[key] = (now, forecast)
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
                f"{weather_description(daily['weather_code'][i])}. Niederschlagsrisiko max. "
                f"{measurement(daily, 'precipitation_probability_max', ' %', i)}, "
                f"Niederschlagsmenge {measurement(daily, 'precipitation_sum', ' mm', i)} am Tag. "
                f"Wind bis {measurement(daily, 'wind_speed_10m_max', ' km/h', i)}, "
                f"Böen bis {measurement(daily, 'wind_gusts_10m_max', ' km/h', i)}."
            )
        return {"text": text, "sources": ["https://open-meteo.com/"], "checked_at": now}

    def catalogue(self, query: str = "") -> dict:
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
