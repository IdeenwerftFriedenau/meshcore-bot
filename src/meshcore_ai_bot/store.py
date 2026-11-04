"""Small durable SQLite inbox, outbox, history, rate limits and repeater catalogue."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .config import Radio
from .memory import safe_memory
from .protocol import Incoming


class Store:
    def __init__(self, path: str | Path):
        path = Path(path)
        self.path = path
        self.limits = Radio()
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            raise ValueError("Unsupported database version; restore a compatible application")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS messages(
            id TEXT PRIMARY KEY,channel TEXT NOT NULL,sender TEXT NOT NULL,raw_text TEXT NOT NULL,question TEXT NOT NULL,
            stamp INTEGER NOT NULL,received REAL NOT NULL,status TEXT NOT NULL,answer TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS packets(
            id TEXT PRIMARY KEY,batch TEXT NOT NULL,message_id TEXT REFERENCES messages(id) ON DELETE CASCADE,
            channel TEXT NOT NULL,text TEXT NOT NULL,stream TEXT NOT NULL,state TEXT NOT NULL,
            expires REAL NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,ordinal INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS packet_queue ON packets(state,created,ordinal);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS model_requests(stamp REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS repeaters(
            id TEXT PRIMARY KEY,name TEXT NOT NULL,aliases TEXT NOT NULL,advert REAL,checked REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS broadcasts(
            identity TEXT PRIMARY KEY,revision TEXT NOT NULL,valid_until REAL NOT NULL,checked REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS memory(
            id TEXT PRIMARY KEY,channel TEXT NOT NULL,sender TEXT NOT NULL,received REAL NOT NULL,
            text TEXT NOT NULL,answer TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS memory_sender ON memory(sender,received);
        CREATE TABLE IF NOT EXISTS incoming_requests(sender TEXT NOT NULL,stamp REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS incoming_stamp ON incoming_requests(stamp);
        PRAGMA user_version=2;
        """)
        if "priority" not in {row[1] for row in self.db.execute("PRAGMA table_info(packets)")}:
            self.db.execute("ALTER TABLE packets ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
        if version < 2:
            self.db.execute(
                "INSERT OR IGNORE INTO memory SELECT id,channel,lower(sender),received,question,answer FROM messages"
            )
        self.db.commit()
        if str(path) != ":memory:":
            os.chmod(path, 0o600)

    @contextlib.contextmanager
    def transaction(self):
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            yield self.db

    def close(self):
        if getattr(self, "_closed", False):
            return
        try:
            from .snapshot import publish

            publish(self)
        finally:
            self.db.close()
            self._closed = True

    def enforce_limits(self, now):
        """Apply lowered/new limits to pre-upgrade queues without splitting batches."""
        with self.transaction() as c:
            c.execute(
                "UPDATE packets SET state='expired',updated=? WHERE state='queued' AND expires<=?", (now, now)
            )
            used = c.execute("SELECT count(*) FROM packets WHERE state='sending'").fetchone()[0]
            rows = c.execute(
                "SELECT batch,channel,stream,count(*) AS size FROM packets WHERE state='queued' "
                "GROUP BY batch,channel,stream ORDER BY max(priority) DESC,"
                "CASE stream WHEN 'warnings' THEN 0 WHEN 'reply' THEN 1 ELSE 2 END,min(created),batch,channel"
            ).fetchall()
            for row in rows:
                maximum = (
                    self.limits.reply_max_packets
                    if row["stream"] == "reply"
                    else self.limits.broadcast_max_packets
                )
                if row["size"] <= maximum and used + row["size"] <= self.limits.max_pending_packets:
                    used += row["size"]
                    continue
                c.execute(
                    "UPDATE packets SET state='cancelled',updated=? "
                    "WHERE batch=? AND channel=? AND state='queued'",
                    (now, row["batch"], row["channel"]),
                )
                c.execute(
                    "INSERT INTO meta VALUES('queue:dropped','1') ON CONFLICT(key) "
                    "DO UPDATE SET value=CAST(value AS INTEGER)+1"
                )

    def observe(self, msg: Incoming, now: float):
        with self.transaction() as c:
            # Bound passive memory even when a sender floods an unaddressed channel.
            count = c.execute(
                "SELECT count(*) FROM memory WHERE sender=? AND received>?",
                (msg.sender.casefold(), now - 3600),
            ).fetchone()[0]
            total = c.execute("SELECT count(*) FROM memory WHERE received>?", (now - 3600,)).fetchone()[0]
            if count < 120 and total < 2000:
                c.execute(
                    "INSERT OR IGNORE INTO memory(id,channel,sender,received,text) VALUES(?,?,?,?,?)",
                    (msg.identity, msg.channel, msg.sender.casefold(), now, msg.text),
                )

    def admit(self, msg: Incoming, question: str, now: float) -> str:
        with self.transaction() as c:
            if c.execute("SELECT 1 FROM messages WHERE id=?", (msg.identity,)).fetchone():
                return "duplicate"
            c.execute("DELETE FROM incoming_requests WHERE stamp<=?", (now - 60,))
            total = c.execute("SELECT count(*) FROM incoming_requests").fetchone()[0]
            personal = c.execute(
                "SELECT count(*) FROM incoming_requests WHERE sender=?", (msg.sender.casefold(),)
            ).fetchone()[0]
            pending = c.execute(
                "SELECT count(*) FROM messages WHERE status IN ('pending','processing')"
            ).fetchone()[0]
            if (
                total >= self.limits.incoming_per_minute
                or personal >= self.limits.incoming_per_sender_per_minute
                or pending >= self.limits.max_pending_messages
            ):
                return "limited"
            c.execute("INSERT INTO incoming_requests VALUES(?,?)", (msg.sender.casefold(), now))
            c.execute(
                "INSERT INTO messages(id,channel,sender,raw_text,question,stamp,received,status) "
                "VALUES(?,?,?,?,?,?,?,'pending')",
                (msg.identity, msg.channel, msg.sender, msg.text, question, msg.timestamp, now),
            )
            return "queued"

    def remember(self, msg: Incoming, question: str, now: float) -> bool:
        self.observe(msg, now)
        with self.transaction() as c:
            return bool(
                c.execute(
                    "INSERT OR IGNORE INTO messages(id,channel,sender,raw_text,question,stamp,received,status) "
                    "VALUES(?,?,?,?,?,?,?,'pending')",
                    (msg.identity, msg.channel, msg.sender, msg.text, question, msg.timestamp, now),
                ).rowcount
            )

    def message(self, identity: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM messages WHERE id=?", (identity,)).fetchone()
        return dict(row) if row else None

    def cancel_message(self, identity: str):
        with self.transaction() as c:
            c.execute("UPDATE messages SET status='cancelled' WHERE id=?", (identity,))

    def next_message(self, now: float, max_age: int) -> dict | None:
        with self.transaction() as c:
            c.execute(
                "UPDATE messages SET status='expired' WHERE status='pending' AND stamp<?", (now - max_age,)
            )
            row = c.execute(
                "SELECT * FROM messages WHERE status='pending' ORDER BY received,id LIMIT 1"
            ).fetchone()
            if row:
                c.execute("UPDATE messages SET status='processing' WHERE id=?", (row["id"],))
            return dict(row) if row else None

    def recover(self, now: float | None = None):
        # Model/tool reads may be repeated; an uncertain radio transmission must not be.
        with self.transaction() as c:
            c.execute("UPDATE messages SET status='pending' WHERE status='processing'")
            changed = c.execute("UPDATE packets SET state='uncertain' WHERE state='sending'").rowcount
            if changed:
                # The process may have died after handing bytes to USB. Start a fresh
                # quiet interval instead of sending the next fragment immediately.
                recovered_at = time.time() if now is None else now
                c.execute(
                    "INSERT INTO meta VALUES('last_tx',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(recovered_at),),
                )

    def history(self, channel: str, sender: str, since: float, turns: int, exclude: str = "") -> list[dict]:
        if not turns:
            return []
        with self.lock:
            rows = self.db.execute(
                "SELECT text,answer FROM memory WHERE sender=? AND received>=? AND id!=? "
                "ORDER BY received DESC,rowid DESC LIMIT ?",
                (sender.casefold(), since, exclude, turns),
            ).fetchall()
        return [
            item
            for row in reversed(rows)
            for item in [
                {"role": "user", "content": row["text"]},
                *(
                    [{"role": "assistant", "content": row["answer"]}]
                    if row["answer"] and not re.search(r"[!?:;]{4,}", row["answer"])
                    else []
                ),
            ]
            if safe_memory(row["text"])
        ]

    def answer(
        self, message_id: str, channel: str, answer: str, parts: list[str], now: float, expires: float
    ):
        with self.transaction() as c:
            queued = self._insert_parts(c, message_id, message_id, channel, parts, "reply", now, expires)
            c.execute(
                "UPDATE messages SET status=?,answer=? WHERE id=?",
                ("answered" if queued else "limited", answer, message_id),
            )
            c.execute("UPDATE memory SET answer=? WHERE id=?", (answer if queued else "", message_id))

    def _insert_parts(self, c, batch, message_id, channel, parts, stream, now, expires, priority=0):
        c.execute(
            "UPDATE packets SET state='expired',updated=? WHERE state='queued' AND expires<=?", (now, now)
        )
        pending = c.execute("SELECT count(*) FROM packets WHERE state IN ('queued','sending')").fetchone()[0]
        limit = self.limits.reply_max_packets if stream == "reply" else self.limits.broadcast_max_packets
        if len(parts) > limit or pending + len(parts) > self.limits.max_pending_packets:
            c.execute(
                "INSERT INTO meta VALUES('queue:dropped','1') ON CONFLICT(key) "
                "DO UPDATE SET value=CAST(value AS INTEGER)+1"
            )
            return False
        for index, part in enumerate(parts):
            key = hashlib.sha256(f"{batch}\0{channel}\0{index}".encode()).hexdigest()
            c.execute(
                "INSERT OR IGNORE INTO packets(id,batch,message_id,channel,text,stream,state,expires,created,updated,ordinal,priority) "
                "VALUES(?,?,?,?,?,?,'queued',?,?,?,?,?)",
                (key, batch, message_id, channel, part, stream, expires, now, now, index, priority),
            )
        return True

    def claim(self, now: float, allowed) -> dict | None:
        with self.transaction() as c:
            c.execute(
                "UPDATE packets SET state='expired',updated=? WHERE state='queued' AND expires<=?", (now, now)
            )
            if c.execute("SELECT 1 FROM packets WHERE state='sending' LIMIT 1").fetchone():
                return None
            rows = c.execute(
                "SELECT * FROM packets WHERE state='queued' ORDER BY priority DESC,created,ordinal,id"
            ).fetchall()
            # Persistent round robin between streams; severity orders warnings within their turn.
            last = c.execute("SELECT value FROM meta WHERE key='scheduler:stream'").fetchone()
            streams = ["warnings", "reply", "news"]
            if last and last[0] in streams:
                pivot = streams.index(last[0]) + 1
                streams = streams[pivot:] + streams[:pivot]
            rows = sorted(
                rows, key=lambda row: streams.index(row["stream"]) if row["stream"] in streams else 3
            )
            for row in rows:
                if not allowed(row["channel"], row["stream"]):
                    c.execute("UPDATE packets SET state='cancelled',updated=? WHERE id=?", (now, row["id"]))
                    continue
                c.execute("UPDATE packets SET state='sending',updated=? WHERE id=?", (now, row["id"]))
                c.execute(
                    "INSERT INTO meta VALUES('scheduler:stream',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (row["stream"],),
                )
                return dict(row)
        return None

    def still_sendable(self, packet_id: str, now: float) -> bool:
        with self.lock:
            row = self.db.execute("SELECT state,expires FROM packets WHERE id=?", (packet_id,)).fetchone()
        return bool(row and row["state"] == "sending" and row["expires"] > now)

    def finish(self, packet_id: str, state: str, now: float):
        if state not in {"accepted", "failed", "uncertain", "cancelled", "expired"}:
            raise ValueError("Invalid final packet state")
        with self.transaction() as c:
            c.execute(
                "UPDATE packets SET state=?,updated=? WHERE id=? AND state='sending'", (state, now, packet_id)
            )
            if state in {"accepted", "failed", "uncertain"}:
                c.execute(
                    "INSERT INTO meta VALUES('last_tx',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(now),),
                )

    def last_tx(self) -> float | None:
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key='last_tx'").fetchone()
        return float(row[0]) if row else None

    def reserve_model_request(self, now: float, per_minute: int, per_day: int) -> bool:
        day = int(now // 86400) * 86400  # UTC accounting, independent of daylight-saving changes
        with self.transaction() as c:
            minute = c.execute("SELECT count(*) FROM model_requests WHERE stamp>?", (now - 60,)).fetchone()[0]
            daily = c.execute("SELECT count(*) FROM model_requests WHERE stamp>=?", (day,)).fetchone()[0]
            if minute >= per_minute or daily >= per_day:
                return False
            c.execute("INSERT INTO model_requests VALUES(?)", (now,))
            return True

    def update_repeaters(self, contacts: list[dict], now: float):
        with self.transaction() as c:
            for contact in contacts:
                key = str(contact.get("public_key", "")).lower()
                if contact.get("type") != 2 or len(key) != 64:
                    continue
                try:
                    bytes.fromhex(key)
                except ValueError:
                    continue
                name = " ".join(str(contact.get("adv_name", "Unnamed")).split())[:160]
                old = c.execute("SELECT * FROM repeaters WHERE id=?", (key,)).fetchone()
                aliases = set(json.loads(old["aliases"])) if old else set()
                if old:
                    aliases.add(old["name"])
                aliases.add(name)
                try:
                    advert = float(contact.get("last_advert", 0))
                except (ValueError, TypeError):
                    advert = 0
                advert = advert if 0 < advert <= now + 120 else None
                if old and old["advert"]:
                    advert = max(old["advert"], advert or 0)
                c.execute(
                    "INSERT INTO repeaters VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "name=excluded.name,aliases=excluded.aliases,advert=excluded.advert,checked=excluded.checked",
                    (key, name, json.dumps(sorted(aliases), ensure_ascii=False), advert, now),
                )

    def heard_advert(self, key: str, now: float):
        with self.transaction() as c:
            c.execute("UPDATE repeaters SET advert=?,checked=? WHERE id=?", (now, now, key.lower()))

    def repeaters(self) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.db.execute("SELECT * FROM repeaters ORDER BY name,id")]

    def broadcast(
        self,
        identity: str,
        revision: str,
        valid_until: float,
        batches: dict[str, list[str]],
        stream: str,
        now: float,
        baseline: bool = False,
        priority: int = 0,
    ) -> bool:
        with self.transaction() as c:
            old = c.execute("SELECT revision FROM broadcasts WHERE identity=?", (identity,)).fetchone()
            if old and old[0] == revision:
                return False
            # Superseded/withdrawn alert fragments must not continue after a new revision.
            old_batch = f"{identity}:{old[0]}" if old else None
            if old_batch:
                c.execute(
                    "UPDATE packets SET expires=MIN(expires,?) WHERE batch=? AND state IN ('queued','sending')",
                    (now, old_batch),
                )
                c.execute(
                    "UPDATE packets SET state='cancelled',updated=? WHERE batch=? AND state='queued'",
                    (now, old_batch),
                )
            if not baseline and valid_until > now:
                pending = c.execute(
                    "SELECT count(*) FROM packets WHERE state IN ('queued','sending')"
                ).fetchone()[0]
                if (
                    any(len(parts) > self.limits.broadcast_max_packets for parts in batches.values())
                    or pending + sum(map(len, batches.values())) > self.limits.max_pending_packets
                ):
                    c.execute(
                        "INSERT INTO meta VALUES('queue:dropped','1') ON CONFLICT(key) "
                        "DO UPDATE SET value=CAST(value AS INTEGER)+1"
                    )
                    return False
            c.execute(
                "INSERT INTO broadcasts VALUES(?,?,?,?) ON CONFLICT(identity) DO UPDATE SET "
                "revision=excluded.revision,valid_until=excluded.valid_until,checked=excluded.checked",
                (identity, revision, valid_until, now),
            )
            if baseline or valid_until <= now:
                return False
            for channel, parts in batches.items():
                self._insert_parts(
                    c, f"{identity}:{revision}", None, channel, parts, stream, now, valid_until, priority
                )
            return True

    def counts(self) -> dict[str, int]:
        with self.lock:
            return {
                row[0]: row[1] for row in self.db.execute("SELECT state,count(*) FROM packets GROUP BY state")
            }

    def meta_get(self, key: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def meta_set(self, key: str, value: Any):
        with self.transaction() as c:
            c.execute(
                "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def prune(self, now: float, retention_hours: int):
        cutoff = now - retention_hours * 3600
        with self.transaction() as c:
            c.execute("UPDATE packets SET state='expired',updated=? WHERE state='queued' AND expires<=?", (now, now))
            c.execute("DELETE FROM messages WHERE received<?", (cutoff,))
            c.execute("DELETE FROM packets WHERE updated<? AND state NOT IN ('queued','sending')", (cutoff,))
            c.execute("DELETE FROM model_requests WHERE stamp<?", (now - 172800,))
            c.execute("DELETE FROM memory WHERE received<?", (cutoff,))
            c.execute("DELETE FROM incoming_requests WHERE stamp<?", (now - 60,))
            c.execute("DELETE FROM broadcasts WHERE valid_until<? AND checked<?", (cutoff, cutoff))
