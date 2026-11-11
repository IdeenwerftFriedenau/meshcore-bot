"""Rundumtest auf dem Pi: echte Fragen erneut stellen, Börsenbefehle über die Engine, Kursalarm und Schlussbericht.

Sendet nichts über Funk und schreibt nicht in die Bot-Datenbank: alles läuft in einer Testdatenbank im Speicher.
Die echte Datenbank wird nur lesend geöffnet, um die zuletzt gestellten Fragen zu holen.

    python rundum.py /etc/meshcore-ai-bot/config.toml [Anzahl echter Fragen, Standard 120]
"""

import asyncio
import glob
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from meshcore_ai_bot.assistant import Assistant
from meshcore_ai_bot.config import load_config
from meshcore_ai_bot.engine import Engine
from meshcore_ai_bot.protocol import Incoming, split_text
from meshcore_ai_bot.settings import refresh_secrets
from meshcore_ai_bot.store import Store

BAD = {
    "leer": lambda t: not t.strip(),
    "fehler": lambda t: bool(re.search(r"schiefgegangen|nicht (verlässlich )?abrufbar|zu viele Fragen|Traceback", t)),
    "ausweichend": lambda t: bool(re.search(r"weiß ich nicht|kann ich (dir )?(leider )?nicht|keine (aktuellen )?Daten|"
                                            r"keinen Zugriff|habe keine Informationen", t, re.I)),
    "englisch": lambda t: bool(re.search(r"\b(the|and|you|is|are|sorry)\b.*\b(the|and|you|is|are)\b", t)) and
    not re.search(r"[äöüß]", t),
    "markdown": lambda t: bool(re.search(r"\*\*|^#|\[[^\]]+\]\(http", t, re.M)),
}
SKIP = re.compile(r"^(ping|test|hilfe|help|\?|123|1 2 3|danke|ok|moin|hallo)\W*$", re.I)


def database(config_path: str, config) -> str | None:
    rel = config.storage.path if hasattr(config, "storage") else "data/bot.sqlite3"
    rel = str(rel)
    cands = [rel] if os.path.isabs(rel) else [os.path.join(b, rel) for b in (
        "/var/lib/meshcore-ai-bot", "/opt/meshcore-ai-bot/current", "/opt/meshcore-ai-bot")]
    cands += glob.glob("/var/lib/meshcore-ai-bot/**/*.sqlite3", recursive=True)
    for path in dict.fromkeys(cands):
        if os.path.exists(path):
            try:
                db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                db.execute("SELECT 1 FROM messages LIMIT 1")
                return path
            except sqlite3.Error:
                continue
    return None


def real_questions(path: str, limit: int) -> list[tuple[str, str, str]]:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rows = db.execute("SELECT channel, question, answer FROM messages ORDER BY received DESC LIMIT 2000").fetchall()
    seen, picked = set(), []
    for channel, question, answer in rows:
        key = " ".join(question.casefold().split())
        if not key or key in seen or SKIP.match(key) or len(key) < 3:
            continue
        seen.add(key)
        picked.append((channel, question, answer or ""))
        if len(picked) >= limit:
            break
    return list(reversed(picked))


def flags(config, text: str) -> list[str]:
    found = [name for name, check in BAD.items() if check(text)]
    parts = split_text("@[" + "x" * 16 + "] " + text, config.radio.packet_bytes)
    if len(parts) > config.radio.reply_max_packets:
        found.append(f"zu_lang({len(parts)} Pakete)")
    return found


async def replay(config, client, store, questions, pause):
    print(f"\n================ 1. ECHTE FRAGEN ERNEUT ({len(questions)}) ================")
    assistant = Assistant(config, store, client)
    stats = Counter()
    for index, (channel, question, old) in enumerate(questions, 1):
        ch = config.channel(channel)
        topic = getattr(ch, "topic", "") if ch else ""
        started = time.monotonic()
        try:
            answer = await (assistant.answer(question, topic=topic) if topic else assistant.answer(question))
            text = answer.text
        except Exception as exc:  # noqa: BLE001
            text = f"Traceback {type(exc).__name__}: {exc}"
        took = time.monotonic() - started
        bad = flags(config, text)
        stats["auffällig" if bad else "ok"] += 1
        print(f"\n#{index} [{channel}] {question!r} ({took:.1f} s){'  AUFFÄLLIG: ' + str(bad) if bad else ''}")
        print(f"   neu: {text}")
        if old and old.strip() != text.strip():
            print(f"   alt: {old}")
        sys.stdout.flush()
        await asyncio.sleep(pause)
    print(f"\nEchte Fragen: {dict(stats)}")
    return stats


async def drain(store) -> list[dict]:
    out = []
    while packet := store.claim(time.time() + 1, lambda *_: True):
        out.append(packet)
        store.finish(packet["id"], "accepted", time.time())
    return out


