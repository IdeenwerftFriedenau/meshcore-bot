"""Tägliche Prüfung der externen Datenquellen des Bots. Liest nur, fragt keine KI und sendet
standardmäßig nichts über Funk.

Aufruf auf dem Pi:
  sudo /opt/meshcore-ai-bot/current/.venv/bin/python -I pruefen.py [config.toml]

Jede Quelle wird mit einer festen Abfrage geholt und die Antwort auf ein erwartetes Muster geprüft.
Schlägt eine Quelle fehl, folgt nach einer Pause ein zweiter Versuch. Das Ergebnis steht in
``$STATE_DIRECTORY/status.json`` (Standard /var/lib/meshcore-quellen) und im Journal.

Rückgabewert: 0 alles in Ordnung, 1 mindestens eine Quelle gestört, 2 das Prüfprogramm selbst ist defekt.

Optional meldet die Prüfung eine Änderung der gestörten Quellen über die lokale Funkwarteschlange:
``QUELLEN_FUNK_KANAL=#test`` in der Dienstumgebung setzen. Ohne diese Angabe wird nie gesendet.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path

# (Anzeigename, Werkzeug, Argumente, Muster, das eine brauchbare Antwort zeigt)
CHECKS = [
    ("Wetter (Open-Meteo)", "get_weather", {"location": "Berlin", "period": "today"}, r"°"),
    ("Abfahrten (BVG)", "get_departures", {"stop": "S Friedenau"}, r"\d"),
    ("Verbindungen (BVG)", "get_journey", {"origin": "S Friedenau", "destination": "Alexanderplatz"}, r"\d\d:\d\d"),
    ("Störungen (BVG)", "get_transit_disruptions", {"line": "S1"}, r"\w"),
    ("Flüge (BER)", "get_flights", {"direction": "ab", "query": ""}, r"BER"),
    ("Flugzeuge (OpenSky)", "get_planes_overhead", {"area": "berlin"}, r"\w"),
    ("Hähnchenwagen", "get_grill_stands", {"day": "", "place": "", "near": ""}, r"\w"),
    ("Kurse (Yahoo Finance)", "get_quote", {"asset": "SAP"}, r"SAP.*\d"),
    ("Tagesgewinner DAX", "get_market_movers", {}, r"\d"),
    ("Krypto-Stimmung", "get_market_mood", {}, r"\d"),
    ("Börsennachrichten", "get_market_news", {"asset": "SAP"}, r"\w"),
    ("Kennzahlen (SEC, Wikidata)", "get_fundamentals", {"asset": "Apple"}, r"\d"),
    ("Insider-Meldungen (BaFin)", "get_insider_trades", {"asset": "SAP"}, r"\w"),
    ("Warnungen (DWD, NINA)", "get_warnings", {}, r"\w"),
    ("Weltraumwetter (NOAA)", "get_space_weather", {}, r"Kp"),
    ("Fußball (OpenLigaDB)", "get_football", {"team": "", "league": "1"}, r"\w"),
    ("Luft (UBA)", "get_air_quality", {"place": ""}, r"\w"),
    ("Pollen (DWD)", "get_pollen", {}, r"\w"),
    ("Feiertage und Ferien", "get_holidays", {"kind": "feiertag", "day": ""}, r"\d"),
    ("Spritpreise (Tankerkönig)", "get_fuel_prices", {"fuel": "diesel", "place": ""}, r"\d,\d|Schlüssel"),
    ("Nachrichten", "search_news", {"query": "Berlin"}, r"\w"),
    ("Wikipedia", "search_wikipedia", {"query": "Berlin"}, r"Berlin"),
    ("Orte in der Nähe", "find_local_places", {"query": "Apotheke", "location": "Friedenau"}, r"\w"),
]
BROKEN = re.compile(r"nicht (abrufbar|erreichbar|verlässlich)", re.I)


async def ask(tools, name: str, args: dict, pattern: str) -> str:
    """'' when the source answered usefully, otherwise a short reason."""
    try:
        result = await asyncio.wait_for(tools.run(name, {k: v for k, v in args.items() if v != ""}), timeout=60)
    except Exception as exc:  # noqa: BLE001 - any failure of a source is a finding, not a crash
        return f"{type(exc).__name__}: {str(exc)[:120]}"
    text = str(result.get("text", ""))
    if not text.strip():
        return "leere Antwort"
    if BROKEN.search(text):
        return "Quelle meldet Störung: " + text[:120]
    if not re.search(pattern, text, re.S):
        return "unerwartete Antwort: " + text[:120]
    return ""


async def run_checks(tools, checks=CHECKS, *, pause: float = 20.0, sleep=asyncio.sleep) -> dict[str, str]:
    """Display name -> reason for every source that failed twice."""
    failed = {}
    for label, name, args, pattern in checks:
        reason = await ask(tools, name, args, pattern)
        if reason:
            failed[label] = (name, args, pattern, reason)
        await sleep(0.5)
    if failed:
        await sleep(pause)  # throttling (Yahoo, Open-Meteo) usually clears within seconds
    final = {}
    for label, (name, args, pattern, _first) in failed.items():
        reason = await ask(tools, name, args, pattern)
        if reason:
            final[label] = reason
    return final


def report(failed: dict[str, str], total: int, state_dir: Path, now: float) -> tuple[dict, bool]:
    """Writes status.json; returns the status and whether the set of broken sources changed."""
    path = state_dir / "status.json"
    try:
        before = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        before = {}
    since = before.get("since", {}) if isinstance(before.get("since"), dict) else {}
    status = {
        "checked": now,
        "total": total,
        "failed": failed,
        "since": {label: since.get(label, now) for label in failed},
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temp, path)
    return status, set(before.get("failed", {})) != set(failed)


def radio_text(failed: dict[str, str]) -> str:
    if not failed:
        return "Quellenprüfung: alle Datenquellen des Bots antworten wieder."
    names = ", ".join(label.split(" (")[0] for label in failed)
    text = f"Quellenprüfung: gestört: {names}"
    return text if len(text.encode()) <= 110 else text.encode()[:107].decode("utf-8", "ignore") + "…"


def queue_radio(channel: str, text: str, outbox: Path, now: float) -> None:
    """Same queue as the local console; the gateway sends it with its normal pacing."""
    with sqlite3.connect(outbox, timeout=3) as db:
        db.execute("CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,channel TEXT NOT NULL,"
                   "text TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL)")
        db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?)", (str(uuid.uuid4()), channel, text, "queued", now, now))


async def main() -> int:
    import httpx

    from meshcore_ai_bot.config import load_config
    from meshcore_ai_bot.store import Store
    from meshcore_ai_bot.tools import Tools

    config = load_config(sys.argv[1] if len(sys.argv) > 1 else "/etc/meshcore-ai-bot/config.toml")
    checks = [c for c in CHECKS if config.research.transit_url or c[1] not in
              {"get_departures", "get_journey", "get_transit_disruptions"}]
    store = Store(":memory:")
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        failed = await run_checks(Tools(config, store, client), checks)
    store.close()
    now = time.time()
    state_dir = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/meshcore-quellen"))
    _status, changed = report(failed, len(checks), state_dir, now)
    print(f"{len(checks) - len(failed)}/{len(checks)} Quellen in Ordnung.")
    for label, reason in failed.items():
        print(f"GESTÖRT {label}: {reason}")
    channel = os.environ.get("QUELLEN_FUNK_KANAL", "").strip()
    if channel and changed:
        outbox = Path(os.environ.get("LOCAL_OUTBOX_PATH", "/var/lib/meshcore-gateway/local-outbox.sqlite3"))
        queue_radio(channel, radio_text(failed), outbox, now)
        print(f"Meldung für {channel} vorgemerkt.")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    except Exception as exc:  # noqa: BLE001 - a broken checker must be told apart from a broken source
        print(f"PRÜFPROGRAMM DEFEKT {type(exc).__name__}: {exc}")
        code = 2
    sys.exit(code)
