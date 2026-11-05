"""MeshCore 2.3.15 adapter. Dependency imported only for an explicit live command."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import os
import sqlite3
import time
from pathlib import Path

import httpx

from . import asa
from .assistant import Assistant
from .broadcasts import Broadcasts
from .config import Config, load_config
from .engine import Engine
from .protocol import Incoming
from .settings import digest, refresh_secrets
from .snapshot import publish
from .store import Store

log = logging.getLogger(__name__)
PUBLIC_KEY = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")  # public standard channel, NOT a secret


def channel_key(channel) -> bytes:
    if channel.secret_env:
        try:
            result = bytes.fromhex(getattr(channel, "_secret", os.environ.get(channel.secret_env, "")))
        except ValueError as exc:
            raise ValueError(f"Invalid channel key in environment variable {channel.secret_env}") from exc
        if len(result) != 16:
            raise ValueError(f"{channel.secret_env} must contain a 16-byte hexadecimal channel key")
        return result
    if channel.name == "Public":
        return PUBLIC_KEY
    if not channel.name.startswith("#"):
        raise ValueError(f"A private channel {channel.name} needs secret_env")
    return hashlib.sha256(channel.name.encode()).digest()[:16]


@contextlib.contextmanager
def instance_lock(path: str):
    # The deployment target is Raspberry Pi OS/Linux; macOS development works too.
    import fcntl

    target = Path(path).with_suffix(".lock")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with target.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another bot instance already uses this state directory") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class RadioTransport:
    def __init__(self, config: Config, store: Store, engine: Engine | None = None):
        self.config, self.store, self.engine = config, store, engine
        self.mc = None
        self.indices: dict[int, str] = {}
        self.EventType = None
        self.needs_reconnect = False

    async def connect(self, *, provision: bool = False):
        try:
            from meshcore import EventType, MeshCore
        except ImportError as exc:
            raise RuntimeError("Install the radio extra: pip install 'meshcore-ai-bot[radio]'") from exc
        self.EventType = EventType
        self.mc = await MeshCore.create_serial(
            self.config.radio.port, default_timeout=5, auto_reconnect=False
        )
        if self.mc is None:
            raise ConnectionError("No MeshCore USB companion response")
        command_lock = asyncio.Lock()
        original = self.mc.commands.send

        async def serialized(*args, **kwargs):
            async with command_lock:
                return await original(*args, **kwargs)

        self.mc.commands.send = serialized
        self.indices = await self.load_channels(provision or self.config.radio.create_channels)
        if self.engine:
            actual_name = self.mc.self_info.get("name") or self.mc.self_info.get("node_name")
            if actual_name:
                self.engine.own_names.add(str(actual_name).casefold())

            async def incoming(event):
                payload = event.payload
                channel = self.indices.get(payload.get("channel_idx"))
                text, stamp = payload.get("text"), payload.get("sender_timestamp")
                if channel and isinstance(text, str) and type(stamp) is int:
                    result = self.engine.receive(Incoming.from_radio(channel, text, stamp))
                    log.info("Incoming channel=%s result=%s", channel, result)

            async def received(_):
                self.engine.pacer.received()

            async def new_contact(event):
                self.store.update_repeaters([event.payload], time.time())

            async def advert(event):
                key = event.payload.get("public_key")
                if isinstance(key, str):
                    self.store.heard_advert(key, time.time())

            self.mc.subscribe(EventType.CHANNEL_MSG_RECV, incoming)
            self.mc.subscribe(EventType.RX_LOG_DATA, received)
            self.mc.subscribe(EventType.NEW_CONTACT, new_contact)
            self.mc.subscribe(EventType.ADVERTISEMENT, advert)
            await self.mc.start_auto_message_fetching()
        await self.refresh_contacts()
        self.needs_reconnect = False

    async def load_channels(self, create: bool) -> dict[int, str]:
        indices, empty = {}, []
        wanted = {c.name: c for c in self.config.channels}
        for index in range(16):
            result = await self.mc.commands.get_channel(index)
            if result.type != self.EventType.CHANNEL_INFO:
                continue
            name = result.payload.get("channel_name", "")
            if not name:
                empty.append(index)
                continue
            if name in wanted:
                channel = wanted[name]
                if channel.mode != "off" or channel.broadcasts:
                    if not hmac.compare_digest(
                        result.payload.get("channel_secret", b""), channel_key(channel)
                    ):
                        raise RuntimeError(f"Channel key mismatch in existing slot {index}; not overwritten")
                indices[index] = name
        for channel in self.config.channels:
            if channel.name in indices.values() or (channel.mode == "off" and not channel.broadcasts):
                continue
            if not create:
                raise RuntimeError(
                    f"Missing channel {channel.name}; run provision or enable radio.create_channels"
                )
            if not empty:
                raise RuntimeError("No empty MeshCore channel slot; existing channels are never overwritten")
            index = empty.pop(0)
            result = await self.mc.commands.set_channel(index, channel.name, channel_key(channel))
            if result.type != self.EventType.OK:
                raise RuntimeError(f"Channel creation rejected: {channel.name}")
            verified = await self.mc.commands.get_channel(index)
            if (
                verified.type != self.EventType.CHANNEL_INFO
                or verified.payload.get("channel_name") != channel.name
                or not hmac.compare_digest(verified.payload.get("channel_secret", b""), channel_key(channel))
            ):
                raise RuntimeError("New channel verification failed")
            indices[index] = channel.name
        return indices

    async def refresh_contacts(self):
        result = await self.mc.commands.get_contacts()
        if result.type != self.EventType.CONTACTS:
            raise ConnectionError("Could not read contacts")
        self.store.update_repeaters(list(result.payload.values()), time.time())

    async def send(self, channel: str, text: str) -> bool:
        index = next((i for i, name in self.indices.items() if name == channel), None)
        if index is None or len(text.encode()) > self.config.radio.packet_bytes:
            raise ValueError("Unconfigured channel or oversized packet")
        if self.needs_reconnect:
            raise ConnectionError("Radio session must reconnect before sending")
        try:
            response = await self.mc.commands.send_chan_msg(index, text)
        except BaseException:
            # Cancellation can leave a late, uncorrelated OK in the old session too.
            self.needs_reconnect = True
            raise
        if response.type == self.EventType.OK:
            return True  # local acceptance, not recipient delivery
        if response.type == self.EventType.ERROR and type(response.payload.get("error_code")) is int:
            return False  # an explicit error frame from the device
        # The SDK represents timeout/disconnect as ERROR with reason/error, rather
        # than raising. No response does not prove that the device did not send.
        self.needs_reconnect = True
        raise ConnectionError("Device acknowledgement missing; reconnect required")

    async def disconnect(self):
        if self.mc:
            with contextlib.suppress(Exception):
                await self.mc.stop_auto_message_fetching()
            with contextlib.suppress(Exception):
                await self.mc.disconnect()
            self.mc = None


class ConfigurationReloaded(Exception):
    pass


def cancel_changed_sources(store, old, new):
    from .broadcasts import selected_regions

    sources = []
    for source in ["dwd", "bbk"]:
        a, b = old.broadcasts, new.broadcasts
        if (a.warnings_enabled, getattr(a, source + "_enabled"), selected_regions(old, source)) != (
            b.warnings_enabled,
            getattr(b, source + "_enabled"),
            selected_regions(new, source),
        ):
            sources.append(source)
    if (old.broadcasts.warnings_enabled, old.asa.enabled, old.asa.regions) != (
        new.broadcasts.warnings_enabled,
        new.asa.enabled,
        new.asa.regions,
    ):
        sources.append("asa")
    if (
        old.broadcasts.news_enabled != new.broadcasts.news_enabled
        or old.broadcasts.rss_feeds != new.broadcasts.rss_feeds
    ):
        sources.append("rss")
    with store.transaction() as c:
        for source in sources:
            c.execute(
                "UPDATE packets SET state='cancelled',updated=? WHERE batch LIKE ? AND state='queued'",
                (time.time(), source + ":%"),
            )


async def housekeeping(config, store):
    """Retention and status publication continue even while USB is unavailable."""
    while True:
        try:
            store.prune(time.time(), config.storage.retention_hours)
            publish(store)
        except (OSError, sqlite3.Error):
            log.exception("State maintenance failed")
        await asyncio.sleep(30)


async def poll_asa_once(config, store):
    work = asyncio.create_task(asyncio.to_thread(asa.poll, config, store))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        # Cancelling an await does not stop the thread using the database.
        await work
        raise


async def run_radio(config: Config, config_path=None):
    with instance_lock(config.storage.path):
        store = Store(config.storage.path)
        maintenance = asyncio.create_task(housekeeping(config, store))
        try:
            async with httpx.AsyncClient(
                timeout=15, follow_redirects=False, headers={"User-Agent": "MeshCore-AI-Bot/0.4"}
            ) as client:
                assistant = Assistant(config, store, client)
                broadcasts = Broadcasts(config, store, client)
                revision = digest(config_path) if config_path else None
                while True:
                    if config_path:
                        try:
                            new = load_config(config_path)
                            if new.storage.path != config.storage.path:
                                raise ValueError("Changing the database path requires a service restart")
                            refresh_secrets(new, config_path)
                            cancel_changed_sources(store, config, new)
                            config.__dict__.update(new.__dict__)
                            revision = digest(config_path)
                        except (ValueError, OSError):
                            log.warning("New settings invalid; keeping last working configuration")
                    store.recover()
                    engine = Engine(config, store, assistant)
                    transport = RadioTransport(config, store, engine)
                    try:
                        await transport.connect()
                        store.meta_set("bot:connected", 1)
                        store.meta_set("bot:heartbeat", time.time())
                        log.info("Connected. Listening channels: %s", ", ".join(transport.indices.values()))

                        async def process(engine=engine):
                            while True:
                                if not await engine.process_one():
                                    await asyncio.sleep(0.2)

                        async def transmit(engine=engine, transport=transport):
                            while True:
                                delivered = await engine.deliver_one(transport)
                                if transport.needs_reconnect:
                                    raise ConnectionError("Radio session acknowledgement is uncertain")
                                if not delivered:
                                    await asyncio.sleep(0.5)

                        async def maintain(transport=transport):
                            while True:
                                store.meta_set("bot:heartbeat", time.time())
                                await transport.refresh_contacts()
                                await asyncio.sleep(60)

                        async def warning_poll():
                            while True:
                                result = await broadcasts.poll_warnings()
                                if result:
                                    log.info("Warning source results: %s", result)
                                await asyncio.sleep(config.broadcasts.warning_poll_seconds)

                        async def news_poll():
                            while True:
                                await broadcasts.poll_news()
                                await asyncio.sleep(config.broadcasts.news_poll_seconds)

                        async def asa_poll():
                            while True:
                                try:
                                    await poll_asa_once(config, store)
                                except Exception:
                                    log.exception("ASA inbox unavailable; keeping radio session")
                                await asyncio.sleep(2)

                        async def watch_settings(revision=revision):
                            while True:
                                await asyncio.sleep(2)
                                try:
                                    changed = digest(config_path)
                                    if changed != revision:
                                        new = load_config(config_path)
                                        if new.storage.path != config.storage.path:
                                            raise ValueError("Database path requires restart")
                                        raise ConfigurationReloaded()
                                except (OSError, ValueError):
                                    log.warning(
                                        "Invalid updated configuration; keeping last working settings"
                                    )

                        async with asyncio.TaskGroup() as group:
                            for worker in [process, transmit, maintain, warning_poll, news_poll]:
                                group.create_task(worker())
                            if config.asa.enabled:
                                group.create_task(asa_poll())
                            if config_path:
                                group.create_task(watch_settings())
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("Connection/worker stopped; reconnecting in 10 seconds")
                    finally:
                        store.meta_set("bot:connected", 0)
                        await transport.disconnect()
                    await asyncio.sleep(10)
        finally:
            maintenance.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await maintenance
            store.close()
