#!/usr/bin/env python3
"""Offline-first status dashboard for the local MeshCore and DAB+ services."""

import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

try:
    from meshcore_ai_bot.dab_control import CONTROL_PATH, PRESETS, fresh_signal, load_settings, save_settings
except ModuleNotFoundError:
    from dab_control import CONTROL_PATH, PRESETS, fresh_signal, load_settings, save_settings

try:
    from meshcore_ai_bot.dab_scan import INVENTORY, SCAN_STATE, read_json, scan_status, write_json
except ModuleNotFoundError:
    from dab_scan import INVENTORY, SCAN_STATE, read_json, scan_status, write_json

HOST = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
PORT = int(os.environ.get("DASHBOARD_PORT", "8088"))
RECORDINGS = Path(os.environ.get("DAB_RECORDINGS_DIR", "/home/marc/dab-warnings-meshcore/recordings"))
INBOX = Path(os.environ.get("INBOX_PATH", "/var/lib/meshcore-gateway/inbox.sqlite3"))
OUTBOX = Path(os.environ.get("LOCAL_OUTBOX_PATH", "/var/lib/meshcore-gateway/local-outbox.sqlite3"))
BERLIN = ZoneInfo("Europe/Berlin")
SERVICES = ("meshcore-gateway.service", "ideenwerft-dab-monitor.service", "ideenwerft-dab-news.service")
CHANNELS = ("#ideenwerft", "#test", "#mc-treffen", "#warnings", "#news", "#fragdenbot", "#bot")


def outbox_db():
    OUTBOX.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(OUTBOX, timeout=3)
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,channel TEXT NOT NULL,text TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL)"
    )
    return db


def local_outbox():
    try:
        with outbox_db() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,channel,text,status,created,updated FROM messages ORDER BY created DESC LIMIT 20"
                )
            ]
    except Exception:
        return []


def service(name):
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "is-active", name], capture_output=True, text=True, timeout=3
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unavailable"


def local_json(path):
    try:
        with urlopen(
            Request("http://127.0.0.1:7979/" + path, headers={"Accept": "application/json"}), timeout=2
        ) as response:
            return json.loads(response.read(300000))
    except Exception:
        return None


def current_dab_channel():
    try:
        with urlopen("http://127.0.0.1:7979/channel", timeout=2) as response:
            channel = response.read(32).decode().strip()
        return channel if channel in PRESETS else None
    except (OSError, ValueError):
        return None


def probe_remote_state():
    url = os.environ.get("INGEST_URL", "")
    parts = urlsplit(url)
    if not parts.scheme or "/api/" not in parts.path:
        return {"state": "unconfigured"}
    health = parts._replace(path=parts.path.split("/api/", 1)[0] + "/health", query="", fragment="").geturl()
    try:
        with urlopen(Request(health, headers={"Accept": "application/json"}), timeout=4) as response:
            return {"state": "online" if response.status == 200 else "error", "checked": time.time()}
    except Exception:
        return {"state": "offline", "checked": time.time()}


REMOTE_STATE = {"state": "checking"}


def monitor_remote():
    global REMOTE_STATE
    while True:
        REMOTE_STATE = probe_remote_state()
        time.sleep(30)


def remote_state():
    return dict(REMOTE_STATE)


def bot_state():
    try:
        data = json.loads(Path("/var/lib/meshcore-ai-bot/bot.sqlite3.dashboard.json").read_text())
        meta = data.get("meta", {})
        heartbeat = float(meta.get("bot:heartbeat", 0))
        return {
            "version": meta.get("bot:version", "0.4.3"),
            "connected": str(meta.get("bot:connected")) == "1" and time.time() - heartbeat < 90,
            "age": round(time.time() - heartbeat),
            "packets": data.get("packets", {}),
            "channels": json.loads(meta.get("bot:receive_channels", "[]")),
            "repeaters": len(data.get("repeaters", [])),
            "reception": data.get("reception", [])[:40],
            "asa_forward_status": meta.get("asa:forward_status", "starting"),
            "asa_watch_age": time.time() - float(meta.get("asa:watch_heartbeat", 0)),
            "asa_validated": json.loads(meta.get("asa:validated", "{}")),
        }
    except (OSError, ValueError, TypeError):
        return {
            "version": "0.4.3",
            "connected": False,
            "age": None,
            "packets": {},
            "channels": [],
            "repeaters": 0,
            "reception": [],
        }


