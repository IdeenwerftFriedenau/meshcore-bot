"""Atomically replaced dashboard data; no chat text or writable SQLite sidecars."""

import json
import os
import tempfile
from pathlib import Path


def snapshot_path(path):
    return Path(str(path) + ".dashboard.json")


def publish(store):
    if str(store.path) == ":memory:":
        return
    with store.transaction() as db:
        data = {
            "meta": dict(db.execute("SELECT key,value FROM meta")),
            "packets": dict(db.execute("SELECT state,count(*) FROM packets GROUP BY state")),
            "repeaters": [dict(row) for row in db.execute("SELECT * FROM repeaters ORDER BY id")],
        }
    target = snapshot_path(store.path)
    fd, temp = tempfile.mkstemp(prefix=".dashboard-", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def read(path):
    target = snapshot_path(path)
    with target.open("rb") as stream:
        raw = stream.read(4_000_001)
    if len(raw) > 4_000_000:
        raise ValueError("Dashboard snapshot too large")
    return json.loads(raw)
