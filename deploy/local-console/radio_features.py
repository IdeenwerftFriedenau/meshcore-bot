"""Bounded local radio recordings and explicit, on-demand song recognition."""

import contextlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path


class RadioFeatures:
    def __init__(self, root, subscribe, recognize=None):
        self.root = Path(root)
        self.subscribe = subscribe
        self.recognize = recognize or self.online_recognize
        self.lock = threading.Lock()
        self.jobs = {}

    def persist(self, job):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        safe = dict(job)
        safe.pop("stop", None)
        path = self.root / (job["id"] + ".json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(safe, ensure_ascii=False))
        temporary.chmod(0o600)
        temporary.replace(path)

    def start(self, kind, sid, seconds=300):
        if kind not in ("record", "identify") or not re.fullmatch(r"0x[0-9a-f]{1,8}", str(sid)):
            raise ValueError("Ungültiger Sender oder Auftrag")
        if kind == "record" and (type(seconds) is not int or seconds not in (60, 300, 900)):
            raise ValueError("Aufnahmedauer: 1, 5 oder 15 Minuten")
        with self.lock:
            active = [
                j for j in self.jobs.values() if j["status"] in ("preparing", "capturing", "recognizing")
            ]
            if len(active) >= 4 or (kind == "identify" and any(j["kind"] == kind for j in active)):
                raise ValueError("Bitte laufende Aufnahme oder Erkennung abwarten")
            if kind == "identify" and any(
                j["kind"] == kind and time.time() - j["created"] < 30 for j in self.jobs.values()
            ):
                raise ValueError("Songerkennung bitte höchstens alle 30 Sekunden starten")
            used = sum(p.stat().st_size for p in self.root.glob("*.mp3"))
            if used + (len(active) + 1) * 32 * 1024 * 1024 > 512 * 1024 * 1024:
                raise ValueError("Aufnahmespeicher voll; bitte ältere Aufnahmen abwarten")
            key = uuid.uuid4().hex
            job = {
                "id": key,
                "kind": kind,
                "sid": sid,
                "seconds": seconds if kind == "record" else 12,
                "created": time.time(),
                "status": "preparing",
                "bytes": 0,
                "stop": threading.Event(),
            }
            self.jobs[key] = job
            self.persist(job)
        threading.Thread(target=self.run, args=(job,), daemon=True).start()
        return {"id": key, "status": "preparing"}

    def stop(self, key):
        with self.lock:
            job = self.jobs.get(key)
            if not job or job["kind"] != "record":
                raise ValueError("Aufnahme nicht gefunden")
            job["stop"].set()
        return {"id": key, "status": "stopping"}

    def list(self):
        records = {}
        for path in self.root.glob("*.json"):
            try:
                item = json.loads(path.read_text())
                if time.time() - item.get("created", 0) > 86400:
                    for suffix in (".json", ".mp3", ".part"):
                        path.with_suffix(suffix).unlink(missing_ok=True)
                    continue
                if (
                    item.get("status") in ("preparing", "capturing", "recognizing")
                    and item.get("id") not in self.jobs
                ):
                    item["status"] = "interrupted"
                records[item["id"]] = item
            except (OSError, ValueError, KeyError):
                continue
        with self.lock:
            for key, item in self.jobs.items():
                snapshot = dict(item)
                snapshot.pop("stop", None)
                records[key] = snapshot
            # Keep a bounded history; local files retain their own 24-hour lifetime.
            for key in list(self.jobs):
                if (
                    self.jobs[key]["status"] not in ("preparing", "capturing", "recognizing")
                    and time.time() - self.jobs[key]["created"] > 3600
                ):
                    del self.jobs[key]
        return sorted(records.values(), key=lambda item: item["created"], reverse=True)[:50]

    def run(self, job):
        listener = None
        temporary = self.root / (job["id"] + ".part")
        try:
            self.list()
            if sum(p.stat().st_size for p in self.root.glob("*.mp3")) > 512 * 1024 * 1024:
                raise ValueError("Aufnahmespeicher voll; ältere Aufnahmen laufen nach 24 Stunden ab")
            listener, info = self.subscribe(job["sid"])
            job.update(info)
            if not listener.stream.ready.wait(8) or not listener.stream.opened:
                raise ValueError("Audio nicht bereit")
            if hasattr(listener.stream, "buffered"):
                listener.stream.buffered.wait(10)
            job["status"] = "capturing"
            self.persist(job)
            deadline = time.monotonic() + job["seconds"]
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
                while time.monotonic() < deadline and not job["stop"].is_set():
                    chunk = listener.read()
                    if not chunk:
                        break
                    output.write(chunk)
                    job["bytes"] += len(chunk)
                    if job["bytes"] >= 32 * 1024 * 1024:
                        break
            listener.close()
            listener = None
            if job["bytes"] < 16000:
                raise ValueError("Zu wenig Audio empfangen")
            if job["kind"] == "record":
                destination = temporary.with_suffix(".mp3")
                temporary.replace(destination)
                job.update(status="ready", file=destination.name)
            else:
                job["status"] = "recognizing"
                self.persist(job)
                result = self.recognize(temporary)
                job.update(status="matched" if result.get("title") else "no_match", result=result)
        except Exception as error:
            job.update(
                status="failed",
                error=str(error)[:250]
                if isinstance(error, ValueError)
                else "Aufnahme oder Online-Erkennung derzeit nicht verfügbar",
            )
        finally:
            if listener is not None:
                listener.close()
            temporary.unlink(missing_ok=True)
            job["finished"] = time.time()
            with contextlib.suppress(OSError):
                self.persist(job)

    @staticmethod
    def online_recognize(path):
        helper = Path(__file__).with_name("recognize_song.py")
        python = "/opt/meshcore-ai-bot/radio-recognition/.venv/bin/python"
        result = subprocess.run(
            [python, str(helper), str(path)], capture_output=True, text=True, timeout=45, check=True
        )
        data = json.loads(result.stdout)
        return {k: str(data.get(k, ""))[:300] for k in ("title", "artist")}
