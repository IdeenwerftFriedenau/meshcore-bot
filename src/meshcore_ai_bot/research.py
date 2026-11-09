"""Free, key-less online research: Wikipedia, news and reading a found web page.

Everything here is read-only. Fetched text is source material for the model, never instructions.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import socket
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx

from .config import Config
from .http import read_bytes, read_json

USER_AGENT = "MeshCoreAIBot/0.4 (+https://github.com/IdeenwerftFriedenau/meshcore-bot)"
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "de"}
URL_PATTERN = re.compile(r"https://[^\s<>\"'„“”]+")
PAGE_LIMIT = 1_500_000
PAGE_TEXT_CHARS = 3500
# Tagesschau region ids for Berlin and Brandenburg.
REGIONS = {"berlin": "3", "brandenburg": "4"}


def plain(value) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", str(value or ""))).split())


def short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def urls_in(text: str) -> set[str]:
    return {match.rstrip(".,;:!?)]") for match in URL_PATTERN.findall(text or "")}


def normalize_url(url: str) -> str:
    return url.split("#", 1)[0].rstrip("/")


class Research:
    def __init__(self, config: Config, client: httpx.AsyncClient):
        self.config, self.client = config, client
        # Loopback services (SearXNG, Kiwix) bypass any proxy settings; tests may inject a client.
        self.local_client: httpx.AsyncClient | None = None
        self._kiwix_book: tuple[float, str] | None = None
        self._cache: dict[tuple[str, str], tuple[float, dict]] = {}

    async def cached(self, kind: str, query: str, ttl: float, fetch) -> dict:
        """Short-lived result cache: repeated questions do not hit the sources again."""
        key = (kind, " ".join(query.casefold().split()))
        now = time.monotonic()
        known = self._cache.get(key)
        if known and now - known[0] < ttl:
            return known[1]
        result = await fetch()
        if result.get("sources"):
            if len(self._cache) >= 128:
                self._cache = {k: v for k, v in self._cache.items() if now - v[0] < 600}
                if len(self._cache) >= 128:
                    self._cache.clear()
            self._cache[key] = (now, result)
        return result

    async def _local_json(self, url: str, **kwargs):
        if self.local_client is not None:
            return await read_json(self.local_client, "GET", url, **kwargs)
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            return await read_json(client, "GET", url, **kwargs)

    async def _local_bytes(self, url: str, **kwargs) -> bytes:
        if self.local_client is not None:
            return await read_bytes(self.local_client, "GET", url, **kwargs)
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            return await read_bytes(client, "GET", url, **kwargs)

    # Web search via a local SearXNG -----------------------------------------------------------------

    async def web_search(self, query: str) -> dict:
        query = query.strip()
        base = self.config.research.searxng_url
        if not base:
            return {"text": "Die allgemeine Websuche ist hier nicht eingerichtet.", "sources": []}
        if not query:
            return {"text": "Wonach soll ich im Web suchen?", "sources": []}
        data = await self._local_json(
            base.rstrip("/") + "/search",
            params={"q": query, "format": "json", "language": "de-DE", "safesearch": 1},
            headers=HEADERS,
            timeout=20,
            limit=2_000_000,
        )
        lines, sources = [], []
        for answer in data.get("answers", [])[:2]:
            value = plain(answer.get("answer") if isinstance(answer, dict) else answer)
            if value:
                lines.append("Direktantwort: " + short(value, 300))
        for box in data.get("infoboxes", [])[:1]:
            if isinstance(box, dict) and box.get("content"):
                lines.append(f"Infobox {plain(box.get('infobox'))}: " + short(plain(box["content"]), 400))
        for row in data.get("results", []):
            if not isinstance(row, dict):
                continue
            url = row.get("url")
            title = plain(row.get("title"))
            if not isinstance(url, str) or not url.startswith("https://") or not title:
                continue
            lines.append(f"{title} ({url}): {short(plain(row.get('content')), 240)}")
            sources.append(url)
            if len(sources) >= self.config.research.max_results:
                break
        if not lines:
            return {"text": f"Die Websuche fand nichts Brauchbares zu „{query}“.", "sources": []}
        return {"text": " | ".join(lines), "sources": sources}

    async def _stop(self, base: str, name: str) -> dict | None:
        """Best stop for a name; a well-known place (POI) wins only when it clearly matches the words asked."""
        places = await read_json(
            self.client, "GET", base + "/locations",
            params={"query": name, "results": 3, "stops": "true", "addresses": "false", "poi": "true"},
            headers=HEADERS, timeout=12, limit=500_000,
        )
        valid = [p for p in places if isinstance(p, dict) and re.fullmatch(r"[\w:-]{1,40}", str(p.get("id", "")))] \
            if isinstance(places, list) else []
        stops = [p for p in valid if p.get("type") == "stop"]
        wanted = {w for w in re.findall(r"\w{3,}", name.casefold())}
        for place in valid:
            if place.get("type") == "stop":
                break
            words = set(re.findall(r"\w{3,}", plain(place.get("name")).casefold()))
            if wanted and wanted <= words and isinstance(place.get("latitude"), (int, float)):
                return place
        return stops[0] if stops else None

    async def journey(self, origin: str, destination: str, when: str = "", arrive: bool = False,
                      only: str = "", direct: bool = False) -> dict:
        """Connections like a journey planner app (BVG/VBB data), short enough for radio.

        when: local time "YYYY-MM-DDTHH:MM" to leave at (or, with arrive, to be there by); only: one product
        (bus, tram, subway, suburban) for "nur mit dem Bus".
        """
        base = self.config.research.transit_url.rstrip("/")
        if not base:
            return {"text": "Die Verbindungsauskunft ist hier nicht eingerichtet.", "sources": []}
        start, goal = await self._stop(base, origin.strip()), await self._stop(base, destination.strip())
        if not start or not goal:
            missing = origin if not start else destination
            return {"text": f"Haltestelle „{missing.strip()}“ nicht gefunden.", "sources": []}
        tz = ZoneInfo(self.config.station.timezone)
        params = {"results": 3, "stopovers": "false", "remarks": "false"}
        for side, place in (("from", start), ("to", goal)):
            if place.get("type") == "stop":
                params[side] = place["id"]
            else:  # a place like "KaDeWe": the planner adds the walk
                params.update({f"{side}.id": place["id"], f"{side}.name": plain(place.get("name")),
                               f"{side}.latitude": place.get("latitude"), f"{side}.longitude": place.get("longitude")})
        wanted = None
        if when:
            try:
                wanted = datetime.fromisoformat(when).replace(tzinfo=tz)
            except ValueError:
                wanted = None
        if wanted:
            params["arrival" if arrive else "departure"] = wanted.isoformat()
        if direct:
            params["transfers"] = 0
        if only in {"bus", "tram", "subway", "suburban"}:
            for product in ("suburban", "subway", "tram", "bus", "ferry", "express", "regional"):
                params[product] = "true" if product == only else "false"
        try:
            data = await read_json(
                self.client, "GET", base + "/journeys", params=params,
                headers=HEADERS, timeout=15, limit=3_000_000,
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code in {400, 404}:  # e.g. no tram at all between the two places
                limit = {"bus": " nur mit Bus", "tram": " nur mit Tram", "subway": " nur mit U-Bahn",
                         "suburban": " nur mit S-Bahn"}.get(only, "") + (" ohne Umsteigen" if direct else "")
                return {"text": f"{origin.strip()} → {destination.strip()}:{limit or ''} keine Verbindung gefunden.",
                        "sources": []}
            raise
        lines = []
        for item in (data.get("journeys") if isinstance(data, dict) else None) or []:
            legs = [leg for leg in item.get("legs") or [] if isinstance(leg, dict)]
            rides = [leg for leg in legs if not leg.get("walking") and leg.get("line")]
            if not rides:
                continue
            try:
                depart = datetime.fromisoformat(rides[0].get("departure") or rides[0]["plannedDeparture"]).astimezone(tz)
                ride_end = datetime.fromisoformat(rides[-1].get("arrival") or rides[-1]["plannedArrival"]).astimezone(tz)
                first = datetime.fromisoformat(legs[0].get("departure") or legs[0]["plannedDeparture"]).astimezone(tz)
                last = datetime.fromisoformat(legs[-1].get("arrival") or legs[-1]["plannedArrival"]).astimezone(tz)
            except (KeyError, TypeError, ValueError):
                continue
            names = [plain((leg.get("line") or {}).get("name")) for leg in rides]
            via = [plain((leg.get("origin") or {}).get("name")).replace(" (Berlin)", "") for leg in rides[1:]]
            delay = rides[0].get("departureDelay")
            late = f" (+{round(delay / 60)})" if type(delay) is int and delay >= 60 else ""
            track = plain(rides[0].get("departurePlatform"))
            board = plain((rides[0].get("origin") or {}).get("name")).replace(" (Berlin)", "")
            text = f"{depart:%H:%M}{late} {' > '.join(names)}"
            if (depart - first).total_seconds() > 180:  # walk to the first stop
                text = f"los {first:%H:%M} zu Fuß, " + text
            if board and board != plain(start.get("name")).replace(" (Berlin)", ""):
                text += f" ab {board}"
            if track:
                text += f" Gl. {track}"
            if via:
                text += f", umsteigen {', '.join(via)}"
            minutes = round((last - first).total_seconds() / 60)
            there = max(ride_end, last)
            text += f", an {there:%H:%M} ({minutes} Min.)"
            lines.append((first, there, text))
        if arrive and wanted:  # the latest connections that are still on time
            lines = [line for line in lines if line[1] <= wanted][-2:] or lines[:1]
        else:  # drop a trip that leaves barely earlier but arrives clearly later than another one
            lines = [a for a in lines if not any(b[1] < a[1] and (b[0] - a[0]).total_seconds() <= 300 for b in lines)]
        lines = sorted(lines[:2], key=lambda line: line[0])
        if not lines:
            return {"text": "Keine passende Verbindung gefunden.", "sources": []}
        names = f"{plain(start.get('name'))} → {plain(goal.get('name'))}".replace(" (Berlin)", "")
        today = datetime.now(tz).date()
        offset = (lines[0][0].date() - today).days
        day = "" if offset == 0 else "morgen, " if offset == 1 else f"{lines[0][0]:%d.%m.}, "
        aim = f"Ankunft bis {wanted:%H:%M}, " if arrive and wanted else ""
        kind = ("ohne Umsteigen, " if direct else "") + {"bus": "nur Bus, ", "tram": "nur Tram, ", "subway": "nur U-Bahn, ", "suburban": "nur S-Bahn, "}.get(only, "")
        lead = f"{names} ({day}{aim}{kind}".rstrip(", ") + ")" if day or aim or kind else names
        return {"text": f"{lead}: " + "; ".join(line[2] for line in lines) + ".",
                "sources": ["https://www.bvg.de/de/verbindungen/verbindungssuche"]}

    # Public transport -------------------------------------------------------------------------------

    async def departures(self, stop: str, trains: bool = False, suburban: bool = False) -> dict:
        base = self.config.research.transit_url.rstrip("/")
        stop = stop.strip()
        if not base:
            return {"text": "Die Abfahrtsauskunft ist hier nicht eingerichtet.", "sources": []}
        if not stop:
            return {"text": "Von welcher Haltestelle?", "sources": []}
        places = await read_json(
            self.client,
            "GET",
            base + "/locations",
            params={"query": stop, "results": 1, "stops": "true", "addresses": "false", "poi": "false"},
            headers=HEADERS,
            timeout=12,
            limit=500_000,
        )
        place = places[0] if isinstance(places, list) and places and isinstance(places[0], dict) else None
        if not place or not re.fullmatch(r"[\w:-]{1,40}", str(place.get("id", ""))):
            return {"text": f"Keine Haltestelle „{stop}“ gefunden.", "sources": []}
        data = await read_json(
            self.client,
            "GET",
            f"{base}/stops/{place['id']}/departures",
            params={"duration": 90 if trains else 30, "results": 8, "remarks": "false",
                    **({"bus": "false", "tram": "false", "subway": "false", "ferry": "false",
                        **({} if suburban else {"suburban": "false"})}
                       if trains else {})},
            headers=HEADERS,
            timeout=12,
            limit=1_000_000,
        )
        rows = data.get("departures", []) if isinstance(data, dict) else data
        tz = ZoneInfo(self.config.station.timezone)
        entries = []
        rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        # Cancelled trips have no real-time "when" and come first in the feed: keep timetable order.
        rows.sort(key=lambda row: str(row.get("when") or row.get("plannedWhen") or ""))
        for row in rows:
            when = row.get("when") or row.get("plannedWhen")
            line = plain((row.get("line") or {}).get("name"))
            direction = plain(row.get("direction"))
            if not when or not line:
                continue
            try:
                clock = datetime.fromisoformat(str(when)).astimezone(tz).strftime("%H:%M")
            except ValueError:
                continue
            delay = row.get("delay")
            late = f" (+{round(delay / 60)})" if type(delay) is int and delay >= 60 else ""
            gone = " fällt aus" if row.get("cancelled") else ""
            track = plain(row.get("platform")) if trains else ""
            track = f" Gl. {track}" if track else ""
            entries.append(f"{clock} {line} → {direction}{track}{late}{gone}")
            if len(entries) >= 6:
                break
        name = plain(place.get("name")) or stop
        if not entries and trains and not suburban:
            # A 'Zug' at an S-Bahn-only station such as S Friedenau is the S-Bahn.
            return await self.departures(stop, trains=True, suburban=True)
        if not entries:
            window = "90 Minuten keine Zugabfahrten" if trains else "30 Minuten keine Abfahrten"
            return {"text": f"{name}: In den nächsten {window} gefunden.", "sources": []}
        return {
            "text": f"Abfahrten {name} (Echtzeit, BVG/VBB): " + "; ".join(entries) + ".",
            "sources": ["https://www.bvg.de/de/verbindungen/echtzeit-abfahrten"],
        }

    # Wikipedia ---------------------------------------------------------------------------------------

    async def wikipedia(self, query: str) -> dict:
        query = query.strip()
        if not query:
            return {"text": "Wonach soll ich in der Wikipedia suchen?", "sources": []}
        try:
            return await self.wikipedia_online(query)
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
            if not self.config.research.kiwix_url:
                raise
        return await self.wikipedia_offline(query)

    async def wikipedia_online(self, query: str) -> dict:
        base = f"https://{self.config.research.wikipedia_language}.wikipedia.org"
        found = await read_json(
            self.client,
            "GET",
            base + "/w/rest.php/v1/search/page",
            params={"q": query, "limit": 3},
            headers=HEADERS,
            timeout=12,
            limit=500_000,
        )
        pages = [p for p in found.get("pages", []) if isinstance(p, dict) and isinstance(p.get("key"), str)]
        if not pages:
            return {"text": f"Die Wikipedia kennt keinen passenden Artikel zu „{query}“.", "sources": []}
        top = pages[0]
        summary = await read_json(
            self.client,
            "GET",
            base + "/api/rest_v1/page/summary/" + quote(top["key"], safe=""),
            headers=HEADERS,
            timeout=12,
            limit=500_000,
        )
        title = plain(summary.get("title") or top.get("title") or top["key"])
        extract = plain(summary.get("extract") or top.get("excerpt") or "")
        link = (summary.get("content_urls") or {}).get("desktop", {}).get("page")
        if not isinstance(link, str) or not link.startswith("https://"):
            link = base + "/wiki/" + quote(top["key"])
        others = [plain(p.get("title") or p["key"]) for p in pages[1:]]
        text = f"Wikipedia „{title}“: {short(extract, 1200) or 'ohne Kurzfassung'}"
        if others:
            text += " Weitere Treffer: " + "; ".join(others) + "."
        return {"text": text, "sources": [link]}

    async def kiwix_book(self) -> str:
        """The served ZIM name changes with every update, so read it from the catalogue."""
        now = time.time()
        if self._kiwix_book and now - self._kiwix_book[0] < 3600:
            return self._kiwix_book[1]
        raw = await self._local_bytes(
            self.config.research.kiwix_url.rstrip("/") + "/catalog/v2/entries",
            params={"count": 50},
            timeout=10,
            limit=1_000_000,
        )
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise ValueError("XML entity declaration")
        books = []
        for entry in ET.fromstring(raw).iter():
            if not entry.tag.endswith("entry"):
                continue
            language = next((c.text or "" for c in entry if c.tag.endswith("language")), "")
            for link in entry:
                href = link.get("href", "") if link.tag.endswith("link") else ""
                match = re.fullmatch(r"/content/([\w.-]+)/?", href)
                if match and "wikipedia" in match.group(1):
                    books.append((language.startswith(self.config.research.wikipedia_language), match.group(1)))
        if not books:
            raise ValueError("No Wikipedia ZIM served")
        book = sorted(books, reverse=True)[0][1]
        self._kiwix_book = (now, book)
        return book

    async def wikipedia_offline(self, query: str) -> dict:
        base = self.config.research.kiwix_url.rstrip("/")
        book = await self.kiwix_book()
        raw = await self._local_bytes(
            base + "/search",
            params={"books.name": book, "pattern": query, "format": "xml", "pageLength": 3},
            timeout=15,
            limit=1_000_000,
        )
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise ValueError("XML entity declaration")
        items = ET.fromstring(raw).findall(".//item")
        hits = [
            (plain(item.findtext("title")), (item.findtext("link") or "").strip(), plain(item.findtext("description")))
            for item in items
        ]
        hits = [hit for hit in hits if hit[0] and hit[1].startswith(f"/content/{book}/")]
        if not hits:
            return {"text": f"Die Offline-Wikipedia kennt keinen passenden Artikel zu „{query}“.", "sources": []}
        title, link, snippet = hits[0]
        body = await self._local_bytes(base + link, timeout=15, limit=4_000_000)
        document = re.sub(r"<(style|script)\b.*?</\1>", " ", body.decode("utf-8", "replace"), flags=re.S | re.I)
        paragraphs = [plain(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", document, re.S)]
        intro = " ".join(p for p in paragraphs if len(p) > 60)[:2000] or snippet
        intro = re.sub(r"\[\d+\]", "", intro)
        stamp = re.search(r"(\d{4}-\d{2})$", book)
        text = f"Wikipedia offline{f' (Stand {stamp.group(1)})' if stamp else ''} „{title}“: {short(intro, 1200)}"
        others = [hit[0] for hit in hits[1:]]
        if others:
            text += " Weitere Treffer: " + "; ".join(others) + "."
        article = link.removeprefix(f"/content/{book}/")
        return {
            "text": text,
            "sources": [f"https://{self.config.research.wikipedia_language}.wikipedia.org/wiki/{article}"],
        }

    # News --------------------------------------------------------------------------------------------

    async def news(self, query: str = "") -> dict:
        query = query.strip()
        tz = ZoneInfo(self.config.station.timezone)
        items: list[dict] = []
        errors = 0
        lowered = query.casefold()
        regions = [rid for name, rid in REGIONS.items() if name in lowered]
        topic = re.sub(r"\b(?:berlin|brandenburg|nachrichten|news|aktuell(?:e|es)?|heute)\b", " ", lowered)
        topic = " ".join(topic.split())
        try:
            if topic:
                data = await read_json(
                    self.client,
                    "GET",
                    "https://www.tagesschau.de/api2u/search/",
                    params={"searchText": topic, "pageSize": 6},
                    headers=HEADERS,
                    timeout=12,
                )
                items += [tagesschau_item(row, tz) for row in data.get("searchResults", [])]
            else:
                params = {"regions": ",".join(regions)} if regions else None
                url = "https://www.tagesschau.de/api2u/news/" if regions else "https://www.tagesschau.de/api2u/homepage"
                data = await read_json(self.client, "GET", url, params=params, headers=HEADERS, timeout=12)
                items += [tagesschau_item(row, tz) for row in data.get("news", [])[:6]]
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            errors += 1
        for feed in self.config.research.news_feeds:
            try:
                items += await self.feed(feed, topic, tz)
            except (httpx.HTTPError, ValueError, TypeError, ET.ParseError):
                errors += 1
        items = [item for item in items if item]
        seen, unique = set(), []
        for item in items:
            key = item["title"].casefold()
            if key not in seen:
                seen.add(key)
                unique.append(item)
        unique = unique[:8]
        if not unique:
            text = (
                "Die Nachrichtenquellen sind gerade nicht erreichbar."
                if errors and not items
                else f"Keine aktuellen Meldungen zu „{query}“ gefunden."
            )
            return {"text": text, "sources": []}
        lines = [
            " ".join(part for part in [item["date"], item["outlet"] + ":", item["title"], item["teaser"]] if part)
            for item in unique
        ]
        return {
            "text": " | ".join(lines),
            "sources": [item["url"] for item in unique if item["url"]],
        }

    async def feed(self, url: str, topic: str, tz: ZoneInfo) -> list[dict]:
        raw = await read_bytes(self.client, "GET", url, headers=HEADERS, timeout=12, limit=1_000_000)
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise ValueError("XML entity declaration")
        root = ET.fromstring(raw)
        outlet = (urlsplit(url).hostname or "RSS").removeprefix("www.")
        words = [w for w in re.findall(r"\w{3,}", topic)]
        rows = []
        for item in root.findall(".//item")[:30]:
            title, link = plain(item.findtext("title")), plain(item.findtext("link"))
            teaser = plain(item.findtext("description"))
            haystack = (title + " " + teaser).casefold()
            if words and not any(w in haystack for w in words):
                continue
            try:
                date = parsedate_to_datetime(item.findtext("pubDate") or "").astimezone(tz).strftime("%d.%m. %H:%M")
            except (TypeError, ValueError):
                date = ""
            if title:
                rows.append(
                    {
                        "title": title,
                        "teaser": short(teaser, 160),
                        "date": date,
                        "outlet": outlet,
                        "url": link if link.startswith("https://") else "",
                    }
                )
            if len(rows) >= 4:
                break
        return rows

    # Reading a page ----------------------------------------------------------------------------------

    async def read_page(self, url: str, allowed: set[str]) -> dict:
        url = url.strip()
        if normalize_url(url) not in {normalize_url(u) for u in allowed}:
            return {
                "text": "Diese Adresse stammt aus keiner Suche und keiner Frage. Erst suchen, dann lesen.",
                "sources": [],
            }
        for _ in range(4):
            await check_public_https(url)
            async with self.client.stream(
                "GET", url, headers=HEADERS, timeout=15, follow_redirects=False
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location", "")
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                kind = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if kind not in {"text/html", "application/xhtml+xml", "text/plain"}:
                    return {"text": "Diese Seite ist kein lesbarer Text.", "sources": []}
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > PAGE_LIMIT:
                        break
                body = bytes(data[:PAGE_LIMIT]).decode(response.encoding or "utf-8", errors="replace")
            title, text = page_text(body) if kind != "text/plain" else ("", " ".join(body.split()))
            if not text:
                return {"text": "Auf dieser Seite steht kein lesbarer Text.", "sources": []}
            return {
                "text": (f"„{title}“: " if title else "") + short(text, PAGE_TEXT_CHARS),
                "sources": [url],
            }
        return {"text": "Zu viele Weiterleitungen.", "sources": []}


def tagesschau_item(row, tz: ZoneInfo) -> dict | None:
    if not isinstance(row, dict) or not row.get("title"):
        return None
    try:
        date = datetime.fromisoformat(str(row.get("date", "")).replace("Z", "+00:00")).astimezone(tz)
        stamp = date.strftime("%d.%m. %H:%M")
    except ValueError:
        stamp = ""
    url = row.get("shareURL") or row.get("detailsweb") or ""
    title = plain(row.get("title"))
    topline = plain(row.get("topline"))
    return {
        "title": f"{topline}: {title}" if topline and topline not in title else title,
        "teaser": short(plain(row.get("firstSentence")), 200),
        "date": stamp,
        "outlet": "tagesschau",
        "url": url if isinstance(url, str) and url.startswith("https://") else "",
    }


async def check_public_https(url: str) -> None:
    """Only public HTTPS hosts: no credentials, odd ports, loopback, LAN or link-local targets."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("Only public HTTPS addresses")
    if parts.port not in {None, 443}:
        raise ValueError("Only the default HTTPS port")
    addresses = await resolve(parts.hostname)
    if not addresses:
        raise ValueError("Unknown host")
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise ValueError("Private address")