async def boerse_engine(config, client, store, pause):
    print("\n================ 2. BÖRSENBEFEHLE ÜBER DIE ENGINE (#börsenbot) ================")
    channel = next((c.name for c in config.channels if getattr(c, "topic", "") == "boerse"), None)
    if not channel:
        print("Kein Kanal mit topic=boerse.")
        return Counter(fehler=1), None
    engine = Engine(config, store, Assistant(config, store, client))
    stats = Counter()
    cases = [
        ("hilfe", r"^Börsenbot:"), ("Top Flop DAX", r"%"), ("Krypto Stimmung", r"/100|Angst|Gier|neutral"),
        ("Nachrichten zu SAP", r"SAP"), ("Warum fällt Rheinmetall?", r"Rheinmetall"), ("Silberpreis", r"^Silber"),
        ("Wie läuft die Börse?", r"Silber"), ("Euro Dollar", r"EZB"), ("Vergleiche Gold und Silber", r"Gold.*Silber"),
        ("Alarme", r"keine Kursalarme"), ("Alarm Bitcoin über 999999", r"Kursalarm gesetzt"),
        ("Alarm SAP unter 1", r"Kursalarm gesetzt"), ("Alarme", r"Bitcoin.*SAP|SAP.*Bitcoin"),
        ("Alarm Bitcoin unter 1", r"Kursalarm gesetzt|schon 3"), ("Alarm Gold über 99999", r"schon 3"),
        ("Alarme löschen", r"gelöscht"), ("Alarme", r"keine Kursalarme"), ("Alarm Quatschfirma123 über 5", r"finde ich keinen"),
    ]
    for text, expect in cases:
        now = int(time.time())
        decision = engine.receive(Incoming(channel, "RundumTest", text, now))
        if decision == "queued":
            await engine.process_one()
        packets = await drain(store)
        joined = " ".join(re.sub(r"^\[\d+/\d+\] ", "", p["text"]) for p in packets if p.get("stream") == "reply")
        joined = re.sub(r"^@\[[^\]]*\]\s*", "", joined)
        ok = bool(packets) and re.search(expect, joined, re.S) is not None
        stats["ok" if ok else "auffällig"] += 1
        print(f"\n{'OK ' if ok else 'XX '} {text!r} ({decision}, {len(packets)} Pakete)\n   {joined}")
        sys.stdout.flush()
        await asyncio.sleep(pause)
    print(f"\nBörsenbefehle: {dict(stats)}")
    return stats, engine


async def alarm_and_report(config, store, engine):
    print("\n================ 3. KURSALARM AUSLÖSEN UND SCHLUSSBERICHT ================")
    stats = Counter()
    watch = engine.watch if engine else None
    channel = next((c.name for c in config.channels if getattr(c, "topic", "") == "boerse"), None)
    if not watch or not channel:
        print("Keine Kursüberwachung aktiv.")
        return Counter(fehler=1)
    now = time.time()
    reply = await watch.command("RundumTest", channel, "Alarm Bitcoin über 999999", now)
    print("Alarm setzen:", reply)
    rows = watch.alarms()
    for row in rows:
        if row["sender"] == "RundumTest":
            row["level"] = 1.0  # Marke so legen, dass der Alarm beim nächsten Check auslöst
    watch.save(rows)
    await watch.check_alarms(now + 1)
    packets = [p for p in await drain(store) if p.get("stream") == "markets"]
    fired = any("Kursalarm" in p["text"] for p in packets)
    stats["ok" if fired and not watch.alarms() else "auffällig"] += 1
    print(f"{'OK ' if fired else 'XX '} Alarm ausgelöst: {[p['text'] for p in packets]}; übrig: {watch.alarms()}")
    tz = ZoneInfo(config.station.timezone)
    local = datetime.now(tz)
    at = (config.research.market_report_time or "17:45").split(":")
    probe = local.replace(hour=int(at[0]), minute=int(at[1]) + 5 if int(at[1]) < 55 else int(at[1]), second=0)
    print(f"Bericht fällig um {':'.join(at)} (Werktag)? {watch.report_due(probe.timestamp())}")
    await watch.close_report(probe.timestamp())
    packets = [p for p in await drain(store) if p.get("stream") == "markets"]
    text = " ".join(re.sub(r"^\[\d+/\d+\] ", "", p["text"]) for p in packets)
    if local.weekday() < 5:
        ok = text.startswith("Börsenschluss") and "Silber" in text
        stats["ok" if ok else "auffällig"] += 1
        print(f"{'OK ' if ok else 'XX '} Schlussbericht ({len(packets)} Pakete): {text or '(keiner, Feiertag?)'}")
    else:
        print("Wochenende: kein Schlussbericht erwartet.", text)
    return stats


async def main(path, limit, pause):
    config = load_config(path)
    refresh_secrets(config, path)
    config.ai.requests_per_minute = 30
    config.radio.incoming_per_minute = 60
    config.radio.incoming_per_sender_per_minute = 30
    db = database(path, config)
    questions = real_questions(db, limit) if db else []
    print(f"Datenbank: {db or 'nicht gefunden'}; {len(questions)} verschiedene echte Fragen")
    print("Kanäle:", Counter(c for c, _, _ in questions))
    store = Store(":memory:")
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=20) as client:
        s1 = await replay(config, client, store, questions, pause)
        s2, engine = await boerse_engine(config, client, store, pause)
        s3 = await alarm_and_report(config, store, engine)
    store.close()
    print("\n================ ZUSAMMENFASSUNG RUNDUM ================")
    print(f"Dauer {(time.monotonic() - started) / 60:.1f} min")
    print("1 echte Fragen:", dict(s1))
    print("2 Börsenbefehle:", dict(s2))
    print("3 Alarm/Bericht:", dict(s3))


asyncio.run(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 120, 2.5))