EMERGENCY = """<!doctype html><html lang=de><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Ideenwerft Notfallzugriff</title><style>body{max-width:760px;margin:40px auto;padding:20px;font:18px system-ui;line-height:1.6;background:#10201c;color:#edf6f2}a{color:#72d7a3}code{overflow-wrap:anywhere}</style><h1>Lokaler Notfallzugriff</h1><p>Diese Seite liegt direkt auf dem Pi. Internet ist dafür nicht erforderlich.</p><p><a href="/">Funk-Dashboard öffnen</a></p><ol><li>Handy oder Computer mit demselben Router beziehungsweise lokalen WLAN verbinden.</li><li><b>http://192.168.8.200:8091</b> öffnen. Alternativ <b>http://marc-desktop.local:8091</b>.</li><li>Unter „MeshCore-Nachricht senden“ einen erlaubten Kanal wählen. „accepted“ bedeutet Annahme durch das eigene Funkgerät, keine Empfangsbestätigung.</li></ol><p>Ohne Internet verfügbar: Funknachrichten senden, lokaler Empfang, Kontaktbestand, DAB+/ASA-Anzeige und vorhandene DLF-Aufnahmen. Der Bot beantwortet ping, Funkkürzel, Rechnen und Memory-Befehle lokal. Aktuelle Wetterdaten, externe Nachrichten und freie KI-Antworten benötigen Internet.</p><p>Pi, Funkgerät und Router/WLAN müssen mit Strom versorgt sein. Diese Einrichtung erzeugt kein zusätzliches WLAN und überbrückt keinen Stromausfall.</p><h2>Wartung</h2><p>SSH im lokalen Netz: <code>ssh marc@192.168.8.200</code>. Konfigurationsoberfläche über einen lokalen Tunnel: <code>ssh -L 8765:127.0.0.1:8765 marc@192.168.8.200</code>, dann <code>http://127.0.0.1:8765</code> öffnen.</p><p>Die GitHub-Version liegt zusätzlich lokal unter <code>/opt/meshcore-ai-bot/source</code>. Ein Internetzugang zu GitHub ist zum Starten nicht nötig. Sicherungen und Wiederherstellung: <code>/var/backups/meshcore-pi-v04/README.txt</code>.</p></html>"""


def recordings():
    now = time.time()
    items = []
    for path in RECORDINGS.glob("dlf-*.mp3"):
        match = re.fullmatch(r"dlf-(\d+)\.mp3", path.name)
        if not match:
            continue
        stamp = int(match.group(1))
        if now - stamp > 86400 or stamp > now + 300:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        items.append(
            {
                "file": path.name,
                "stamp": stamp,
                "time": datetime.fromtimestamp(stamp, BERLIN).strftime("%d.%m. %H:%M"),
                "bytes": size,
            }
        )
    return sorted(items, key=lambda item: item["stamp"], reverse=True)


def inbox_state(channel=None):
    if not INBOX.exists():
        return {"available": False}
    try:
        with sqlite3.connect(INBOX, timeout=1) as db:
            since = time.time() - 86400
            where = "received>?" + (" AND channel=?" if channel else "")
            parameters = (since, channel) if channel else (since,)
            rows = db.execute(
                "SELECT state,COUNT(*) FROM messages WHERE " + where + " GROUP BY state", parameters
            ).fetchall()
            latest = db.execute(
                "SELECT channel,text,received,state FROM messages WHERE "
                + where
                + " ORDER BY received DESC LIMIT 100",
                parameters,
            ).fetchall()
            channels = [
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT channel FROM messages WHERE received>? ORDER BY channel", (since,)
                )
            ]
        return {
            "available": True,
            "counts": dict(rows),
            "channel": channel,
            "channels": channels,
            "limit": 100,
            "latest": [
                {"channel": r[0], "text": r[1][:1200], "received": r[2], "state": r[3]} for r in latest
            ],
        }
    except Exception:
        return {"available": False}