async def resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "header", "footer", "form", "aside", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.article_depth = 0
        self.title, self.in_title = "", False
        self.parts: list[str] = []
        self.article: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.depth += 1
        elif tag in {"article", "main"}:
            self.article_depth += 1
        elif tag == "title":
            self.in_title = True
        if tag in {"p", "br", "li", "h1", "h2", "h3", "div", "tr"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.depth:
            self.depth -= 1
        elif tag in {"article", "main"} and self.article_depth:
            self.article_depth -= 1
        elif tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.depth:
            self.parts.append(data)
            if self.article_depth:
                self.article.append(data)


def page_text(body: str) -> tuple[str, str]:
    parser = _Text()
    try:
        parser.feed(body)
        parser.close()
    except (AssertionError, ValueError):
        pass
    article = " ".join(" ".join(parser.article).split())
    text = article if len(article) > 300 else " ".join(" ".join(parser.parts).split())
    return short(parser.title, 120), text


# Flights ---------------------------------------------------------------------------------------------

BER_FLIGHTS = "https://ber.berlin-airport.de/de/fliegen/abfluege-ankuenfte/api.flights.json"
BER_PAGE = "https://ber.berlin-airport.de/de/fliegen/abfluege-ankuenfte.html"
OPENSKY = "https://opensky-network.org/api/states/all"
ADSBDB = "https://api.adsbdb.com/v0/callsign/"


def flight_number(value: str) -> str:
    """'fr1144', 'FR 1144' -> 'FR1144' for the BER search; other text stays as typed."""
    compact = re.sub(r"\s+", "", value).upper()
    return compact if re.fullmatch(r"[A-Z0-9]{2}\d{1,4}[A-Z]?", compact) else value.strip()


async def ber_flights(client: httpx.AsyncClient, timezone: str, arrivals: bool = False, query: str = "") -> dict:
    """Next departures/arrivals at BER from the airport website's own data feed."""
    tz = ZoneInfo(timezone)
    now = datetime.now(tz)
    search = flight_number(query)[:40]
    start = now.replace(hour=0, minute=0, second=0) if search else now.replace(second=0)
    tomorrow = (now + timedelta(days=1)).date().isoformat()
    data = await read_json(
        client,
        "GET",
        BER_FLIGHTS,
        params={
            "arrivalDeparture": "A" if arrivals else "D",
            "dateFrom": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "dateUntil": tomorrow,
            "search": search,
            "lang": "de",
            "page": 1,
            "terminal": "",
        },
        headers=HEADERS,
        timeout=12,
        limit=2_000_000,
    )
    items = ((data or {}).get("data") or {}).get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise httpx.TransportError("Unexpected BER data")
    kind = "Ankünfte" if arrivals else "Abflüge"
    entries = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            planned = datetime.fromisoformat(str(item.get("scheduled_time"))).astimezone(tz)
        except ValueError:
            continue
        expected = item.get("arr_estimated_time") if arrivals else item.get("dep_estimated_time")
        late = ""
        try:
            if expected:
                diff = round((datetime.fromisoformat(str(expected)) - planned).total_seconds() / 60)
                late = f" (+{diff})" if diff >= 5 else ""
        except ValueError:
            pass
        place = plain(item.get("dep_airport_name") if arrivals else item.get("arr_airport_name"))
        number = " ".join(plain(item.get("flight_number")).split())
        status = plain(item.get("flight_status_label"))
        gate = plain(item.get("gate")) if not arrivals and search else ""
        bits = [f"{planned:%H:%M} {number} {'aus' if arrivals else '→'} {place}{late}"]
        if status and status.casefold() not in {"geplant", "planmäßig"}:
            bits.append(status)
        if gate:
            bits.append(f"Gate {gate}")
        entries.append(", ".join(bits))
        if len(entries) >= (3 if search else 5):
            break
    if not entries:
        hint = f" zu „{query.strip()}“" if search else ""
        return {"text": f"BER: keine {kind}{hint} bis morgen gefunden.", "sources": [BER_PAGE]}
    return {"text": f"BER {kind} (Live): " + "; ".join(entries) + ".", "sources": [BER_PAGE]}


RESCUE_CALLSIGN = re.compile(r"CHX(\d{1,3})")


async def planes_overhead(
    client: httpx.AsyncClient, lat: float, lon: float, place: str, wide: bool = False, rescue: bool = False
) -> dict:
    """Aircraft currently in the air near the station (OpenSky), with route if adsbdb knows it.

    rescue=True only looks for rescue helicopters (callsign CHX…, "Christoph N") over all of Berlin.
    """
    wide = wide or rescue
    if wide:  # all of Berlin instead of the sky over the station
        lat, lon, place = 52.52, 13.40, "Berlin"
    dlat, dlon = (0.25, 0.45) if wide else (0.15, 0.25)
    box = {"lamin": lat - dlat, "lamax": lat + dlat, "lomin": lon - dlon, "lomax": lon + dlon}
    data = await read_json(client, "GET", OPENSKY, params=box, headers=HEADERS, timeout=12, limit=2_000_000)
    states = data.get("states") if isinstance(data, dict) else None
    if not isinstance(data, dict) or "states" not in data:
        raise httpx.TransportError("OpenSky gave no data")
    planes = []
    for row in states or []:
        if not isinstance(row, list) or len(row) < 14 or row[8] or row[5] is None or row[6] is None:
            continue
        callsign = plain(row[1]).replace(" ", "")
        if not callsign:
            continue
        distance = ((row[6] - lat) * 111) ** 2 + ((row[5] - lon) * 68) ** 2
        planes.append((distance, callsign, row[13] or row[7]))
    planes.sort()
    if rescue:
        planes = [p for p in planes if RESCUE_CALLSIGN.fullmatch(p[1])]
        if not planes:
            return {"text": f"Gerade ist kein Rettungshubschrauber (Christoph) über {place} in der Luft gemeldet. "
                            "Nicht jeder sendet seine Position.",
                    "sources": ["https://opensky-network.org/"]}
    if not planes:
        return {"text": f"Gerade kein Flugzeug mit Rufzeichen in der Luft über {place} gemeldet.",
                "sources": ["https://opensky-network.org/"]}
    entries = []
    for distance, callsign, altitude in planes[:3]:
        route = ""
        try:
            info = await read_json(client, "GET", ADSBDB + quote(callsign), headers=HEADERS, timeout=8, limit=200_000)
            flight = ((info or {}).get("response") or {}).get("flightroute") or {}
            origin = plain((flight.get("origin") or {}).get("municipality"))
            target = plain((flight.get("destination") or {}).get("municipality"))
            airline = plain((flight.get("airline") or {}).get("name"))
            name = plain(flight.get("callsign_iata")) or callsign
            route = f"{name} {airline}".strip()
            low = isinstance(altitude, (int, float)) and altitude < 1500
            local = any(re.search(r"berlin|schönefeld|schoenefeld", x, re.I) for x in (origin, target))
            if origin and target and (local or not low):  # adsbdb routes are sometimes stale
                route += f", {origin} → {target}"
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            route = ""
        christoph = RESCUE_CALLSIGN.fullmatch(callsign)
        if christoph:
            route = f"Rettungshubschrauber Christoph {christoph.group(1)}"
        height = f", {round(altitude / 100) * 100:g} m hoch" if isinstance(altitude, (int, float)) else ""
        entries.append(f"{route or callsign}{height}, {distance ** 0.5:.0f} km entfernt")
    return {"text": f"Flugzeuge bei {place} (OpenSky): " + "; ".join(entries) + ".",
            "sources": ["https://opensky-network.org/", "https://www.adsbdb.com/"]}


# Grill trucks (GaumenSchmaus Hähnchen-Radar) ----------------------------------------------------------

GRILL_API = "https://api.haehnchengriller.de/haehnchengriller/getLocations/v3/"
GRILL_PAGE = "https://standorte.gaumenschmaus.de/liste"
GRILL_COMPANIES = "2,5,6,7,8,9,10,11,12,13,14,27,30"  # the brands the GaumenSchmaus web app shows
WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
DAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _fold(text: str) -> str:
    text = text.casefold().replace("ß", "ss").replace("str.", "strasse")
    for a, b in (("ä", "a"), ("ö", "o"), ("ü", "u"), ("ae", "a"), ("oe", "o"), ("ue", "u")):
        text = text.replace(a, b)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _day_label(day, today) -> str:
    offset = (day - today).days
    name = WEEKDAYS[day.weekday()]
    prefix = {0: "Heute", 1: "Morgen", 2: "Übermorgen"}.get(offset)
    return f"{prefix} ({name[:2]} {day:%d.%m.})" if prefix else f"{name} {day:%d.%m.}"


async def grill_stands(client: httpx.AsyncClient, lat: float, lon: float, tz: str,
                       day: str = "", place: str = "", now: datetime | None = None, near: str = "") -> dict:
    """Where the GaumenSchmaus chicken trucks stand on a day, nearest first, or when one is at a place next.

    day: heute, morgen, übermorgen, a weekday name or "" (next day with a stand).
    place: optional market, street or post code to look for, e.g. "Schlangenbader".
    """
    now = now or datetime.now(ZoneInfo(tz)).replace(tzinfo=None)
    today = now.date()
    near_label = near
    word = _fold(day)
    offsets: list[int]
    if word in {"", "naechste", "nachste", "wann", "wieder"}:
        offsets = list(range(1 if now.hour >= 19 else 0, 8))  # late in the evening "wann?" means from tomorrow
    elif word.startswith("heute"):
        offsets = [0]
    elif word.startswith("ubermorgen"):
        offsets = [2]
    elif word.startswith("morgen"):
        offsets = [1]
    else:
        index = next((i for i, name in enumerate(WEEKDAYS) if word.startswith(_fold(name)[:2])), None)
        if index is None:
            offsets = list(range(0, 8))
        else:
            first = (index - today.weekday()) % 7
            offsets = [first] if not place else [first, first + 7]
    if place and len(offsets) == 1:
        offsets += [o for o in range(offsets[0] + 1, offsets[0] + 8)]  # "not there that day" -> next date
    wanted = [w for w in _fold(place).split() if len(w) > 2 and w not in {"rewe", "netto", "edeka", "markt",
                                                                            "strasse", "berlin"}]
    brand = [w for w in _fold(place).split() if w in {"rewe", "netto", "edeka"}]

    async def fetch(date) -> list[dict]:
        code = "today" if date == today else DAY_CODES[date.weekday()]
        url = f"{GRILL_API}{lat:.4f}/{lon:.4f}/{code}/2.13.2w/meshcore-bot/web?companies={GRILL_COMPANIES}"
        data = await read_json(client, "GET", url, headers=HEADERS, timeout=12, limit=1_000_000)
        if not isinstance(data, dict):
            raise ValueError("Unexpected grill data")
        rows = []
        iso = date.isoformat()
        for row in data.get("locations") or []:
            if not isinstance(row, dict):
                continue
            days = row.get("t") or []
            if days and date.isoweekday() not in days:
                continue
            closed = bool(row.get("v") and row.get("w") and str(row["v"]) <= iso <= str(row["w"]))
            rows.append({
                "name": plain(row.get("b")), "street": " ".join(filter(None, [plain(row.get("e")),
                                                                              plain(row.get("f"))])),
                "zip": plain(row.get("d")), "km": row.get("i"), "from": plain(row.get("p")),
                "until": plain(row.get("q")), "closed": closed, "note": plain(row.get("x")),
            })
        rows.sort(key=lambda r: r["km"] if isinstance(r["km"], (int, float)) else 999)
        return rows

    def line(row, date) -> str:
        hours = f", {row['from']}–{row['until']}" if row["from"] and row["until"] else ""
        if date == today and row["until"] and now.strftime("%H:%M") > row["until"]:
            hours += " (heute schon zu)"
        far = f", {row['km']} km" if isinstance(row["km"], (int, float)) else ""
        return f"{row['name']} {row['street']}{far}{hours}"

    def matches(row) -> bool:
        text = _fold(f"{row['name']} {row['street']} {row['zip']}")
        return all(w in text for w in wanted) and all(b in text for b in brand)

    source = [GRILL_PAGE]
    first_label, missed = "", "nicht dort"
    for offset in offsets:
        date = today + timedelta(days=offset)
        rows = await fetch(date)
        label = _day_label(date, today)
        first_label = first_label or label
        if place:
            hits = [r for r in rows if matches(r)]
            open_hits = [r for r in hits if not r["closed"]]
            if open_hits:
                lead = "" if offset == offsets[0] else f"{first_label} {missed}. Nächster Termin "
                return {"text": f"GaumenSchmaus {lead}{label}: " + line(open_hits[0], date) + ".",
                        "sources": source}
            if hits and offset == offsets[0]:
                missed = "fällt aus" + (f" ({short(hits[0]['note'], 80)})" if hits[0]["note"] else "")
            continue
        open_rows = [r for r in rows if not r["closed"]]
        if open_rows:
            near = "; ".join(line(r, date) for r in open_rows[:3])
            if near.count("(heute schon zu)") == len(open_rows[:3]) > 1:
                near = near.replace(" (heute schon zu)", "") + " – heute aber schon geschlossen"
            where = f"nahe {near_label}" if near_label else "am nächsten"
            return {"text": f"GaumenSchmaus {label}, {where}: {near}.", "sources": source}
        if len(offsets) == 1:
            return {"text": f"GaumenSchmaus {label}: kein Grillwagen in der Nähe.", "sources": source}
    if place:
        return {"text": f"GaumenSchmaus: keinen Stand zu „{place.strip()}“ in den nächsten Tagen gefunden.",
                "sources": source}
    return {"text": "GaumenSchmaus: in den nächsten Tagen kein Grillwagen in der Nähe gefunden.", "sources": source}
