"""Local configuration UI. Never opens a radio or transmits a packet."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import shutil
import sqlite3
import threading
import time
from collections import deque
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from .config import load_config
from .preview import run_preview, validate_request
from .regions import STATES
from .settings import read_settings, refresh_secrets, save_settings


def runtime(config):
    result = {
        "database": False,
        "connected": False,
        "heartbeat": None,
        "packets": {},
        "repeaters": None,
        "asa_last_received": None,
        "warning_sources": {},
    }
    path = Path(config.storage.path)
    if not path.is_file():
        return result
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            meta = dict(db.execute("SELECT key,value FROM meta"))
            result.update(
                database=True,
                packets=dict(db.execute("SELECT state,count(*) FROM packets GROUP BY state")),
                repeaters=db.execute("SELECT count(*) FROM repeaters").fetchone()[0],
                heartbeat=meta.get("bot:heartbeat"),
                asa_last_received=meta.get("asa:last_received"),
            )
            result["connected"] = (
                meta.get("bot:connected") == "1" and time.time() - float(meta.get("bot:heartbeat", 0)) < 120
            )
            result["warning_sources"] = json.loads(meta.get("warnings:status", "{}"))
    except (sqlite3.Error, ValueError, OSError):
        result["error"] = "Status zurzeit nicht lesbar"
    return result


def receiver_status():
    devices = Path("/sys/bus/usb/devices")
    detected = None if not devices.exists() else False
    if devices.exists():
        for item in devices.iterdir():
            try:
                if (item / "idVendor").read_text().strip() == "0bda" and (
                    item / "idProduct"
                ).read_text().strip() in {"2832", "2838"}:
                    detected = True
            except OSError:
                pass
    return {
        "usb_detected": detected,
        "dab_decoder_installed": bool(shutil.which("welle-cli")),
        "asa_decoder_verified": False,
    }


def make_server(path, host=None, port=None):
    path = Path(path).resolve()
    config = load_config(path)
    host, port = host or config.dashboard.host, config.dashboard.port if port is None else port
    token = os.environ.get(config.dashboard.token_env, "")
    if host not in {"127.0.0.1", "localhost"} and len(token) < 24:
        raise ValueError("LAN access requires a dashboard token of at least 24 characters")
    csrf = secrets.token_urlsafe(32)
    lock = threading.Lock()
    preview_lock = threading.Lock()
    online_tests = deque()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_):
            pass

        def respond(self, code, value, content_type="application/json"):
            raw = (
                json.dumps(value, ensure_ascii=False).encode()
                if content_type == "application/json"
                else value
            )
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(raw)

        def allowed(self):
            hostname = urlsplit("http://" + self.headers.get("Host", "")).hostname
            if host in {"127.0.0.1", "localhost"} and hostname not in {"127.0.0.1", "localhost"}:
                self.respond(403, {"error": "Ungültiger Host"})
                return False
            if token and not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.respond(401, {"error": "Zugangscode erforderlich"})
                return False
            return True

        def do_GET(self):
            route = urlsplit(self.path).path
            if route.startswith("/api/"):
                if not self.allowed():
                    return
                try:
                    with lock:
                        current = load_config(path)
                        status = {"runtime": runtime(current), "receiver": receiver_status()}
                        if route == "/api/state":
                            status.update(
                                read_settings(path),
                                csrf=csrf,
                                states=[{"code": k, "name": v[0]} for k, v in STATES.items()],
                            )
                        elif route != "/api/status":
                            self.respond(404, {"error": "Nicht gefunden"})
                            return
                    self.respond(200, status)
                except (ValueError, OSError):
                    self.respond(500, {"error": "Konfiguration nicht lesbar"})
                return
            assets = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/app.css": ("app.css", "text/css; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            }
            if route not in assets:
                self.respond(404, {"error": "Nicht gefunden"})
                return
            name, mime = assets[route]
            self.respond(200, files("meshcore_ai_bot").joinpath("web", name).read_bytes(), mime)

        def do_POST(self):
            if not self.allowed():
                return
            if self.path not in {"/api/settings", "/api/preview"}:
                self.respond(404, {"error": "Nicht gefunden"})
                return
            if self.headers.get("Origin") != "http://" + self.headers.get(
                "Host", ""
            ) or not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), csrf):
                self.respond(403, {"error": "Ungültiger Ursprung oder Sitzung"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if (
                    not 0 < length <= 131072
                    or self.headers.get("Content-Type", "").split(";")[0] != "application/json"
                ):
                    raise ValueError("JSON-Anfrage bis 128 KB erforderlich")
                payload = json.loads(self.rfile.read(length))
                if self.path == "/api/preview":
                    with lock:
                        current = load_config(path)
                        refresh_secrets(current, path)
                        _, _, _, offline = validate_request(payload, current)
                    if not preview_lock.acquire(blocking=False):
                        self.respond(429, {"error": "Ein Texttest läuft bereits. Bitte kurz warten."})
                        return
                    try:
                        if not offline:
                            now = time.time()
                            while online_tests and online_tests[0] < now - 86400:
                                online_tests.popleft()
                            if (
                                len(online_tests) >= current.ai.requests_per_day
                                or sum(t > now - 60 for t in online_tests) >= current.ai.requests_per_minute
                            ):
                                self.respond(
                                    429,
                                    {
                                        "error": "Testlimit erreicht. Offline testen oder später erneut versuchen."
                                    },
                                )
                                return
                            online_tests.append(now)
                        result = run_preview(current, payload)
                    finally:
                        preview_lock.release()
                else:
                    with lock:
                        result = save_settings(path, payload)
                self.respond(200, result)
            except TimeoutError:
                self.respond(504, {"error": "Der Texttest hat zu lange gedauert. Bitte erneut versuchen."})
            except FileExistsError:
                self.respond(409, {"error": "Konfiguration wurde geändert. Bitte neu laden."})
            except (ValueError, TypeError, KeyError, AttributeError):
                self.respond(400, {"error": "Ungültige Einstellungen. Eingaben und Kanalregeln prüfen."})
            except OSError:
                self.respond(500, {"error": "Speichern nicht möglich. Dateirechte prüfen."})

    return ThreadingHTTPServer((host, port), Handler)


def serve(path):
    with make_server(path) as server:
        print(f"MeshCore Dashboard: http://{server.server_address[0]}:{server.server_address[1]}", flush=True)
        server.serve_forever()