def asa_diagnostics(mux, now):
    asa = mux.get("asa", {}) if isinstance(mux, dict) else {}
    demod = mux.get("demodulator", {}) if isinstance(mux, dict) else {}
    frame = demod.get("time_last_fct0_frame")
    age = None
    if isinstance(frame, (int, float)) and not isinstance(frame, bool) and frame > 0:
        elapsed = now - frame / 1000
        if -5 <= elapsed < 86400:
            age = max(0, round(elapsed, 1))
    return {
        "frame_age_seconds": age,
        "signal_fresh": fresh_signal(mux, now),
        "asa_data_present": isinstance(asa, dict) and type(asa.get("active")) is bool,
        "ews_ensemble": asa.get("ews_ensemble") is True,
        "emergency_warning": asa.get("emergency_warning") is True,
        "asa_changed_at": asa.get("last_change"),
        "asa_forwarding": False,
        "dab_channel": "5C",
    }


def snapshot():
    mux = local_json("mux.json") or {}
    journaline = local_json("journaline.json") or {}
    demod = mux.get("demodulator", {}) if isinstance(mux, dict) else {}
    ensemble = mux.get("ensemble", {}).get("label", {}) if isinstance(mux, dict) else {}
    settings = load_settings(CONTROL_PATH)
    actual_channel = current_dab_channel()
    bot = bot_state()
    checked = bot.get("asa_validated", {})
    verified = (
        checked.get("validated") is True
        and -5 <= time.time() - checked.get("validated_at", 0) < 15
        and checked.get("channel") == actual_channel
    )
    checked_signals = [s for s in checked.get("signals", []) if s.get("active")]
    return {
        "now": time.time(),
        "services": {name: service(name) for name in SERVICES},
        "remote": remote_state(),
        "bot": bot,
        "dab": {
            "reachable": bool(mux),
            "snr": demod.get("snr"),
            "ensemble": ensemble.get("label", ""),
            **asa_diagnostics(mux, time.time()),
            "asa_data_present": verified,
            "asa_active": verified and checked.get("active") is True,
            "emergency_warning": verified and checked.get("active") is True and not checked.get("is_test"),
            "asa_test": verified and checked.get("is_test") is True,
            "asa_outside_area": verified and checked.get("outside_area") is True,
            "asa_status": ("test" if checked.get("is_test") else "actual") if verified else "unverified",
            "asa_level": checked_signals[0].get("level", 0) if verified and checked_signals else 0,
            "ews_ensemble": verified and checked.get("ews_ensemble") is True,
            "dab_channel": actual_channel,
            "desired_channel": settings["channel"],
            "presets": [{"channel": key, **value} for key, value in PRESETS.items()],
            "asa_forwarding_requested": settings["forward_asa"],
            "asa_forwarding": settings["forward_asa"]
            and bot.get("asa_watch_age", 999) > -5
            and bot.get("asa_watch_age", 999) < 30
            and bot.get("asa_forward_status")
            in {"idle", "queued", "duplicate", "test_suppressed", "outside_area"},
            "services": [
                {
                    "sid": str(s.get("sid", ""))[:16],
                    "name": str(s.get("label", {}).get("label", ""))[:80].strip(),
                }
                for s in mux.get("services", [])[:64]
            ],
            "inventory": read_json(INVENTORY, {"ensembles": [], "complete": False}),
            "scan": scan_status(),
            "journaline": journaline.get("objects", [])[:8] if isinstance(journaline, dict) else [],
        },
        "recordings": recordings(),
        "inbox": inbox_state(),
        "outbox": local_outbox(),
        "channels": CHANNELS,
    }


