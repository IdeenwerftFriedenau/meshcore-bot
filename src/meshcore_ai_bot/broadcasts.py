"""Opt-in official warnings and RSS headlines. No RF operations in fetchers."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import httpx

from .config import Config
from .http import read_bytes, read_json
from .protocol import allows_send, split_text
from .regions import STATES, bbk_states, canonical, ordered
from .store import Store

BERLIN = ZoneInfo("Europe/Berlin")
log = logging.getLogger(__name__)


def object_value(value):
    if not isinstance(value, dict):
        raise ValueError("Expected a source object")
    return value


def warning_priority(warning):
    value = warning.get("severity", "")
    return {
        "Extreme": 4,
        "Severe": 3,
        "Moderate": 2,
        "Minor": 1,
        "Stufe 4": 4,
        "Stufe 3": 3,
        "Stufe 2": 2,
        "Stufe 1": 1,
    }.get(value, 0)


def warning_parts(warning, config):
    """Use short labels and an explicit source-linked summary for oversized alerts."""
    codes = warning["codes"]
    label = "/".join(codes) if len(codes) <= 2 else "DE"
    full = warning_text(warning, config.broadcasts.include_instructions)
    if len(codes) > 2:
        full += " | Bundesländer: " + "/".join(codes)
    parts = split_text(full, config.radio.packet_bytes, label)
    if len(parts) <= config.radio.broadcast_max_packets:
        return parts
    # Never disguise omissions as a complete official warning. Instructions are
    # referenced, not paraphrased; the source remains available for the full text.
    for size in (160, 100, 60, 30):
        title = warning["title"][:size]
        area = warning["area"][:size]
        summary = (
            f"WARNUNG {warning['source']} {warning['severity']}: {title}"
            f" | Gebiet: {area} | Bundesländer: {'/'.join(codes)}"
            f" | Gültig von {label_date(warning.get('onset'))} bis {label_date(warning.get('expires'))}"
            f" | Kurzfassung; Volltext und Handlungsempfehlungen: {warning['url']}"
        )
        parts = split_text(summary, config.radio.packet_bytes, label)
        if len(parts) <= config.radio.broadcast_max_packets:
            return parts
    # The store records the rejection and retries next poll without marking it sent.
    return parts


def plain(value) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", str(value or ""))).split())


def date_value(value) -> float | None:
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.timestamp() if dt.tzinfo else None


def label_date(value: float | None) -> str:
    return datetime.fromtimestamp(value, BERLIN).strftime("%d.%m.%Y %H:%M %Z") if value else "nicht angegeben"


def warning_text(warning: dict, instructions: bool = False) -> str:
    pieces = [
        f"WARNUNG {warning['source']} {warning['severity']}: {warning['title']}",
        "Gebiet: " + warning["area"],
        "Gültig von " + label_date(warning.get("onset")) + " bis " + label_date(warning.get("expires")),
        "Beschreibung: " + (warning.get("description") or "nicht angegeben"),
    ]
    if instructions:
        pieces.append("Handlungsempfehlung: " + (warning.get("instruction") or "nicht angegeben"))
    pieces.append("Quelle: " + warning["url"])
    return " | ".join(pieces)


def dwd_warnings(data: dict, now: float, regions: list[str]) -> list[dict]:
    data = object_value(data)
    if abs(now - float(data["time"]) / 1000) > 3600:
        raise ValueError("Stale DWD feed")
    grouped = {}
    for values in object_value(data["warnings"]).values():
        if not isinstance(values, list):
            raise ValueError("Invalid DWD warning list")
        for row in values:
            row = object_value(row)
            code = canonical(row.get("stateShort", ""))
            if code not in STATES:
                code = next((k for k, (name, _) in STATES.items() if name == row.get("state")), None)
            if code not in regions or float(row.get("end", 0)) / 1000 <= now:
                continue
            key = tuple(
                str(row.get(k, ""))
                for k in ("headline", "level", "start", "end", "description", "instruction")
            )
            if key not in grouped:
                grouped[key] = {
                    "source": "DWD",
                    "title": plain(row.get("headline", "Warnung")),
                    "severity": "Stufe " + str(row.get("level", "?")),
                    "areas": set(),
                    "codes": set(),
                    "onset": float(row["start"]) / 1000 if row.get("start") else None,
                    "expires": float(row["end"]) / 1000,
                    "description": plain(row.get("description")),
                    "instruction": plain(row.get("instruction")),
                    "url": "https://www.dwd.de/DE/wetter/warnungen_gemeinden/warnWetter_node.html",
                }
            grouped[key]["areas"].add(plain(row.get("regionName")))
            grouped[key]["codes"].add(code)
    result = []
    for row in grouped.values():
        row["area"] = "; ".join(sorted(row.pop("areas")))
        row["codes"] = ordered(row["codes"])
        # Start+title distinguish a new event; content revision handles updates.
        row["identity"] = (
            "dwd:"
            + hashlib.sha256(
                json.dumps([row["title"], row["onset"], row["codes"]], sort_keys=True).encode()
            ).hexdigest()
        )
        result.append(row)
    return result


def selected_regions(config, source):
    return getattr(config.broadcasts, source + "_regions", []) or config.broadcasts.warning_regions


class Broadcasts:
    def __init__(self, config: Config, store: Store, client: httpx.AsyncClient):
        self.config, self.store, self.client = config, store, client
        self.store.limits = config.radio
        self._semaphore = asyncio.Semaphore(4)
        self._bbk_cache = {}
        self._unassigned_bbk = set()

    async def _json(self, url):
        async with self._semaphore:
            return await read_json(self.client, "GET", url)

    async def fetch_dwd(self) -> list[dict]:
        raw = await read_bytes(
            self.client,
            "GET",
            "https://www.dwd.de/DWD/warnungen/warnapp/json/warnings.json",
            limit=10_000_000,
        )
        text = raw.decode()
        data = json.loads(text[text.index("{") : text.rindex("}") + 1])
        return dwd_warnings(data, time.time(), selected_regions(self.config, "dwd"))

    async def fetch_bbk(self) -> list[dict]:
        regions = selected_regions(self.config, "bbk")
        indexes = await asyncio.gather(
            *(
                self._json("https://warnung.bund.de/api31/" + source + "/mapData.json")
                for source in ["mowas", "katwarn", "biwapp", "lhp"]
            )
        )
        versions = {}
        for alerts in indexes:
            if not isinstance(alerts, list):
                raise ValueError("Invalid BBK index")
            for alert in alerts:
                alert = object_value(alert)
                identity = alert.get("id", "")
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,250}", identity):
                    raise ValueError("Invalid warning identifier")
                versions[identity] = str(alert.get("version", ""))
        if len(versions) > 1500:
            raise ValueError("Oversized warning index")

        async def detail(key):
            cached = self._bbk_cache.get(key)
            if cached and cached[0] == versions[key] and time.time() - cached[1] < 600:
                return cached[2]
            value = await self._json("https://warnung.bund.de/api31/warnings/" + key + ".json")
            self._bbk_cache[key] = (versions[key], time.time(), value)
            return value

        details = await asyncio.gather(*(detail(key) for key in versions))
        self._bbk_cache = {key: value for key, value in self._bbk_cache.items() if key in versions}
        self._unassigned_bbk = set()
        result = []
        for key, detail in zip(versions, details, strict=True):
            detail = object_value(detail)
            if detail.get("status") != "Actual" or detail.get("msgType") == "Cancel":
                continue
            infos = detail.get("info", [])
            if not isinstance(infos, list):
                raise ValueError("Invalid BBK information list")
            info = next(
                (i for i in infos if str(object_value(i).get("language", "de")).startswith("de")), None
            )
            if info is None:
                raise ValueError("Missing German warning details")
            targets = bbk_states(info)
            if not targets:
                self._unassigned_bbk.add("bbk:" + key)
                continue
            codes = ordered(targets.intersection(regions))
            if not codes:
                continue
            expires = date_value(info.get("expires"))
            if expires and expires <= time.time():
                continue
            result.append(
                {
                    "identity": "bbk:" + key,
                    "source": "BBK/NINA",
                    "title": plain(info.get("headline", "Warnung")),
                    "severity": plain(info.get("severity", "?")),
                    "description": plain(info.get("description")),
                    "instruction": plain(info.get("instruction")),
                    "onset": date_value(info.get("onset") or info.get("effective")),
                    "expires": expires,
                    "codes": codes,
                    "area": "; ".join(plain(a.get("areaDesc")) for a in info.get("area", [])),
                    "url": "https://warnung.bund.de/meldungen/" + key,
                }
            )
        return result

    def queue_warnings(self, source: str, warnings: list[dict], now: float) -> int:
        key = (
            "source_initialized:"
            + source
            + ":"
            + hashlib.sha256(json.dumps(sorted(selected_regions(self.config, source))).encode()).hexdigest()
        )
        baseline = self.store.meta_get(key) is None
        queued = 0
        identities = set()
        for warning in warnings:
            identity = warning["identity"]
            identities.add(identity)
            revision = hashlib.sha256(
                json.dumps(warning, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            batches = {
                channel.name: warning_parts(warning, self.config)
                for channel in self.config.channels
                if allows_send(self.config, channel.name, "warnings")
            }
            queued += self.store.broadcast(
                identity,
                revision,
                warning.get("expires") or now + 86400,
                batches,
                "warnings",
                now,
                baseline=baseline,
                priority=warning_priority(warning),
            )
        # A healthy complete source can withdraw alerts; a fetch exception never comes here.
        with self.store.transaction() as c:
            previous = c.execute(
                "SELECT identity,revision FROM broadcasts WHERE identity LIKE ?", (source + ":%",)
            ).fetchall()
            for row in previous:
                if row["identity"] not in identities and row["identity"] not in self._unassigned_bbk:
                    batch = row["identity"] + ":" + row["revision"]
                    c.execute(
                        "UPDATE packets SET expires=MIN(expires,?) WHERE batch=? AND state IN ('queued','sending')",
                        (now, batch),
                    )
                    c.execute(
                        "UPDATE packets SET state='cancelled',updated=? WHERE batch=? AND state='queued'",
                        (now, batch),
                    )
        self.store.meta_set(key, now)
        return queued

    async def poll_warnings(self) -> dict[str, int | str]:
        if not self.config.broadcasts.warnings_enabled:
            return {}
        results = {}
        for source, fetch in [("dwd", self.fetch_dwd), ("bbk", self.fetch_bbk)]:
            if not getattr(self.config.broadcasts, source + "_enabled") or not selected_regions(
                self.config, source
            ):
                results[source] = "disabled"
                continue
            try:
                warnings = await fetch()
                count = self.queue_warnings(source, warnings, time.time())
                results[source] = "partial" if source == "bbk" and self._unassigned_bbk else count
            except Exception:
                # Preserve state; do not turn a broken source into an all-clear.
                log.warning("Warning source %s unavailable", source, exc_info=True)
                results[source] = "unavailable"
        self.store.meta_set("warnings:status", json.dumps(results))
        return results

    async def poll_news(self) -> int:
        if not self.config.broadcasts.news_enabled:
            return 0
        now = time.time()
        queued = 0
        for url in self.config.broadcasts.rss_feeds:
            try:
                raw = await read_bytes(self.client, "GET", url, limit=1_000_000)
                # Entity declarations are unnecessary for RSS and not accepted.
                if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                    raise ValueError("XML entity declaration")
                root = ET.fromstring(raw)
                items = root.findall(".//item")
                initialized = "rss_initialized:" + hashlib.sha256(url.encode()).hexdigest()
                baseline = self.store.meta_get(initialized) is None
                for item in items[:20]:
                    title, link = plain(item.findtext("title")), plain(item.findtext("link"))
                    published = parsedate_to_datetime(item.findtext("pubDate") or "")
                    if not published.tzinfo or not 0 <= now - published.timestamp() <= 3600:
                        continue
                    if not title or not link.startswith("https://"):
                        continue
                    identity = "rss:" + hashlib.sha256((url + link).encode()).hexdigest()
                    text = title + " | Quelle: " + link
                    revision = hashlib.sha256(text.encode()).hexdigest()
                    batches = {
                        ch.name: split_text(text, self.config.radio.packet_bytes, "NEWS")
                        for ch in self.config.channels
                        if allows_send(self.config, ch.name, "news")
                    }
                    queued += self.store.broadcast(
                        identity,
                        revision,
                        published.timestamp() + 3600,
                        batches,
                        "news",
                        now,
                        baseline=baseline,
                    )
                self.store.meta_set(initialized, now)
            except Exception:
                log.warning("RSS source unavailable", exc_info=True)
                continue
        return queued
