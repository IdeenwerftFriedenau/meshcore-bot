"""#börsenbot on its own: price alarms and a short report after the Xetra close.

Both send on the 'markets' stream, which only channels with topic = "boerse" accept. Hard limits keep the
radio quiet: three alarms per person, thirty in total, fourteen days at most, three alarm messages per check.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .config import Config
from .markets import dax_movers, fold, market_overview, number, quote, resolve, unit
from .protocol import split_text
from .store import Store

log = logging.getLogger(__name__)

ALARM_KEY = "markets:alarms"
REPORT_KEY = "markets:report_day"
PER_SENDER, TOTAL, MAX_AGE, PER_CHECK = 3, 30, 14 * 86400, 3
CHECK_SECONDS = 300

ALARM = re.compile(
    r"^(?:kurs)?alarm(?:ieren)?\s+(?:fuer\s+|bei\s+wenn\s+)?(?P<name>.+?)\s+"
    r"(?P<op>unter|ueber|<|>|bei|kleiner als|groesser als|faellt unter|steigt ueber)\s+(?P<level>[\d.,]+)"
)
LIST = re.compile(r"^(?:meine\s+)?(?:kurs)?alarme?(?:\s+liste)?\s*\??$|^welche\s+alarme")
CLEAR = re.compile(r"^(?:kurs)?alarme?\s+(?:loeschen|weg|stopp|stop|aus|entfernen)$|^loesche\s+(?:meine\s+)?alarme?$")


def parse_level(text: str) -> float | None:
    text = text.strip(".,")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
        text = text.replace(".", "")
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > 0 else None


def is_alarm_command(question: str) -> bool:
    folded = fold(question.replace("?", ""))
    return bool(ALARM.match(folded) or LIST.match(folded) or CLEAR.match(folded) or folded in {"alarm", "kursalarm"})


class MarketWatch:
    def __init__(self, config: Config, store: Store, client: httpx.AsyncClient):
        self.config, self.store, self.client = config, store, client
        self.last_check = 0.0
        self.running: asyncio.Task | None = None

    # ---- alarms -------------------------------------------------------------------------------------
    def alarms(self) -> list[dict]:
        try:
            rows = json.loads(self.store.meta_get(ALARM_KEY) or "[]")
        except ValueError:
            return []
        return [r for r in rows if isinstance(r, dict)]

    def save(self, rows: list[dict]):
        self.store.meta_set(ALARM_KEY, json.dumps(rows, ensure_ascii=False))

    async def command(self, sender: str, channel: str, question: str, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        folded = fold(question.replace("?", ""))
        mine = [a for a in self.alarms() if a.get("sender", "").casefold() == sender.casefold()]
        if CLEAR.match(folded):
            self.save([a for a in self.alarms() if a not in mine])
            return f"{len(mine)} Kursalarm(e) gelöscht." if mine else "Du hast keine Kursalarme."
        if LIST.match(folded) or folded in {"alarm", "kursalarm"}:
            if not mine:
                return "Du hast keine Kursalarme. Beispiel: Alarm Rheinmetall unter 800"
            return "Deine Alarme: " + ", ".join(
                f"{a['label']} {'unter' if a['op'] == '<' else 'über'} {number(a['level'])}" for a in mine)
        match = ALARM.match(folded)
        if not match:
            return None
        level = parse_level(match["level"])
        if level is None:
            return "Den Kurswert verstehe ich nicht. Beispiel: Alarm SAP über 220"
        if len(mine) >= PER_SENDER:
            return f"Du hast schon {PER_SENDER} Kursalarme. Mit „Alarme löschen“ machst du Platz."
        rows = [a for a in self.alarms() if now - a.get("created", 0) < MAX_AGE]
        if len(rows) >= TOTAL:
            return "Gerade sind zu viele Kursalarme aktiv. Versuch es später nochmal."
        try:
            found = await resolve(self.client, match["name"])
            if not found:
                return f"Zu „{match['name']}“ finde ich keinen Kurs."
            data = await quote(self.client, *found)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return "Die Kursdaten sind gerade nicht abrufbar. Versuch es bitte gleich nochmal."
        op = match["op"]
        if op in {"bei"}:
            op = "<" if level < data["price"] else ">"
        op = "<" if op in {"unter", "<", "kleiner als", "faellt unter"} else ">"
        if (op == "<" and data["price"] <= level) or (op == ">" and data["price"] >= level):
            return (f"{found[1]} steht schon bei {number(data['price'], data['kind'])}{unit(data)}, "
                    "der Alarm würde sofort auslösen.")
        alarm = {"id": hashlib.sha256(f"{sender}{found[0]}{op}{level}{now}".encode()).hexdigest()[:12],
                 "sender": sender, "channel": channel, "symbol": found[0], "label": found[1], "op": op,
                 "level": level, "created": now, "kind": data["kind"], "unit": unit(data)}
        self.save(rows + [alarm])
        word = "unter" if op == "<" else "über"
        return (f"Kursalarm gesetzt: {found[1]} {word} {number(level, data['kind'])}{unit(data)} "
                f"(jetzt {number(data['price'], data['kind'])}{unit(data)}). Gilt 14 Tage, ich melde mich hier.")

    async def check_alarms(self, now: float):
        rows = [a for a in self.alarms() if now - a.get("created", 0) < MAX_AGE]
        fired, keep = 0, []
        prices: dict[str, dict] = {}
        for alarm in rows:
            if fired >= PER_CHECK:
                keep.append(alarm)
                continue
            try:
                data = prices.get(alarm["symbol"]) or await quote(self.client, alarm["symbol"], alarm["label"])
                prices[alarm["symbol"]] = data
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                keep.append(alarm)
                continue
            hit = data["price"] <= alarm["level"] if alarm["op"] == "<" else data["price"] >= alarm["level"]
            if not hit:
                keep.append(alarm)
                continue
            word = "unter" if alarm["op"] == "<" else "über"
            text = (f"@[{alarm['sender']}] Kursalarm: {alarm['label']} steht bei "
                    f"{number(data['price'], data['kind'])}{unit(data)}, {word} deiner Marke "
                    f"{number(alarm['level'], data['kind'])}{unit(data)}.")
            if self._send(f"alarm:{alarm['id']}", alarm["channel"], text, now, 1800):
                fired += 1
            else:
                keep.append(alarm)
        if len(keep) != len(self.alarms()) or keep != self.alarms():
            self.save(keep)

    # ---- close report -------------------------------------------------------------------------------
    def report_due(self, now: float) -> bool:
        at = (self.config.research.market_report_time or "").strip()
        if not re.fullmatch(r"\d{1,2}:\d\d", at):
            return False
        local = datetime.fromtimestamp(now, ZoneInfo(self.config.station.timezone))
        hour, minute = map(int, at.split(":"))
        start = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return (
            local.weekday() < 5
            and 0 <= (local - start).total_seconds() < 3600
            and self.store.meta_get(REPORT_KEY) != local.date().isoformat()
        )

    async def close_report(self, now: float):
        tz = self.config.station.timezone
        local = datetime.fromtimestamp(now, ZoneInfo(tz))
        self.store.meta_set(REPORT_KEY, local.date().isoformat())
        try:
            dax = await quote(self.client, "^GDAXI", "DAX")
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return
        traded = datetime.fromtimestamp(dax.get("time") or 0, ZoneInfo(tz)).date()
        if traded != local.date():
            return  # holiday: no trading, no report
        parts = []
        try:
            parts.append((await market_overview(self.client, tz))["text"].replace("Märkte: ", ""))
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            pass
        try:
            parts.append((await dax_movers(self.client))["text"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            pass
        if not parts:
            return
        text = f"Börsenschluss {local:%d.%m.}: " + " ".join(parts)
        for channel in self.config.channels:
            if channel.topic == "boerse" and channel.mode != "off":
                self._send(f"report:{local.date()}:{channel.name}", channel.name, text, now, 3 * 3600)

    # ---- plumbing -----------------------------------------------------------------------------------
    def _send(self, identity: str, channel: str, text: str, now: float, ttl: float) -> bool:
        parts = split_text(text, self.config.radio.packet_bytes)[: self.config.radio.broadcast_max_packets]
        return self.store.broadcast(identity, "1", now + ttl, {channel: parts}, "markets", now)

    async def tick(self, now: float | None = None):
        now = time.time() if now is None else now
        try:
            if self.report_due(now):
                await self.close_report(now)
            if now - self.last_check >= CHECK_SECONDS and self.alarms():
                self.last_check = now
                await self.check_alarms(now)
        except Exception:  # noqa: BLE001 - a market hiccup must never stop the radio engine
            log.exception("Market watch failed")

    def poke(self, now: float):
        """Start a tick in the background at most once a minute; never blocks the reply loop."""
        if self.running and not self.running.done():
            return
        if now - getattr(self, "_poked", 0.0) < 60:
            return
        self._poked = now
        self.running = asyncio.get_running_loop().create_task(self.tick(now))
