"""Run the real answer pipeline in disposable memory; no transport exists here."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

import httpx
from dotenv import dotenv_values

from .assistant import Assistant
from .config import AI, load_config
from .engine import Engine
from .protocol import Incoming
from .store import Store


class PreviewAI(AI):
    @property
    def api_key(self):
        return self._preview_key


def load_preview_config(path):
    """Keep provider and credential together throughout a test, without global env writes."""
    config = load_config(path)
    value = dotenv_values(Path(path).parent / ".env").get(config.ai.api_key_env)
    key = config.ai.api_key if value is None else value
    config.ai = PreviewAI(**asdict(config.ai))
    config.ai._preview_key = key
    return config


def validate_request(payload, config):
    if not isinstance(payload, dict) or set(payload) - {"text", "channel", "sender", "offline"}:
        raise ValueError("Unknown preview fields")
    text, channel = payload.get("text"), payload.get("channel")
    sender, offline = payload.get("sender", "Testfunker"), payload.get("offline", True)
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > 600:
        raise ValueError("Text must contain 1–600 UTF-8 bytes")
    if not isinstance(channel, str) or config.channel(channel) is None:
        raise ValueError("Unknown preview channel")
    if (
        not isinstance(sender, str)
        or not sender.strip()
        or len(sender.encode()) > 32
        or any(ord(c) < 32 for c in sender)
    ):
        raise ValueError("Invalid sender")
    if type(offline) is not bool:
        raise ValueError("Offline must be boolean")
    return text, channel, sender, offline


def load_contacts(config, store):
    path = Path(config.storage.path)
    if not path.is_file():
        return False
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as source:
            rows = source.execute("SELECT id,name,aliases,advert,checked FROM repeaters").fetchall()
        with store.transaction() as db:
            db.executemany("INSERT INTO repeaters VALUES(?,?,?,?,?)", rows)
        return True
    except (sqlite3.Error, OSError):
        return False


async def preview(config, payload):
    text, channel, sender, offline = validate_request(payload, config)
    store = Store(":memory:")
    contacts_loaded = False
    try:
        contacts_loaded = await asyncio.to_thread(load_contacts, config, store)
        async with httpx.AsyncClient(timeout=config.ai.timeout_seconds, follow_redirects=False) as client:
            assistant = Assistant(config, store, client, offline=offline)
            engine = Engine(config, store, assistant)
            decision = engine.receive(Incoming(channel, sender, text, int(time.time())))
            if decision == "queued":
                await engine.process_one()
            row = store.db.execute("SELECT answer FROM messages ORDER BY received DESC LIMIT 1").fetchone()
            packets = [r[0] for r in store.db.execute("SELECT text FROM packets ORDER BY ordinal")]
            return {
                "input": text,
                "channel": channel,
                "decision": decision,
                "answer": row[0] if row else "",
                "packets": packets,
                "packet_bytes": [len(p.encode()) for p in packets],
                "radio_sent": False,
                "offline": offline,
                "contacts_loaded": contacts_loaded,
                "minimum_spacing_seconds": config.radio.send_interval_seconds,
            }
    finally:
        store.close()


def run_preview(config, payload):
    async def bounded():
        return await asyncio.wait_for(preview(config, payload), timeout=60)

    return asyncio.run(bounded())