ASSETS = Path(os.environ.get("DASHBOARD_ASSETS", str(Path(__file__).with_name("local-console"))))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body, content_type="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/api/inbox":
            values = parse_qs(parsed.query, keep_blank_values=True).get("channel", [""])
            channel = values[0]
            if len(values) != 1 or len(channel.encode()) > 32 or any(ord(c) < 32 for c in channel):
                return self.reply(400, b'{"error":"Invalid channel filter"}', "application/json")
            return self.reply(
                200,
                json.dumps(inbox_state(channel or None), ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
            )
        if self.path == "/notfall":
            return self.reply(200, EMERGENCY.encode())
        if self.path in ("/", "/index.html"):
            return self.reply(200, (ASSETS / "index.html").read_bytes())
        if self.path in ("/console.css", "/console.js"):
            content_type = (
                "text/css; charset=utf-8" if self.path.endswith(".css") else "text/javascript; charset=utf-8"
            )
            return self.reply(200, (ASSETS / self.path.lstrip("/")).read_bytes(), content_type)
        if self.path == "/api/status":
            return self.reply(
                200, json.dumps(snapshot(), ensure_ascii=False).encode(), "application/json; charset=utf-8"
            )
        match = re.fullmatch(r"/audio/(dlf-\d+\.mp3)", self.path)
        if match:
            path = RECORDINGS / match.group(1)
            if not path.is_file() or time.time() - path.stat().st_mtime > 90000:
                return self.reply(404, b"Nicht gefunden", "text/plain")
            data = path.read_bytes()
            return self.reply(200, data, "audio/mpeg")
        return self.reply(404, b"Nicht gefunden", "text/plain")

    def do_POST(self):
        if self.path not in {"/api/send", "/api/dab/settings", "/api/dab/scan"}:
            return self.reply(404, b"Nicht gefunden", "text/plain")
        try:
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                raise ValueError("Fremder Ursprung nicht erlaubt")
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("JSON erforderlich")
            size = int(self.headers.get("Content-Length", "0"))
            if size < 2 or size > 1024:
                raise ValueError("Ungültige Nachricht")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("JSON-Objekt erforderlich")
            if self.path in {"/api/dab/settings", "/api/dab/scan"} and scan_status().get("running"):
                raise ValueError("DAB-Suchlauf läuft; bitte bis zur Wiederherstellung warten.")
            if self.path == "/api/dab/scan":
                if data:
                    raise ValueError("Suchlauf erwartet keine Parameter")
                write_json(
                    SCAN_STATE,
                    {"running": True, "index": 0, "total": len(PRESETS), "updated_at": time.time()},
                )
                try:
                    subprocess.Popen(
                        ["/opt/meshcore-ai-bot/current/.venv/bin/python", "-m", "meshcore_ai_bot.dab_scan"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except OSError:
                    write_json(
                        SCAN_STATE, {"running": False, "error": "Suchlauf konnte nicht gestartet werden."}
                    )
                    return self.reply(503, b'{"error":"Scan unavailable"}', "application/json")
                return self.reply(202, b'{"status":"started"}', "application/json")
            if self.path == "/api/dab/settings":
                settings = save_settings(data, CONTROL_PATH)
                return self.reply(
                    202,
                    json.dumps({"status": "requested", "settings": settings}).encode(),
                    "application/json",
                )
            channel = data.get("channel")
            text = " ".join(str(data.get("text", "")).split())
            if channel not in CHANNELS:
                raise ValueError("Ungültiger Kanal")
            if not text or len(text.encode()) > 110:
                raise ValueError("Nachricht muss 1 bis 110 Bytes haben")
            now = time.time()
            key = str(uuid.uuid4())
            with outbox_db() as db:
                if (
                    db.execute(
                        "SELECT COUNT(*) FROM messages WHERE status IN ('queued','sending')"
                    ).fetchone()[0]
                    >= 5
                ):
                    raise ValueError("Lokale Funkwarteschlange ist voll")
                db.execute(
                    "INSERT INTO messages VALUES(?,?,?,?,?,?)", (key, channel, text, "queued", now, now)
                )
            return self.reply(202, json.dumps({"id": key, "status": "queued"}).encode(), "application/json")
        except (ValueError, json.JSONDecodeError) as error:
            return self.reply(400, json.dumps({"error": str(error)}).encode(), "application/json")


if __name__ == "__main__":
    print(f"Local dashboard on http://{HOST}:{PORT}")
    threading.Thread(target=monitor_remote, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
