"""Durable request and transmit pipeline. Only the explicit radio runner opens USB."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable

from .assistant import Assistant
from .config import Config
from .memory import control
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
        self.store.enforce_limits(time.time())
        self.pacer = pacer or Pacer(config, store.last_tx())
        self.own_names: set[str] = set()
        # Per channel and sender: (expires, text not yet sent, sources) for "mehr" and "Quelle?".
        self.followups: dict[tuple[str, str], tuple[float, str, list[str]]] = {}

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
        last = self.followups.get(key)
        if last and last[0] < now:
            last = None
        sources: list[str] = []
        command = follow = None
        try:
            command = control(question)
            follow = followup_command(question)
            if follow == "more" and last and last[1]:
                text, sources = last[1], last[2]
            elif follow == "source":
                text = (
                    "Quellen: " + " | ".join(short_url(url) for url in last[2][:3])
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
                facts = [item["content"] for item in history if item["role"] == "user"][-3:]
                text = (
                    "Aus deinen letzten Nachrichten in diesem Memory-Bereich: " + " | ".join(facts)
                    if facts
                    else "In diesem Memory-Bereich habe ich keine nutzbaren Erinnerungen an dich."
                )
            else:
                if follow == "more":
                    question = "Erzähl mir bitte mehr Details zu deiner letzten Antwort."
                answer = await self.assistant.answer(question, history)
                text, sources = answer.text, list(getattr(answer, "sources", None) or [])
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
        history_text, parts = fit_reply(
            prefix, text, self.config.radio.packet_bytes, self.config.radio.reply_max_packets
        )
        if follow != "source":
            if command == "forget":
                self.followups = {k: v for k, v in self.followups.items() if k[1] != key[1]}
            else:
                if len(self.followups) >= 256:
                    self.followups = {k: v for k, v in self.followups.items() if v[0] >= now}
                self.followups[key] = (
                    now + self.config.storage.reply_ttl_seconds,
                    remaining_text(text, history_text),
                    sources,
                )
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


def short_url(url: str) -> str:
    """Radio packets are small: drop the scheme and a leading www."""
    return re.sub(r"^https://(?:www\.)?", "", url).rstrip("/")
