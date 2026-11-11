"""Qualitätsbericht: schwache Bot-Antworten der letzten Tage, nur lesend, ohne Absendernamen.

Aufruf auf dem Pi:  sudo /opt/meshcore-ai-bot/current/.venv/bin/python -I bericht.py [tage] [anzahl]
"""

import os
import re
import sqlite3
import sys
import time
import tomllib
from collections import Counter

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 7
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 30
WEAK = re.compile(
    r"nicht abrufbar|finde ich (keine|keinen|nichts)|weiß ich nicht|kann ich (dir )?nicht|habe ich (dafür )?nicht|"
    r"keine (aktuellen|daten|informationen|angaben)|schiefgegangen|fehler|leider|nicht verfügbar|unbekannt|"
    r"nicht sicher|keine ahnung|nicht beantworten|versuch es (bitte )?(gleich|später)", re.I)
UNHAPPY = re.compile(r"^(falsch|stimmt nicht|quatsch|nein|hä|was\?|hä\?|das war nicht|nicht das|ich meinte|"
                     r"ich meine|nochmal|wieder falsch|blödsinn|unsinn)\b", re.I)


def database() -> str:
    cfg = tomllib.load(open("/etc/meshcore-ai-bot/config.toml", "rb"))
    rel = cfg.get("storage", {}).get("path", "data/bot.sqlite3")
    for path in [rel] if os.path.isabs(rel) else [os.path.join(b, rel) for b in (
            "/var/lib/meshcore-ai-bot", "/opt/meshcore-ai-bot/current", "/opt/meshcore-ai-bot")]:
        if os.path.exists(path):
            return path
    sys.exit("Keine Datenbank gefunden.")


def short(text: str, n: int = 140) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


db = sqlite3.connect(f"file:{database()}?mode=ro", uri=True)
since = time.time() - DAYS * 86400
rows = db.execute("SELECT received,channel,sender,question,status,answer FROM messages "
                  "WHERE received>=? AND question!='' ORDER BY received", (since,)).fetchall()
print(f"Qualitätsbericht {time.strftime('%d.%m.%Y %H:%M')}: {len(rows)} Fragen in {DAYS} Tagen")
print("Status:", ", ".join(f"{k} {v}" for k, v in Counter(r[4] for r in rows).most_common()))
print("Kanäle:", ", ".join(f"{k} {v}" for k, v in Counter(r[1] for r in rows).most_common(8)))

flagged = []
for i, (rec, ch, snd, q, st, a) in enumerate(rows):
    reasons = []
    if not (a or "").strip():
        reasons.append("leer")
    elif WEAK.search(a):
        reasons.append("schwach")
    if st not in {"accepted", "answered", "prepared", "sent"}:
        reasons.append(st)
    # The same person reacts within five minutes with a correction or asks again: the answer missed.
    nxt = next((r for r in rows[i + 1:] if r[2] == snd and r[1] == ch and r[0] - rec < 300), None)
    if nxt and (UNHAPPY.search(nxt[3].strip()) or nxt[3].strip().casefold() == q.strip().casefold()):
        reasons.append("nachgehakt: " + short(nxt[3], 40))
    if reasons:
        flagged.append((rec, ch, q, a, reasons))

print(f"Auffällig: {len(flagged)} von {len(rows)}")
words = Counter(w for _, _, q, _, _ in flagged for w in re.findall(r"[a-zäöüß]{4,}", q.casefold()))
print("Häufige Wörter darin:", ", ".join(w for w, _ in words.most_common(15)))
for rec, ch, q, a, reasons in flagged[-LIMIT:]:
    print(f"\n[{time.strftime('%d.%m. %H:%M', time.localtime(rec))} {ch}] {', '.join(reasons)}")
    print("  F:", short(q))
    print("  A:", short(a))
