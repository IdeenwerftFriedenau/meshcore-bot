"""Letzte Fragen und Antworten des Bots, nur lesend, ohne Absendernamen.

Aufruf auf dem Pi:  sudo /opt/meshcore-ai-bot/current/.venv/bin/python -I letzte.py [anzahl] [suchwort]
"""

import os
import sqlite3
import sys
import time
import tomllib


def database() -> str:
    cfg = tomllib.load(open("/etc/meshcore-ai-bot/config.toml", "rb"))
    rel = cfg.get("storage", {}).get("path", "data/bot.sqlite3")
    for path in [rel] if os.path.isabs(rel) else [os.path.join(b, rel) for b in (
            "/var/lib/meshcore-ai-bot", "/opt/meshcore-ai-bot/current", "/opt/meshcore-ai-bot")]:
        if os.path.exists(path):
            return path
    sys.exit("Keine Datenbank gefunden.")


LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 20
WORD = sys.argv[2] if len(sys.argv) > 2 else ""

db = sqlite3.connect(f"file:{database()}?mode=ro", uri=True)
rows = db.execute("SELECT received,channel,question,status,answer FROM messages WHERE question!='' "
                  "AND (?='' OR question LIKE ? OR answer LIKE ?) ORDER BY received DESC LIMIT ?",
                  (WORD, f"%{WORD}%", f"%{WORD}%", LIMIT)).fetchall()
for rec, ch, q, st, a in reversed(rows):
    print(f"{time.strftime('%d.%m. %H:%M', time.localtime(rec))} {ch} [{st}]")
    print("  F:", " ".join(q.split()))
    print("  A:", " ".join((a or "(keine Antwort)").split()))
