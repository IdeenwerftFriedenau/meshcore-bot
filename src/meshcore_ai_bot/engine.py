"""Durable request and transmit pipeline. Only the explicit radio runner opens USB."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

from .assistant import Assistant
from .config import Config
from .market_watch import MarketWatch, is_alarm_command
from .memory import (
    PROFILE_LABELS,
    control,
    describe_profile,
    extract_facts,
    forget_request,
    remember_request,
)
from .protocol import (
    Incoming,
    allows_send,
    fit_reply,
    followup_command,
    ignored_reason,
    may_remember,
    question_for,
    remaining_text,
)
from .store import Store

log = logging.getLogger(__name__)


class Pacer:
    def __init__(
        self,
        config: Config,
        last_tx: float | None = None,
        *,
        monotonic=time.monotonic,
        wall=time.time,
        sleep=asyncio.sleep,
        jitter=lambda: random.uniform(0.5, 1.5),
    ):
        self.config, self.clock, self.wall, self.sleep, self.jitter = config, monotonic, wall, sleep, jitter
        self.lock = asyncio.Lock()
        self.last_rx = None
        interval = config.radio.send_interval_seconds
        remaining = max(0, min(interval, interval - (wall() - last_tx))) if last_tx is not None else 0
        self.next_tx = monotonic() + remaining

    def received(self):
        self.last_rx = self.clock()

    async def transmit(self, send: Callable[[], Awaitable[bool]], valid: Callable[[], bool]):
        async with self.lock:
            started = self.clock()
            earliest = started + self.jitter()
            while True:
                now = self.clock()
                quiet = (
                    self.last_rx + self.config.radio.receive_quiet_seconds
                    if self.last_rx is not None
                    else now
                )
                max_wait = self.config.radio.max_rx_wait_seconds
                if max_wait and now - started >= max_wait:
                    quiet = now
                delay = max(earliest, self.next_tx, quiet) - now
                if delay <= 0:
                    break
                await self.sleep(delay)
            if not valid():
                return None
            try:
                return await send()
            finally:
                self.next_tx = self.clock() + self.config.radio.send_interval_seconds


class Engine:
    def __init__(self, config: Config, store: Store, assistant: Assistant, *, pacer: Pacer | None = None):
        self.config, self.store, self.assistant = config, store, assistant
        self.store.limits = config.radio
        self.store.profile_days = config.storage.profile_days
        self.store.enforce_limits(time.time())
        self.pacer = pacer or Pacer(config, store.last_tx())
        self.own_names: set[str] = set()
        # Per channel and sender: (expires, text not yet sent, sources, question) for "mehr" and "Quelle?".
        self.followups: dict[tuple[str, str], tuple[float, str, list[str], str]] = {}
        client = getattr(assistant, "client", None)
        self.watch = MarketWatch(config, store, client) if client is not None else None

    def receive(self, message: Incoming, now: float | None = None) -> str:
        now = time.time() if now is None else now
        self.pacer.received()
        if message.sender.casefold() in self.own_names:
            return "ignored"
        question = question_for(message, self.config, now)
        channel = self.config.channel(message.channel)
        if (
            self.config.storage.memory_enabled
            and channel
            and channel.memory_scope != "off"
            and may_remember(message, self.config, now)
            and not control(question or message.text)
        ):
            self.store.observe(message, now)
        if question is None:
            if channel and message.channel not in {"Public", "#public"}:
                reason = ignored_reason(message, self.config, now)
                self.store.record_receive(message, "ignored", reason, now)
            return "ignored"
        decision = self.store.admit(message, question, now)
        reason = "rate_or_queue_limit" if decision == "limited" else decision
        self.store.record_receive(message, decision, reason, now)
        return decision

    async def process_one(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if self.watch and any(ch.topic == "boerse" and ch.mode != "off" for ch in self.config.channels):
            self.watch.poke(now)
        row = self.store.next_message(now, self.config.storage.message_max_age_seconds)
        if not row:
            return False
        original = Incoming(row["channel"], row["sender"], row["raw_text"], row["stamp"])
        question = question_for(original, self.config, now)
        if question is None or row["sender"].casefold() in self.own_names:
            self.store.cancel_message(row["id"])
            return True
        channel = self.config.channel(row["channel"])
        shared = (
            [ch.name for ch in self.config.channels if ch.shares_memory and ch.memory_scope != "off"]
            if channel.shares_memory
            else []
        )
        memory_on = self.config.storage.memory_enabled and channel.memory_scope != "off"
        history = (
            self.store.history(
                row["channel"],
                row["sender"],
                now - self.config.storage.retention_hours * 3600,
                self.config.storage.history_turns,
                exclude=row["id"],
                shared_channels=shared,
            )
            if memory_on
            else []
        )
        key = (row["channel"], row["sender"].casefold())
        last = self.followups.get(key) or self.store.followup(row["channel"], row["sender"], now)
        if last and last[0] < now:
            last = None
        # Durable facts follow the same boundary as the chat history: shared hashtag channels or this channel.
        scope = "shared" if channel.shares_memory else row["channel"]
        profile: dict[str, list[str]] = {}
        sources: list[str] = []
        command = follow = None
        try:
            command = control(question)
            follow = followup_command(question)
            forget_key = forget_request(question) if not command else None
            remember = remember_request(question) if not command and not forget_key else None
            stated = (
                extract_facts(question)
                if memory_on and not (command or forget_key or remember or follow)
                else []
            )
            if stated:
                stated = self.store.remember_facts(scope, row["sender"], stated, now)
            if memory_on and not command:
                profile = self.store.profile(scope, row["sender"], now)
            if follow == "more" and last and last[1]:
                text, sources = last[1], last[2]
            elif follow == "source":
                text = (
                    source_names(last[2])
                    if last and last[2]
                    else "Für meine letzte Antwort habe ich keine Online-Quelle benutzt."
                )
                sources = last[2] if last else []
            elif command == "forget":
                self.store.forget_memory(row["sender"], now, exclude=row["id"])
                text = "Deine Erinnerungen sind gelöscht; ich merke mir nichts Neues. Mit 'memory an' aktivierst du es wieder."
            elif command == "enable":
                self.store.enable_memory(row["sender"])
                text = "Deine persönliche Erinnerung ist wieder erlaubt. Es gelten weiterhin die Memory-Einstellungen dieses Kanals."
            elif command == "inspect":
                known = describe_profile(self.store.profile(scope, row["sender"])) if memory_on else ""
                facts = [item["content"] for item in history if item["role"] == "user"][-3:]
                parts = []
                if known:
                    parts.append("Gemerkt: " + known + ".")
                if facts:
                    parts.append("Aus deinen letzten Nachrichten in diesem Memory-Bereich: " + " | ".join(facts))
                text = (
                    " ".join(parts).rstrip(" .") + "." + (" 'vergiss mich' löscht alles." if known else "")
                    if parts
                    else "In diesem Memory-Bereich habe ich keine nutzbaren Erinnerungen an dich."
                )
            elif forget_key:
                if not memory_on:
                    text = "In diesem Kanal speichere ich nichts über dich."
                elif self.store.forget_fact(scope, row["sender"], forget_key):
                    text = f"Erledigt, {PROFILE_LABELS[forget_key]} ist vergessen."
                else:
                    text = f"Zu {PROFILE_LABELS[forget_key]} hatte ich nichts gespeichert."
            elif remember is not None:
                facts, note = remember
                wanted = facts + ([("notiz", note)] if note else [])
                if not memory_on:
                    text = "In diesem Kanal speichere ich nichts über dich."
                elif not wanted:
                    text = "Das kann ich mir so nicht merken. Beispiel: merk dir meine Haltestelle Rathaus Friedenau."
                else:
                    saved = self.store.remember_facts(scope, row["sender"], wanted, now)
                    text = (
                        "Gemerkt: " + saved_text(saved) + ". 'Was weißt du über mich' zeigt alles."
                        if saved
                        else "Deine Erinnerung ist ausgeschaltet. Mit 'memory an' geht es wieder."
                    )
            elif stated and is_statement(question):
                text = "Gemerkt: " + saved_text(stated) + ". " + usage_hint(stated)
            else:
                if follow == "more":
                    topic = last[3] if last and last[3] else "deiner letzten Antwort"
                    question = f"Erzähl mir bitte weitere, neue Details zu: {topic}. Wiederhole nichts aus deiner letzten Antwort."
                alarm = (
                    await self.watch.command(row["sender"], row["channel"], question, now)
                    if self.watch and channel.topic == "boerse" and is_alarm_command(question)
                    else None
                )
                if alarm is not None:
                    answer = SimpleNamespace(text=alarm, sources=[])
                else:
                    extra = {"topic": channel.topic} if channel.topic else {}
                    if profile:
                        extra["profile"] = profile
                    answer = await self.assistant.answer(question, history, **extra)
                text, sources = answer.text, list(getattr(answer, "sources", None) or [])
                if follow == "more" and last:
                    sources = list(dict.fromkeys(sources + last[2]))[:5]
        except Exception:
            # A plugin bug must not kill the daemon or expose a traceback on air.
            log.exception("Answer generation failed for message %s", row["id"][:12])
            text = "Bei der Antwort ist etwas schiefgegangen. Versuch es bitte später nochmal."
        recipient = row["sender"].replace("[", "").replace("]", "").encode()[:32].decode("utf-8", "ignore")
        drawing = bool(re.search(r"ascii|zeichenbild", question, re.I))
        prefix = f"@[{recipient}] " + ("\n\n" if drawing else "")
        # Historical injected punctuation must not become a permanent decoration.
        if not drawing:
            text = re.sub(r"[!?:;]{4,}", ".", text)
            text = plain_radio_text(text)
        history_text, parts = fit_reply(
            prefix, text, self.config.radio.packet_bytes, self.config.radio.reply_max_packets
        )
        if follow != "source":
            if command == "forget":
                self.followups = {k: v for k, v in self.followups.items() if k[1] != key[1]}
            else:
                if len(self.followups) >= 256:
                    self.followups = {k: v for k, v in self.followups.items() if v[0] >= now}
                topic = last[3] if follow == "more" and last else question
                self.followups[key] = (
                    now + self.config.storage.reply_ttl_seconds,
                    remaining_text(text, history_text),
                    sources,
                    topic[:200],
                )
                try:
                    self.store.save_followup(row["channel"], row["sender"], self.followups[key])
                except Exception:  # noqa: BLE001 - 'mehr' after a restart is a convenience only
                    log.warning("Could not persist follow-up state")
        completed = time.time()
        self.store.answer(
            row["id"],
            row["channel"],
            history_text,
            parts,
            completed,
            completed + self.config.storage.reply_ttl_seconds,
        )
        return True

    def _valid(self, packet: dict, now: float) -> bool:
        if not self.store.still_sendable(packet["id"], now):
            return False
        if not allows_send(self.config, packet["channel"], packet["stream"]):
            return False
        if packet["stream"] == "reply":
            row = self.store.message(packet["message_id"])
            if not row:
                return False
            original = Incoming(row["channel"], row["sender"], row["raw_text"], row["stamp"])
            # The reply TTL governs queue age; recheck current channel/keyword rules independently.
            return question_for(original, self.config, float(row["stamp"])) is not None
        return True

    async def deliver_one(self, transport) -> bool:
        packet = self.store.claim(time.time(), lambda ch, stream: allows_send(self.config, ch, stream))
        if not packet:
            return False
        try:
            result = await self.pacer.transmit(
                lambda: transport.send(packet["channel"], packet["text"]),
                lambda: self._valid(packet, time.time()),
            )
            state = "cancelled" if result is None else "accepted" if result else "failed"
        except asyncio.CancelledError:
            self.store.finish(packet["id"], "uncertain", time.time())
            raise
        except Exception:
            log.warning("Radio acknowledgement uncertain for packet %s", packet["id"][:12])
            state = "uncertain"
        self.store.finish(packet["id"], state, time.time())
        log.info("Packet %s channel=%s state=%s", packet["id"][:12], packet["channel"], state)
        return True


KNOWN_SOURCES = {
    "tagesschau.de": "tagesschau",
    "rbb24.de": "rbb24",
    "bvg.transport.rest": "BVG/VBB",
    "open-meteo.com": "Open-Meteo",
    "finance.yahoo.com": "Yahoo Finance",
    "openstreetmap.org": "OpenStreetMap",
    "swr.de": "SWR",
    "ndr.de": "NDR",
    "wdr.de": "WDR",
    "br.de": "BR",
    "mdr.de": "MDR",
    "hr.de": "HR",
    "zdf.de": "ZDF",
    "zdfheute.de": "ZDF",
    "deutschlandfunk.de": "Deutschlandfunk",
    "dw.com": "DW",
    "berlin.de": "berlin.de",
    "tagesspiegel.de": "Tagesspiegel",
    "berliner-zeitung.de": "Berliner Zeitung",
    "morgenpost.de": "Berliner Morgenpost",
    "spiegel.de": "Spiegel",
    "zeit.de": "Zeit",
    "sueddeutsche.de": "SZ",
    "faz.net": "FAZ",
    "heise.de": "heise",
    "golem.de": "Golem",
    "meshcore.co.uk": "MeshCore",
    "github.com": "GitHub",
}


def source_name(url: str) -> str:
    """Links cannot be clicked on air: name where the facts came from instead."""
    from urllib.parse import unquote, urlsplit

    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    if host.endswith("wikipedia.org"):
        title = unquote(parts.path.rsplit("/", 1)[-1]).replace("_", " ").strip()
        return f"Wikipedia ({title})" if title and len(title) <= 40 else "Wikipedia"
    for known, name in KNOWN_SOURCES.items():
        if host == known or host.endswith("." + known):
            return name
    return host or "unbekannt"


def source_names(urls: list[str]) -> str:
    names = list(dict.fromkeys(source_name(url) for url in urls))[:3]
    return ("Quelle: " if len(names) == 1 else "Quellen: ") + ", ".join(names)


def plain_radio_text(text: str) -> str:
    """Markdown costs bytes on air: drop emphasis, bullets and headings, keep the words."""
    if "**" not in text and "__" not in text and "\n" not in text.strip():
        return text
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), text)
    if "\n" in text.strip():
        text = re.sub(r"(?m)^\s*(?:[*\-•]|\d{1,2}\.)\s+", "", text)
        text = re.sub(r"(?m)^#{1,6}\s+", "", text)
    text = re.sub(r"\s*\n\s*", " ", text.strip())
    return re.sub(r" {2,}", " ", text)


def saved_text(saved: list[tuple[str, str]]) -> str:
    grouped: dict[str, list[str]] = {}
    for key, value in saved:
        grouped.setdefault(key, []).append(value)
    return ", ".join(f"{PROFILE_LABELS[key]} {' / '.join(values)}" for key, values in grouped.items())


def is_statement(text: str) -> bool:
    """'Ich wohne in Steglitz.' gets a short confirmation; a question with a fact in it gets a real answer."""
    text = " ".join(text.split())
    return (
        len(text) <= 120
        and "?" not in text
        and not re.match(r"(wie|was|wann|wo|wer|warum|wieso|welche\w*|kannst|gibt|ist|sind|wird|zeig|sag)\b", text, re.I)
    )


def usage_hint(saved: list[tuple[str, str]]) -> str:
    keys = {key for key, _ in saved}
    if "haltestelle" in keys:
        return "'Nächste Bahn' zeigt dir jetzt die Abfahrten dort."
    if "ort" in keys:
        return "Wetter und Verbindungen beziehe ich jetzt darauf."
    if "aktien" in keys:
        return "'Meine Aktien' zeigt dir die Kurse."
    return "'Was weißt du über mich' zeigt alles, 'vergiss mich' löscht es."
