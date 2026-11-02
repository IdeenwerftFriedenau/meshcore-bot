"""MeshCore 2.3.15 adapter. Dependency imported only for an explicit live command."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import os
import time
from pathlib import Path

import httpx

from .assistant import Assistant
from .broadcasts import Broadcasts
from .config import Config
from .engine import Engine
from .protocol import Incoming
from .store import Store

log = logging.getLogger(__name__)
PUBLIC_KEY = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")  # public standard channel, NOT a secret


def channel_key(channel) -> bytes:
    if channel.secret_env:
        try:
            result = bytes.fromhex(os.environ.get(channel.secret_env, ""))
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
        response = await self.mc.commands.send_chan_msg(index, text)
        return response.type == self.EventType.OK  # local acceptance, not recipient delivery

    async def disconnect(self):
        if self.mc:
            with contextlib.suppress(Exception):
                await self.mc.stop_auto_message_fetching()
            with contextlib.suppress(Exception):
                await self.mc.disconnect()
            self.mc = None


async def run_radio(config: Config):
    with instance_lock(config.storage.path):
        store = Store(config.storage.path)
        try:
            async with httpx.AsyncClient(
                timeout=15, follow_redirects=False, headers={"User-Agent": "MeshCore-AI-Bot/0.1"}
            ) as client:
                assistant = Assistant(config, store, client)
                broadcasts = Broadcasts(config, store, client)
                while True:
                    store.recover()
                    engine = Engine(config, store, assistant)
                    transport = RadioTransport(config, store, engine)
                    try:
                        await transport.connect()
                        log.info("Connected. Listening channels: %s", ", ".join(transport.indices.values()))

                        async def process(engine=engine):
                            while True:
                                if not await engine.process_one():
                                    await asyncio.sleep(0.2)

                        async def transmit(engine=engine, transport=transport):
                            while True:
                                if not await engine.deliver_one(transport):
                                    await asyncio.sleep(0.5)

                        async def maintain(transport=transport):
                            while True:
                                await transport.refresh_contacts()
                                store.prune(time.time(), config.storage.retention_hours)
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

                        async with asyncio.TaskGroup() as group:
                            for worker in [process, transmit, maintain, warning_poll, news_poll]:
                                group.create_task(worker())
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("Connection/worker stopped; reconnecting in 10 seconds")
                    finally:
                        await transport.disconnect()
                    await asyncio.sleep(10)
        finally:
            store.close()
