"""Durable read-only app mirror. No AI generation and no radio methods."""

import json
import sqlite3
import time
import urllib.request
from pathlib import Path


class ChannelMirror:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS mirrors(id TEXT PRIMARY KEY,channel TEXT,stamp INTEGER,text TEXT,created REAL,status TEXT,attempts INTEGER,next_try REAL)"
            )

    def db(self):
        return sqlite3.connect(self.path, timeout=3)

    def record(self, key, channel, stamp, text):
        now = time.time()
        with self.db() as c:
            c.execute("DELETE FROM mirrors WHERE created<?", (now - 86400,))
            c.execute(
                "INSERT OR IGNORE INTO mirrors VALUES(?,?,?,?,?,'pending',0,0)",
                (key, channel, stamp, text, now),
            )

    def next(self):
        now = time.time()
        with self.db() as c:
            row = c.execute(
                "SELECT id,channel,stamp,text,created,attempts FROM mirrors WHERE status='pending' AND next_try<=? AND created>=? ORDER BY created LIMIT 1",
                (now, now - 86400),
            ).fetchone()
        return row

    def finish(self, key, success):
        with self.db() as c:
            if success:
                c.execute("UPDATE mirrors SET status='mirrored' WHERE id=?", (key,))
            else:
                row = c.execute("SELECT attempts FROM mirrors WHERE id=?", (key,)).fetchone()
                if row:
                    attempts = row[0] + 1
                    c.execute(
                        "UPDATE mirrors SET attempts=?,next_try=? WHERE id=?",
                        (attempts, time.time() + min(60, 2 ** min(attempts, 6)), key),
                    )


def upload_mirror(url, token, row):
    key, channel, stamp, text, received, attempts = row
    payload = {"channel": channel, "stamp": stamp, "text": text, "queued_at": received, "receive_only": True}
    request = urllib.request.Request(
        url,
        json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        result = json.load(response)
    if result.get("status") not in {"listened", "duplicate"} or result.get("reply") is not None:
        raise ValueError("App mirror was not acknowledged as receive-only")
