"""Read-only Pi diagnostics. Never opens USB, sends packets or calls external services."""

import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path


def doctor(config):
    path = Path(config.storage.path)
    parent = path.parent
    existing = parent
    while not existing.exists():
        existing = existing.parent
    free = shutil.disk_usage(existing).free
    checks = {
        "free_space": free >= 100 * 1024 * 1024,
        "state_directory_writable": parent.is_dir() and os.access(parent, os.W_OK),
    }
    if path.is_file():
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                checks["database_integrity"] = db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                checks["database_schema"] = db.execute("PRAGMA user_version").fetchone()[0] == 3
        except sqlite3.Error:
            checks["database_integrity"] = False
    else:
        checks["database_exists"] = False
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "free_bytes": free,
        "usb_path_present": Path(config.radio.port).exists(),
        "radio_opened": False,
        "network_used": False,
    }
