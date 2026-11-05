"""Run the v0.4 engine inside the existing Pi gateway, with one USB owner.

The collector supplies its existing, shared paced MeshCore connection. This
adapter never opens a serial port and never uses the old cloud reply worker.
"""

import asyncio
import contextlib
import json
import logging
import time

import httpx

from meshcore_ai_bot import __version__
from meshcore_ai_bot.asa_fic import FicState, matches_location
from meshcore_ai_bot.asa_signal import forward_signal
from meshcore_ai_bot.assistant import Assistant
from meshcore_ai_bot.config import load_config
from meshcore_ai_bot.dab_control import effective_channel, load_settings
from meshcore_ai_bot.engine import Engine
from meshcore_ai_bot.protocol import Incoming, allows_send
from meshcore_ai_bot.settings import digest, refresh_secrets
from meshcore_ai_bot.snapshot import publish
from meshcore_ai_bot.store import Store

log = logging.getLogger(__name__)


class GatewayBridge:
    def __init__(self, path, mc, indices):
        self.path, self.mc, self.indices = path, mc, indices
        self.config = load_config(path)
        refresh_secrets(self.config, path)
        self.revision = digest(path)
        self.store = Store(self.config.storage.path)
        self.store.recover()
        self.client = httpx.AsyncClient(timeout=15, follow_redirects=False)
        self.engine = Engine(self.config, self.store, Assistant(self.config, self.store, self.client))
        own = mc.self_info.get("name") or mc.self_info.get("node_name")
        if own:
            self.engine.own_names.add(str(own).casefold())
        self.needs_reconnect = False
        self.raw_asa = FicState()
        self.asa_channel = None
        self.asa_ignore_until = 0
        self.tasks = []

    def start(self):
        self.tasks = [
            asyncio.create_task(work())
            for work in (self.process, self.transmit, self.maintain, self.asa_watch, self.asa_stream)
        ]
        log.info("Local v0.4 engine active; existing gateway owns USB")

    def receive(self, channel, text, stamp):
        return self.engine.receive(Incoming.from_radio(channel, text, stamp))

    def update_contacts(self, payload):
        self.store.update_repeaters(list(payload.values()), time.time())

    def check(self):
        for task in self.tasks:
            if task.done():
                task.result()
                raise RuntimeError("Local bot worker stopped")
        if self.needs_reconnect:
            raise ConnectionError("Uncertain radio acknowledgement; reconnect required")

    async def send(self, channel, text):
        index = next((i for i, name in self.indices.items() if name == channel), None)
        if self.needs_reconnect or index is None or len(text.encode()) > self.config.radio.packet_bytes:
            raise ConnectionError("Radio session unavailable or invalid packet")
        try:
            response = await self.mc.commands.send_chan_msg(index, text)
        except BaseException:
            self.needs_reconnect = True
            raise
        kind = response.type.name
        if kind == "OK":
            return True
        if kind == "ERROR" and type(response.payload.get("error_code")) is int:
            return False
        self.needs_reconnect = True
        raise ConnectionError("Missing radio acknowledgement")

    async def process(self):
        while True:
            if not await self.engine.process_one():
                await asyncio.sleep(0.2)

    async def transmit(self):
        while True:
            if not await self.engine.deliver_one(self):
                await asyncio.sleep(0.5)
            if self.needs_reconnect:
                return

    async def maintain(self):
        while True:
            self.store.meta_set("bot:connected", 1)
            self.store.meta_set("bot:heartbeat", time.time())
            self.store.meta_set("bot:adapter", "existing-pi-gateway")
            self.store.meta_set("bot:version", __version__)
            self.store.meta_set("bot:receive_channels", json.dumps(list(self.indices.values())))
            try:
                if digest(self.path) != self.revision:
                    new = load_config(self.path)
                    if new.storage.path != self.config.storage.path:
                        raise ValueError("Database path requires gateway restart")
                    refresh_secrets(new, self.path)
                    self.config.__dict__.update(new.__dict__)
                    self.store.limits = self.config.radio
                    self.store.enforce_limits(time.time())
                    self.revision = digest(self.path)
                    log.info("Local bot configuration reloaded")
            except (ValueError, OSError):
                log.warning("Invalid new settings; keeping working local settings")
            self.store.prune(time.time(), self.config.storage.retention_hours)
            publish(self.store)
            await asyncio.sleep(10)

    async def asa_watch(self):
        while True:
            try:
                settings = load_settings()
                response = await self.client.get("http://127.0.0.1:7979/channel", timeout=3)
                response.raise_for_status()
                channel = response.text.strip()
                if channel != self.asa_channel:
                    self.asa_channel = channel
                    self.raw_asa = FicState()
                    self.asa_ignore_until = time.time() + 8
                response = await self.client.get("http://127.0.0.1:7979/mux.json", timeout=3)
                response.raise_for_status()
                mux = response.json()
                verify = await self.client.get("http://127.0.0.1:7979/channel", timeout=3)
                verify.raise_for_status()
                if channel != verify.text.strip() or channel != effective_channel(settings):
                    status = "retuning"
                else:
                    now = time.time()
                    raw = self.raw_asa.snapshot(now)
                    active = [s for s in raw["signals"] if s["active"]]
                    relevant = [
                        s
                        for s in active
                        if matches_location(
                            s["locations"],
                            self.config.asa.location_latitude,
                            self.config.asa.location_longitude,
                        )
                    ]
                    raw["outside_area"] = bool(active) and not relevant
                    raw["active"] = bool(relevant)
                    raw["is_test"] = bool(relevant) and all(s["is_test"] for s in relevant)
                    self.store.meta_set("asa:validated", json.dumps({**raw, "channel": channel}))
                    status = "awaiting_validated_signal"
                    if raw["validated"]:
                        status = "idle" if settings["forward_asa"] else "disabled"
                        if settings["forward_asa"] and (
                            not self.config.asa.enabled
                            or not allows_send(self.config, "#warnings", "warnings")
                        ):
                            status = "blocked"
                        if raw["heartbeat"]:
                            self.clear_asa_heartbeat(channel)
                        for event in raw["signals"]:
                            checked = {**mux, "asa": event}
                            result = forward_signal(self.config, self.store, checked, channel, settings, now)
                            if result not in {"idle", "duplicate"}:
                                status = "test_suppressed" if result == "test_or_unconfirmed" else result
                self.store.meta_set("asa:forward_status", status)
                self.store.meta_set("asa:watch_heartbeat", time.time())
            except (OSError, ValueError, TypeError, KeyError, httpx.HTTPError):
                self.store.meta_set("asa:forward_status", "receiver_unavailable")
                self.store.meta_set("asa:watch_heartbeat", time.time())
            await asyncio.sleep(2)

    def clear_asa_heartbeat(self, channel):
        with self.store.transaction() as db:
            for key, value in db.execute(
                "SELECT key,value FROM meta WHERE key LIKE 'asa:signal:%'"
            ).fetchall():
                record = json.loads(value)
                if record.get("channel") == channel and record.get("active"):
                    record["active"] = False
                    db.execute("UPDATE meta SET value=? WHERE key=?", (json.dumps(record), key))

    async def asa_stream(self):
        while True:
            self.raw_asa = FicState()
            self.asa_ignore_until = time.time() + 8
            try:
                async with self.client.stream(
                    "GET", "http://127.0.0.1:7979/fic", timeout=httpx.Timeout(5, connect=3)
                ) as response:
                    response.raise_for_status()
                    pending = b""
                    async for chunk in response.aiter_bytes():
                        pending += chunk
                        while len(pending) >= 32:
                            fib, pending = pending[:32], pending[32:]
                            if self.asa_channel and time.time() >= self.asa_ignore_until:
                                self.raw_asa.feed(fib, time.time())
            except (OSError, httpx.HTTPError):
                pass
            await asyncio.sleep(1)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.store.meta_set("bot:connected", 0)
        self.store.meta_set("bot:heartbeat", time.time())
        self.store.close()
        await self.client.aclose()
