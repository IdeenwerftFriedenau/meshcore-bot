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
]


class Tools:
    def __init__(self, config: Config, store: Store, client: httpx.AsyncClient, *, offline: bool = False):
        self.config, self.store, self.client, self.offline = config, store, client, offline
        self.cache: dict[str, tuple[float, dict]] = {}

    async def run(self, name: str, arguments: dict) -> dict:
        allowed = {
            "get_weather": {"location", "period"},
            "get_repeater_status": {"name"},
            "list_repeaters": {"query"},
        }
        if name not in allowed or not isinstance(arguments, dict) or set(arguments) - allowed[name]:
            raise ValueError("Unsupported tool or arguments")
        if any(not isinstance(v, str) or len(v) > 160 for v in arguments.values()):
            raise ValueError("Invalid tool argument")
        if name == "get_weather":
            return await self.weather(**arguments)
        if name == "get_repeater_status":
            return self.repeater(**arguments)
        return self.catalogue(**arguments)

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
                    "current": "temperature_2m,weather_code",
                    "daily": "weather_code,temperature_2m_min,temperature_2m_max,precipitation_probability_max",
                },
            )
            forecast["_name"] = name
            if len(self.cache) >= 64:
                self.cache.clear()
            self.cache[key] = (now, forecast)
        if period == "now":
            current = forecast["current"]
            text = f"{name} jetzt: {current['temperature_2m']:g} °C, {weather_description(current['weather_code'])}."
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
                f"{weather_description(daily['weather_code'][i])}, Regenwahrscheinlichkeit "
                f"{daily['precipitation_probability_max'][i]} %."
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
