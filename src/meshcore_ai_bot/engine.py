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
from .protocol import Incoming, allows_send, fit_reply, may_remember, question_for
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

    def receive(self, message: Incoming, now: float | None = None) -> str:
        now = time.time() if now is None else now
        self.pacer.received()
        if message.sender.casefold() in self.own_names:
            return "ignored"
        if may_remember(message, self.config, now):
            self.store.observe(message, now)
        question = question_for(message, self.config, now)
        if question is None:
            return "ignored"
        return self.store.admit(message, question, now)

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
        history = self.store.history(
            row["channel"],
            row["sender"],
            now - self.config.storage.retention_hours * 3600,
            self.config.storage.history_turns,
            exclude=row["id"],
        )
        try:
            answer = await self.assistant.answer(question, history)
            text = answer.text
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
        self.store.answer(
            row["id"], row["channel"], history_text, parts, now, now + self.config.storage.reply_ttl_seconds
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
