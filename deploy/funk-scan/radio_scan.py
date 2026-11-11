#!/usr/bin/env python3
"""Ideenwerft Funk-Suchlauf: kleine Web-Seite fuer Suchlaeufe mit dem RTL-SDR-Stick.

Band-Suchlauf auf Knopfdruck: haelt die DAB-Dienste kurz an (der Stick kann nur
von einem Programm benutzt werden), misst mit rtl_power ausgewaehlte Baender
(UKW, Flugfunk, 2 m, PMR446, 433 MHz, 868 MHz) und startet die Dienste danach in
jedem Fall wieder. Der DAB+-Suchlauf bleibt bei dab_scan.py; beide laufen nie gleichzeitig.

Nur Python-Standardbibliothek. Einstellungen ueber Umgebungsvariablen (siehe CONFIG).
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONFIG = {
    "bind": os.environ.get("RADIO_SCAN_BIND", "127.0.0.1"),
    "port": int(os.environ.get("RADIO_SCAN_PORT", "8093")),
    "state_dir": Path(os.environ.get("RADIO_SCAN_STATE_DIR", "/var/lib/ideenwerft-radio-scan")),
    # Dienste, die den Stick belegen. Werden beim Band-Suchlauf angehalten und danach wieder gestartet.
    "sdr_units": os.environ.get(
        "RADIO_SCAN_SDR_UNITS", "ideenwerft-dab-news.service ideenwerft-dab-monitor.service"
    ).split(),
    "rtl_power": os.environ.get("RADIO_SCAN_RTL_POWER", shutil.which("rtl_power") or "/usr/bin/rtl_power"),
    "rtl_fm": os.environ.get("RADIO_SCAN_RTL_FM", shutil.which("rtl_fm") or "/usr/bin/rtl_fm"),
    "rtl_433": os.environ.get("RADIO_SCAN_RTL_433", shutil.which("rtl_433") or "/usr/bin/rtl_433"),
    "systemctl": os.environ.get("RADIO_SCAN_SYSTEMCTL", "systemctl --no-ask-password").split(),
    # Erlaubtes Fenster in Sekunden nach der vollen Stunde (DAB-Nachrichten xx:59:30–xx:05:50).
    "allow_from": int(os.environ.get("RADIO_SCAN_ALLOW_FROM", "360")),
    "allow_until": int(os.environ.get("RADIO_SCAN_ALLOW_UNTIL", "3300")),
    "dab_scan_state": os.environ.get("RADIO_SCAN_DAB_SCAN_STATE", "/var/lib/meshcore-gateway/dab-scan.json"),
}

# key: (Anzeigename, Start-Hz, Ende-Hz, Bin-Hz, Schwelle dB ueber Rauschen, Hinweis)
BANDS = {
    "cb": ("CB-Funk 27 MHz", 26.96e6, 27.41e6, 5e3, 8.0, "Jedermannfunk, 40 Kanäle im 10-kHz-Raster. Kanal 9 = 27,065 MHz (Notruf)."),
    "ukw": ("UKW-Radio 87,5–108 MHz", 87.5e6, 108.0e6, 25e3, 10.0, "Dauerhafte Spitzen sind Radiosender."),
    "flugfunk": ("Flugfunk 118–137 MHz", 118.0e6, 137.0e6, 12.5e3, 8.0, "Kurze Spitzen sind Sprechfunk (AM), ATIS/VOLMET senden dauerhaft."),
    "sat137": ("Wettersatelliten 137–138 MHz", 137.0e6, 138.0e6, 10e3, 6.0, "Meteor-M sendet Bilddaten (LRPT um 137,9 MHz), nur beim Überflug für einige Minuten."),
    "2m": ("Amateurfunk 2 m 144–146 MHz", 144.0e6, 146.0e6, 12.5e3, 8.0, "Relais und Simplex-Verkehr."),
    "freenet": ("Freenet 149 MHz", 149.01e6, 149.12e6, 6.25e3, 8.0, "Jedermannfunk, 6 Kanäle 149,025–149,1125 MHz."),
    "sonde": ("Wettersonden 400–406 MHz", 400.0e6, 406.0e6, 10e3, 8.0, "Radiosonden vom Wetterballon (z. B. aus Lindenberg), Datenfunk."),
    "70cm": ("Amateurfunk 70 cm 430–440 MHz", 430.0e6, 440.0e6, 12.5e3, 8.0, "Relais und Simplex, teilt sich 433 MHz mit Sensoren."),
    "ism433": ("ISM 433 MHz", 433.05e6, 434.79e6, 10e3, 8.0, "Funk-Thermometer, Wetterstationen, Fernbedienungen, LPD-Funkgeräte."),
    "pmr": ("PMR446 446,0–446,2 MHz", 446.0e6, 446.2e6, 6.25e3, 8.0, "Jedermann-Funkgeraete, Kanal 1 = 446,00625 MHz."),
    "srd868": ("SRD 868 MHz", 863.0e6, 870.0e6, 12.5e3, 8.0, "Sensoren, Alarmanlagen, LoRa/MeshCore."),
}
RASTER_MHZ = {"ukw": 0.1}
DEFAULT_BANDS = ["ukw", "flugfunk", "pmr", "ism433", "srd868"]

# Bekannte Frequenzen fuer Beschriftungen (Hz, Text). Nur Orientierung.
KNOWN_FREQS = [
    (869.618e6, "MeshCore EU (Ideenwerft-Mesh)"),
    (869.525e6, "Meshtastic EU"),
    (868.1e6, "LoRaWAN"), (868.3e6, "LoRaWAN"), (868.5e6, "LoRaWAN"),
    (433.92e6, "433-MHz-Sensoren"),
    (121.5e6, "Flug-Notfrequenz"),
    (145.5e6, "2-m-Anruffrequenz"),
    (27.065e6, "CB Kanal 9 (Notruf)"), (27.185e6, "CB Kanal 19"),
    (137.9e6, "Meteor-M LRPT"),
    (433.5e6, "70-cm-Anruffrequenz"),
]
# Bänder, die nur gemessen, aber nicht abgehört werden dürfen (§ 5 TDDDG).
LISTEN_FORBIDDEN = {"flugfunk"}

SCAN_SECONDS = {"per_band": 14, "integration": 2}

# Funksensoren (rtl_433): je Frequenz so lange lauschen. Viele Sensoren senden nur alle 30–60 s.
SENSOR_FREQS = [("433", "433.92M", []), ("868", "868.3M", ["-s", "1024k"])]
SENSOR_SECONDS = int(os.environ.get("RADIO_SCAN_SENSOR_SECONDS", "75"))
SENSOR_META = {"time", "model", "id", "channel", "mic", "mod", "freq", "freq1", "freq2", "rssi", "snr",
               "noise", "protocol", "subtype", "sequence_num", "message_num", "rc"}
SENSOR_KEEP = 150  # so viele Geraete merken (auch aus frueheren Suchlaeufen)

# Reinhoeren: nur Funk, der fuer die Allgemeinheit, Funkamateure oder einen unbestimmten
# Personenkreis bestimmt ist (§ 5 TDDDG). Flugfunk ist bewusst nicht dabei.
# 433/868 MHz sind Datenfunk: zu hoeren sind nur Pakete als kurzes Rauschen/Zirpen.
# key: (Name, Start-Hz, Ende-Hz, rtl_fm-Modus, Abtastrate Ausgang)
LISTEN_BANDS = {
    "cb": ("CB-Funk", 26.96e6, 27.41e6, "fm", 16000),
    "ukw": ("UKW-Radio", 87.5e6, 108.0e6, "wbfm", 32000),
    "sat137": ("Wettersatelliten", 137.0e6, 138.0e6, "fm", 48000),
    "2m": ("Amateurfunk 2 m", 144.0e6, 146.0e6, "fm", 16000),
    "freenet": ("Freenet", 149.01e6, 149.12e6, "fm", 16000),
    "70cm": ("Amateurfunk 70 cm", 430.0e6, 440.0e6, "fm", 16000),
    "ism433": ("ISM 433 MHz", 433.05e6, 434.79e6, "fm", 24000),
    "pmr": ("PMR446", 446.0e6, 446.2e6, "fm", 16000),
    "srd868": ("SRD 868 MHz", 863.0e6, 870.0e6, "fm", 24000),
}
LISTEN_MAX_SECONDS = int(os.environ.get("RADIO_SCAN_LISTEN_MAX", "600"))


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- rtl_power

def parse_rtl_power(text: str) -> dict[float, list[float]]:
    """rtl_power-CSV -> {Frequenz Hz: [dB je Durchlauf]}."""
    bins: dict[float, list[float]] = {}
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        try:
            low = float(parts[2])
            step = float(parts[4])
            values = [float(v) for v in parts[6:] if v not in ("", "nan", "-nan")]
        except ValueError:
            continue
        for i, db in enumerate(values):
            freq = round(low + i * step, 1)
            bins.setdefault(freq, []).append(db)
    return bins


def known_label(freq: float, tolerance: float = 40e3) -> str:
    for f, label in KNOWN_FREQS:
        if abs(freq - f) <= tolerance:
            return label
    return ""


def analyse_band(bins: dict[float, list[float]], threshold: float, bin_hz: float) -> dict:
    """Rauschboden, Signale (zusammenhaengende Bins ueber Schwelle) und ein kleines Spektrum."""
    if not bins:
        return {"floor": None, "signals": [], "spectrum": []}
    freqs = sorted(bins)
    peak = {f: max(bins[f]) for f in freqs}
    mean = {f: sum(bins[f]) / len(bins[f]) for f in freqs}
    floor = statistics.median(mean.values())
    limit = floor + threshold

    signals = []
    group: list[float] = []

    def close_group() -> None:
        if not group:
            return
        top = max(group, key=lambda f: peak[f])
        # Anteil der Durchlaeufe, in denen das Signal ueber der Schwelle lag
        hits = bins[top]
        duty = sum(1 for v in hits if v > limit) / len(hits)
        signals.append({
            "mhz": round(top / 1e6, 4),
            "db": round(peak[top] - floor, 1),
            "width_khz": round((group[-1] - group[0] + bin_hz) / 1e3, 1),
            "dauer": "dauerhaft" if duty >= 0.8 else ("oft" if duty >= 0.4 else "kurz"),
            "label": known_label(top),
        })
        group.clear()

    gap = bin_hz * 2.5
    for f in freqs:
        if peak[f] > limit:
            if group and f - group[-1] > gap:
                close_group()
            group.append(f)
    close_group()
    signals.sort(key=lambda s: -s["db"])

    # Spektrum auf hoechstens 400 Punkte verdichten (Max-Hold)
    step = max(1, len(freqs) // 400)
    spectrum = []
    for i in range(0, len(freqs), step):
        chunk = freqs[i:i + step]
        spectrum.append([round(chunk[0] / 1e6, 4), round(max(peak[f] for f in chunk), 1)])
    return {"floor": round(floor, 1), "signals": signals[:40], "spectrum": spectrum}


# ---------------------------------------------------------------- Scanner

class Scanner:
    def __init__(self, blocker=None, state_dir: Path | None = None) -> None:
        # blocker(): liefert einen Grund (Text), warum gerade nicht gemessen werden darf, sonst "".
        self.blocker = blocker
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.proc: subprocess.Popen | None = None
        self.status = {"running": False, "kind": None, "step": "", "progress": 0.0,
                       "started": None, "error": None, "log": []}
        self.state_dir = Path(state_dir) if state_dir else CONFIG["state_dir"]
        self.state_file = self.state_dir / "results.json"
        self.results = self._load()
        # Reinhoeren
        self.listen_lock = threading.Lock()
        self.listen_stop_evt = threading.Event()
        self.pending: dict | None = None      # vorbereitete, noch nicht geoeffnete Sitzung
        self.listening: dict | None = None    # laufende Sitzung
        self.last_listen: dict | None = None  # zuletzt geoeffnete Sitzung (fuer Wiederholungsanfragen)
        self.held: list[str] = []             # von uns angehaltene Dienste
        self.held_file = self.state_dir / "held-units.json"
        self._recover()

    # -- Zustand
    def _load(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {"bands": None}

    def _save(self) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.results, ensure_ascii=False, indent=1))
            tmp.replace(self.state_file)
        except OSError as exc:
            self.log(f"Ergebnis nicht gespeichert: {exc}")

    def log(self, msg: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {msg}"
        print(line, flush=True)
        self.status["log"] = (self.status["log"] + [line])[-60:]

    @property
    def running(self) -> bool:
        """True, solange der Stick vom Suchlauf oder vom Reinhoeren belegt ist."""
        return bool(self.status["running"] or self.listening or self.pending or self.held)

    def snapshot(self) -> dict:
        return {"status": dict(self.status), "results": self.results,
                "bands": {k: v[0] for k, v in BANDS.items()}, "default_bands": DEFAULT_BANDS,
                "quiet": quiet_window_reason(),
                "listen": dict(self.listening) if self.listening else None,
                "listen_bands": {k: [v[0], v[1] / 1e6, v[2] / 1e6] for k, v in LISTEN_BANDS.items()},
                "encoder": encoder_name(), "listen_forbidden": sorted(LISTEN_FORBIDDEN),
                "rtl_433": os.access(CONFIG["rtl_433"], os.X_OK), "sensor_seconds": SENSOR_SECONDS * len(SENSOR_FREQS)}

    # -- Start/Stopp
    def start(self, kind: str, bands: list[str] | None, force: bool) -> tuple[bool, str]:
        if kind not in ("bands", "sensors"):
            return False, "Unbekannte Art von Suchlauf."
        if kind == "sensors":
            if not os.access(CONFIG["rtl_433"], os.X_OK):
                return False, "rtl_433 ist auf dem Pi nicht installiert (sudo apt install rtl-433)."
            bands = []
        else:
            bands = [b for b in (bands or DEFAULT_BANDS) if b in BANDS]
            if not bands:
                return False, "Kein Band ausgewaehlt."
        if dab_scan_running():
            return False, "Gerade läuft der DAB-Suchlauf. Bitte danach starten."
        busy = self.blocker() if self.blocker else ""
        if busy and not force:
            return False, busy + " Mit „trotzdem starten“ geht es auch jetzt."
        reason = quiet_window_reason()
        if reason and not force:
            return False, reason + " Mit „trotzdem starten“ geht es auch jetzt."
        with self.lock:
            if self.status["running"]:
                return False, "Es laeuft schon ein Suchlauf."
            if self.listening or self.pending or self.held:
                return False, "Gerade wird reingehört. Bitte erst „Hören beenden“."
            self.cancel.clear()
            self.status.update(running=True, kind=kind, step="Start", progress=0.0,
                               started=now_iso(), error=None, log=[])
        job = self._run_sensors if kind == "sensors" else (lambda: self._run_bands(bands))
        threading.Thread(target=self._wrap, args=(job,), daemon=True).start()
        return True, "Sensor-Suche gestartet." if kind == "sensors" else "Suchlauf gestartet."

    def stop(self) -> None:
        self.cancel.set()
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()

    def _wrap(self, target) -> None:
        try:
            target()
        except Exception as exc:  # noqa: BLE001 - Fehler auf der Seite anzeigen
            self.status["error"] = str(exc)
            self.log(f"Fehler: {exc}")
        finally:
            self.status.update(running=False, step="Fertig" if not self.status["error"] else "Abgebrochen",
                               progress=1.0)

    # -- Baender
    def _units(self, action: str, units: list[str]) -> None:
        if not units:
            return
        cmd = CONFIG["systemctl"] + [action] + units
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)}: {res.stderr.strip() or res.returncode}")

    def _active_units(self) -> list[str]:
        active = []
        for u in CONFIG["sdr_units"]:
            res = subprocess.run(CONFIG["systemctl"] + ["is-active", u], capture_output=True, text=True)
            if res.stdout.strip() == "active":
                active.append(u)
        return active

    def _run_bands(self, bands: list[str]) -> None:
        was_active = self._active_units()
        self.log("Halte an: " + (", ".join(was_active) or "nichts"))
        out = {"time": now_iso(), "complete": False, "bands": {}}
        try:
            self._remember(was_active)
            self._units("stop", was_active)
            time.sleep(2)
            for i, key in enumerate(bands):
                if self.cancel.is_set():
                    self.log("Abgebrochen")
                    break
                name, lo, hi, bin_hz, thr, hint = BANDS[key]
                self.status.update(step=name, progress=i / len(bands))
                self.log(f"Messe {name}")
                text = self._rtl_power(lo, hi, bin_hz)
                res = analyse_band(parse_rtl_power(text), thr, bin_hz)
                if key in RASTER_MHZ:  # z. B. UKW auf 0,1-MHz-Raster runden
                    r = RASTER_MHZ[key]
                    for sig in res["signals"]:
                        sig["mhz"] = round(round(sig["mhz"] / r) * r, 2)
                res.update(name=name, hint=hint)
                out["bands"][key] = res
                self.log(f"{name}: {len(res['signals'])} Signale (Rauschboden {res['floor']} dB)")
            out["complete"] = not self.cancel.is_set()
        finally:
            if was_active:
                try:
                    self._units("start", list(reversed(was_active)))
                    self.log("Wieder gestartet: " + ", ".join(was_active))
                    self._remember([])
                except Exception as exc:  # noqa: BLE001
                    self.status["error"] = f"Dienste nicht wieder gestartet: {exc}"
                    self.log(self.status["error"])
            self.results["bands"] = out
            self._save()

    def _run_sensors(self) -> None:
        was_active = self._active_units()
        self.log("Halte an: " + (", ".join(was_active) or "nichts"))
        found: dict[str, dict] = {}
        complete = False
        try:
            self._remember(was_active)
            self._units("stop", was_active)
            time.sleep(2)
            for i, (label, freq, extra) in enumerate(SENSOR_FREQS):
                if self.cancel.is_set():
                    self.log("Abgebrochen")
                    break
                self.status.update(step=f"Funksensoren {label} MHz", progress=i / len(SENSOR_FREQS))
                self.log(f"Lausche {SENSOR_SECONDS} s auf {label} MHz (rtl_433)")
                n = self._rtl_433(freq, extra, label, found, i)
                self.log(f"{label} MHz: {n} Pakete")
            complete = not self.cancel.is_set()
        finally:
            if was_active:
                try:
                    self._units("start", list(reversed(was_active)))
                    self.log("Wieder gestartet: " + ", ".join(was_active))
                    self._remember([])
                except Exception as exc:  # noqa: BLE001
                    self.status["error"] = f"Dienste nicht wieder gestartet: {exc}"
                    self.log(self.status["error"])
            self.results["sensors"] = merge_sensors(self.results.get("sensors"), found, complete)
            self._save()
            self.log(f"Funksensoren: {len(found)} Geräte in diesem Durchlauf")

    def _rtl_433(self, freq: str, extra: list[str], label: str, found: dict, index: int) -> int:
        cmd = [CONFIG["rtl_433"], "-f", freq, *extra, "-T", str(SENSOR_SECONDS), "-F", "json",
               "-M", "level", "-M", "protocol"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        packets, start = 0, time.time()
        err: list[str] = []
        threading.Thread(target=lambda p=self.proc: err.extend(p.stderr.read().splitlines()[-5:]), daemon=True).start()
        proc = self.proc

        def tick() -> None:
            while proc.poll() is None:
                frac = min(1.0, (time.time() - start) / SENSOR_SECONDS)
                self.status["progress"] = (index + frac) / len(SENSOR_FREQS)
                time.sleep(1)
        threading.Thread(target=tick, daemon=True).start()
        try:
            for line in self.proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("model"):
                    packets += 1
                    add_sensor(found, msg, label)
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        finally:
            rc = self.proc.returncode
            self.proc = None
        if rc not in (0, None) and not self.cancel.is_set() and packets == 0:
            time.sleep(0.2)
            raise RuntimeError(f"rtl_433 brach ab (rc={rc}): {' '.join(err[-3:])}")
        return packets

    def _rtl_power(self, lo: float, hi: float, bin_hz: float) -> str:
        cmd = [CONFIG["rtl_power"], "-f", f"{int(lo)}:{int(hi)}:{int(bin_hz)}",
               "-i", str(SCAN_SECONDS["integration"]), "-e", f"{SCAN_SECONDS['per_band']}s", "-c", "20%"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            stdout, stderr = self.proc.communicate(timeout=SCAN_SECONDS["per_band"] + 40)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            stdout, stderr = self.proc.communicate()
        finally:
            rc = self.proc.returncode
            self.proc = None
        if not stdout.strip():
            tail = " ".join(stderr.strip().splitlines()[-3:])
            raise RuntimeError(f"rtl_power lieferte nichts (rc={rc}): {tail}")
        return stdout

    # -- Absicherung: angehaltene Dienste ueberleben keinen Neustart der Seite
    def _remember(self, units: list[str]) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if units:
                self.held_file.write_text(json.dumps(units))
            else:
                self.held_file.unlink(missing_ok=True)
        except OSError as exc:
            self.log(f"Merkliste nicht gespeichert: {exc}")

    def _recover(self) -> None:
        try:
            units = [u for u in json.loads(self.held_file.read_text()) if u in CONFIG["sdr_units"]]
        except (OSError, ValueError, TypeError):
            return

        def restart() -> None:
            try:
                self._units("start", list(reversed(units)))
                self.log("Nach Neustart wieder gestartet: " + ", ".join(units))
                self._remember([])
            except Exception as exc:  # noqa: BLE001
                self.log(f"Konnte {', '.join(units)} nicht wieder starten: {exc}")

        threading.Thread(target=restart, daemon=True).start()

    # -- Reinhoeren
    def _hold(self) -> None:
        """Dienste anhalten, die den Stick belegen (nur einmal, auch bei Senderwechsel)."""
        active = [u for u in self._active_units() if u not in self.held]
        if active:
            self._remember(self.held + active)
            self._units("stop", active)
            self.held += active
            time.sleep(1.5)

    def _release(self) -> None:
        if not self.held:
            return
        units, self.held = list(reversed(self.held)), []
        try:
            self._units("start", units)
            self.log("Wieder gestartet: " + ", ".join(units))
            self._remember([])
        except Exception as exc:  # noqa: BLE001
            self.status["error"] = f"Dienste nicht wieder gestartet: {exc}"
            self.log(self.status["error"])

    def listen_prepare(self, mhz, force: bool) -> tuple[bool, str, str]:
        try:
            freq = float(mhz) * 1e6
        except (TypeError, ValueError):
            return False, "Ungültige Frequenz.", ""
        hits = [k for k, v in LISTEN_BANDS.items() if v[1] <= freq <= v[2]]
        band = min(hits, key=lambda k: LISTEN_BANDS[k][2] - LISTEN_BANDS[k][1]) if hits else None
        if not band:
            names = ", ".join(f"{v[0]} {v[1] / 1e6:g}–{v[2] / 1e6:g} MHz" for v in LISTEN_BANDS.values())
            return False, f"Reinhören geht nur bei: {names}.", ""
        if self.status["running"]:
            return False, "Gerade läuft der Band-Suchlauf.", ""
        if dab_scan_running():
            return False, "Gerade läuft der DAB-Suchlauf.", ""
        if not self.listening and not self.held:
            busy = self.blocker() if self.blocker else ""
            if busy and not force:
                return False, busy + " Mit „trotzdem starten“ geht es auch jetzt.", ""
        reason = quiet_window_reason()
        if reason and not force:
            return False, reason + " Mit „trotzdem starten“ geht es auch jetzt.", ""
        token = os.urandom(12).hex()
        with self.listen_lock:
            self.pending = {"token": token, "mhz": round(freq / 1e6, 4), "band": band,
                            "name": LISTEN_BANDS[band][0], "created": time.time()}
        self.listen_stop_evt.set()  # laufende Sitzung beenden (Senderwechsel)
        threading.Timer(25, self._expire_pending, args=(token,)).start()
        return True, f"Stelle {freq / 1e6:.3f} MHz ein …", token

    def _expire_pending(self, token: str) -> None:
        with self.listen_lock:
            if not self.pending or self.pending["token"] != token:
                return
            self.pending = None
            idle = not self.listening
        if idle:
            self._release()

    def listen_stop(self) -> None:
        with self.listen_lock:
            self.pending = None
            self.last_listen = None
        self.listen_stop_evt.set()
        if not self.listening:
            self._release()

    def listen_open(self, token: str):
        """Liefert (Content-Type, Generator mit Audio-Bloecken). Generator.close() raeumt auf."""
        with self.listen_lock:
            session = self.pending
            if not session or session["token"] != token:
                # Safari & Co. holen denselben Ton oft mit mehreren Anfragen (Probe + eigentliche
                # Wiedergabe). Gleiche Sitzung erneut oeffnen statt abzulehnen.
                last = self.last_listen
                if not last or last["token"] != token or time.time() > last["until"]:
                    raise ValueError("Sitzung abgelaufen. Bitte erneut auf Anhören drücken.")
                session = {k: last[k] for k in ("token", "mhz", "band", "name", "created")}
                self.pending = session
                self.listen_stop_evt.set()  # vorige Verbindung derselben Sitzung beenden
        # vorige Sitzung abwarten
        for _ in range(100):
            if not self.listening:
                break
            time.sleep(0.1)
        with self.listen_lock:
            if self.listening or self.pending is not session:
                raise ValueError("Es wird schon reingehört.")
            self.pending = None
            self.listen_stop_evt.clear()
            prev = self.last_listen
            same = prev is not None and prev["token"] == session["token"]
            until = prev["until"] if same else time.time() + LISTEN_MAX_SECONDS
            self.last_listen = dict(session, until=until)
            session = dict(session, started=now_iso(), until=until)
            session.pop("token")
            self.listening = session
        try:
            self._hold()
            procs, out, ctype = self._spawn_audio(session)
        except Exception:
            self.listening = None
            self._release()
            raise
        self.log(f"Höre {session['mhz']:.3f} MHz ({session['name']})")
        return ctype, self._audio_chunks(session, procs, out, ctype)

    def _spawn_audio(self, session: dict):
        _, _, _, mode, rate = LISTEN_BANDS[session["band"]]
        freq = str(int(session["mhz"] * 1e6))
        if mode == "wbfm":
            radio = [CONFIG["rtl_fm"], "-M", "wbfm", "-f", freq, "-r", str(rate), "-"]
        else:
            radio = [CONFIG["rtl_fm"], "-M", "fm", "-f", freq, "-s", str(rate), "-r", str(rate), "-E", "dc", "-"]
        rtl = subprocess.Popen(radio, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        procs = [rtl]
        enc = encoder_name()
        if enc == "ffmpeg":
            cmd = [shutil.which("ffmpeg"), "-loglevel", "error", "-f", "s16le", "-ar", str(rate), "-ac", "1",
                   "-i", "pipe:0", "-f", "mp3", "-b:a", "64k", "-ar", "32000" if rate < 32000 else str(rate),
                   "-flush_packets", "1", "pipe:1"]
        elif enc == "lame":
            cmd = [shutil.which("lame"), "--quiet", "-r", "-s", str(rate / 1000), "--bitwidth", "16",
                   "--signed", "--little-endian", "-m", "m", "-b", "64", "-", "-"]
        else:
            return procs, rtl.stdout, "audio/wav"
        coder = subprocess.Popen(cmd, stdin=rtl.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        rtl.stdout.close()  # gehoert jetzt dem Encoder
        procs.append(coder)
        return procs, coder.stdout, "audio/mpeg"

    def _audio_chunks(self, session: dict, procs, out, ctype: str):
        try:
            if ctype == "audio/wav":
                rate = LISTEN_BANDS[session["band"]][4]
                yield wav_header(rate)
            last_check = 0.0
            while not self.listen_stop_evt.is_set():
                chunk = out.read1(4096) if hasattr(out, "read1") else out.read(4096)
                if not chunk:
                    self.log("Empfänger-Ton beendet (rtl_fm).")
                    break
                yield chunk
                now = time.time()
                if now - last_check > 5:
                    last_check = now
                    if now > session["until"]:
                        self.log("Zeitlimit erreicht, Hören beendet.")
                        break
                    if quiet_window_reason(now) and (now % 3600) >= 3540:
                        self.log("DAB-Nachrichten haben Vorrang, Hören beendet.")
                        break
        finally:
            for p in reversed(procs):
                if p.poll() is None:
                    p.terminate()
            for p in procs:
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()
            with self.listen_lock:
                self.listening = None
                follow_up = self.pending is not None
            if not follow_up:
                time.sleep(3)  # Browser fragt oft gleich noch einmal an
                with self.listen_lock:
                    follow_up = self.pending is not None
                if not follow_up:
                    self._release()
            self.log(f"Hören {session['mhz']:.3f} MHz beendet")


def sensor_key(msg: dict) -> str:
    return "|".join(str(msg.get(k, "")) for k in ("model", "id", "channel"))


def add_sensor(found: dict, msg: dict, band: str) -> None:
    if str(msg.get("type", "")).upper() == "TPMS":
        # Reifendrucksensoren vorbeifahrender Autos: nur zaehlen, keine Kennungen speichern.
        msg = {"model": "Reifendrucksensoren vorbeifahrender Autos", "id": "", "channel": "",
               "rssi": msg.get("rssi")}
    key = sensor_key(msg)
    values = {k: v for k, v in msg.items() if k not in SENSOR_META and isinstance(v, (int, float, str, bool))
              and len(str(v)) < 60}
    item = found.setdefault(key, {"key": key, "model": str(msg.get("model")), "id": msg.get("id"),
                                  "channel": msg.get("channel"), "band": band, "count": 0, "rssi": None})
    item["count"] += 1
    item["last"] = now_iso()
    item["values"] = values
    rssi = msg.get("rssi")
    if isinstance(rssi, (int, float)) and (item["rssi"] is None or rssi > item["rssi"]):
        item["rssi"] = round(rssi, 1)


def merge_sensors(prev: dict | None, found: dict, complete: bool) -> dict:
    """Neue Funde vorn; Geraete aus frueheren Suchlaeufen bleiben mit ihrem letzten Stand erhalten."""
    devices = {d["key"]: dict(d, now=False) for d in (prev or {}).get("devices", []) if isinstance(d, dict)}
    for key, d in found.items():
        old = devices.get(key, {})
        devices[key] = dict(d, now=True, first=old.get("first") or d["last"],
                            seen=int(old.get("seen", 0)) + 1)
    ordered = sorted(devices.values(), key=lambda d: (not d["now"], -int(d.get("count", 0)), d.get("model", "")))
    return {"time": now_iso(), "complete": complete, "seconds": SENSOR_SECONDS,
            "found": len(found), "devices": ordered[:SENSOR_KEEP]}


def encoder_name() -> str:
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    if shutil.which("lame"):
        return "lame"
    return "wav"


def wav_header(rate: int) -> bytes:
    import struct
    size = 0x7FFFFFFF
    return (b"RIFF" + struct.pack("<I", size) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", size - 36))


def quiet_window_reason(now: float | None = None) -> str:
    """Gleiche Sperrzeit wie der DAB-Suchlauf: Nachrichten haben xx:59:30–xx:05:50 Vorrang.

    Der Band-Suchlauf dauert gut eine Minute, deshalb ist ab xx:55 Schluss.
    """
    second = (time.time() if now is None else now) % 3600
    if not (CONFIG["allow_from"] <= second < CONFIG["allow_until"]):
        a, u = CONFIG["allow_from"], CONFIG["allow_until"]
        return (f"Zwischen xx:{u // 60:02d} und xx:{a // 60:02d}:{a % 60:02d} haben die DAB-Nachrichten "
                "den Empfänger, deshalb wartet der Suchlauf.")
    return ""


def dab_scan_running() -> bool:
    """Laeuft gerade der vorhandene DAB-Suchlauf (dab_scan.py)?"""
    try:
        state = json.loads(Path(CONFIG["dab_scan_state"]).read_text())
    except (OSError, ValueError):
        return False
    pid = state.get("pid")
    if not state.get("running") or type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


# ---------------------------------------------------------------- Web

SCANNER: Scanner | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "IdeenwerftRadioScan/1"

    def log_message(self, fmt, *args):  # ruhiger Log
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/api/status":
            self._json(SCANNER.snapshot())
        elif path == "/healthz":
            self._json({"ok": True})
        else:
            self._json({"error": "nicht gefunden"}, 404)

    def do_POST(self):
        length = min(int(self.headers.get("Content-Length") or 0), 10000)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            data = {}
        if self.path == "/api/scan":
            ok, msg = SCANNER.start(str(data.get("kind")), data.get("bands"), bool(data.get("force")))
            self._json({"ok": ok, "message": msg}, 200 if ok else 409)
        elif self.path == "/api/cancel":
            SCANNER.stop()
            self._json({"ok": True, "message": "Wird abgebrochen …"})
        else:
            self._json({"error": "nicht gefunden"}, 404)


PAGE = r"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Funk-Suchlauf</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c2330;--mute:#677285;--line:#dde2ea;--acc:#1f6feb;--ok:#1a7f37;--warn:#b35900;--bad:#c62828}
@media (prefers-color-scheme:dark){:root{--bg:#11151c;--card:#1a2029;--fg:#e6e9ef;--mute:#9aa4b5;--line:#2c3440;--acc:#58a6ff;--ok:#3fb950;--warn:#d29922;--bad:#f85149}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,sans-serif}
main{max-width:1000px;margin:0 auto;padding:16px}h1{font-size:22px;margin:4px 0 2px}h2{font-size:17px;margin:0 0 8px}
.sub{color:var(--mute);margin:0 0 16px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:14px}
button{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid var(--acc);background:var(--acc);color:#fff;cursor:pointer}
button.sec{background:transparent;color:var(--acc)}button:disabled{opacity:.45;cursor:default}
.row{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center}label{white-space:nowrap}
.bar{height:8px;background:var(--line);border-radius:4px;overflow:hidden;margin:10px 0 6px}.bar i{display:block;height:100%;background:var(--acc);width:0}
.log{font:12px ui-monospace,monospace;color:var(--mute);max-height:140px;overflow:auto;white-space:pre-wrap}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:5px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mute);font-weight:600}.mute{color:var(--mute)}.msg{margin-top:8px}.err{color:var(--bad)}.warn{color:var(--warn)}
.tbl{overflow-x:auto}svg{width:100%;height:150px;display:block}.pill{font-size:12px;padding:1px 7px;border-radius:9px;border:1px solid var(--line);color:var(--mute)}
</style></head><body><main>
<h1>Funk-Suchlauf</h1>
<p class="sub">RTL-SDR am Pi. Suchläufe starten nur auf Knopfdruck.</p>

<div class="card">
  <h2>Band-Suchlauf</h2>
  <p class="mute">Hält den DAB-Monitor für ca. 15 s je Band an, misst mit rtl_power und startet ihn danach wieder. Das Mesh-Funkgerät ist davon nicht betroffen.</p>
  <div class="row" id="bandBoxes"></div>
  <div class="row" style="margin-top:10px"><button id="bBands">Band-Suchlauf starten</button></div>
</div>

<div class="card" id="statusCard">
  <div class="row"><b id="stStep">Bereit</b><span class="mute" id="stStarted"></span>
    <label><input type="checkbox" id="force"> trotzdem starten (auch kurz vor/nach der vollen Stunde)</label>
    <button class="sec" id="bCancel" disabled>Abbrechen</button></div>
  <div class="bar"><i id="stBar"></i></div>
  <div id="stMsg" class="msg"></div>
  <div class="log" id="stLog"></div>
</div>

<div class="card"><h2>Band-Ergebnis <span class="mute" id="bandTime"></span></h2><div id="bandOut" class="mute">Noch kein Suchlauf.</div></div>
</main>
<script>
const $=id=>document.getElementById(id);let last=null,boxesBuilt=false;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt=t=>t?new Date(t).toLocaleString('de-DE',{dateStyle:'short',timeStyle:'short'}):'';
async function api(p,body){const r=await fetch(p,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});return r.json()}
async function start(kind){const bands=[...document.querySelectorAll('#bandBoxes input:checked')].map(x=>x.value);
  const r=await api('api/scan',{kind,bands,force:$('force').checked});$('stMsg').innerHTML=`<span class="${r.ok?'':'warn'}">${esc(r.message)}</span>`;refresh()}
$('bBands').onclick=()=>start('bands');$('bCancel').onclick=async()=>{await api('api/cancel',{});refresh()};
function chart(sp,floor,signals){if(!sp.length)return'';const W=1000,H=150,x0=sp[0][0],x1=sp[sp.length-1][0];
  const ys=sp.map(p=>p[1]);const lo=Math.min(...ys),hi=Math.max(...ys)+2;const X=f=>(f-x0)/(x1-x0||1)*W,Y=v=>H-18-(v-lo)/(hi-lo||1)*(H-26);
  const path=sp.map((p,i)=>(i?'L':'M')+X(p[0]).toFixed(1)+' '+Y(p[1]).toFixed(1)).join('');
  const marks=signals.slice(0,12).map(s=>`<line x1="${X(s.mhz)}" x2="${X(s.mhz)}" y1="8" y2="${H-18}" stroke="var(--warn)" stroke-dasharray="3 3"/>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Spektrum">
   <line x1="0" x2="${W}" y1="${Y(floor)}" y2="${Y(floor)}" stroke="var(--line)"/>${marks}
   <path d="${path}" fill="none" stroke="var(--acc)" stroke-width="1.5" vector-effect="non-scaling-stroke"/>
   <text x="2" y="${H-3}" fill="var(--mute)" font-size="12">${x0} MHz</text><text x="${W-2}" y="${H-3}" fill="var(--mute)" font-size="12" text-anchor="end">${x1} MHz</text></svg>`}
function render(d){last=d;const s=d.status;
  if(!boxesBuilt){$('bandBoxes').innerHTML=Object.entries(d.bands).map(([k,n])=>`<label><input type="checkbox" value="${k}" ${d.default_bands.includes(k)?'checked':''}> ${esc(n)}</label>`).join('');boxesBuilt=true}
  $('stStep').textContent=s.running?('Läuft: '+s.step):(s.started?(s.error?'Fehler':'Fertig'):'Bereit');
  $('stStarted').textContent=s.started?('gestartet '+fmt(s.started)):'';$('stBar').style.width=Math.round((s.progress||0)*100)+'%';
  $('bBands').disabled=s.running;$('bCancel').disabled=!s.running;
  if(s.error)$('stMsg').innerHTML=`<span class="err">${esc(s.error)}</span>`;else if(!s.running&&d.quiet&&!$('stMsg').textContent)$('stMsg').innerHTML=`<span class="warn">${esc(d.quiet)}</span>`;
  $('stLog').textContent=(s.log||[]).join('\n');$('stLog').scrollTop=1e9;
  const b=d.results.bands;if(b){$('bandTime').textContent=fmt(b.time)+(b.complete?'':' (unvollständig)');$('bandOut').classList.remove('mute');
    $('bandOut').innerHTML=Object.values(b.bands).map(r=>`<h3 style="font-size:15px;margin:14px 0 4px">${esc(r.name)} <span class="pill">${r.signals.length} Signale</span></h3>
     <p class="mute" style="margin:0 0 6px">${esc(r.hint)} Rauschboden ${r.floor} dB.</p>${chart(r.spectrum,r.floor,r.signals)}
     ${r.signals.length?`<div class="tbl"><table><tr><th>MHz</th><th>über Rauschen</th><th>Breite</th><th>Auftreten</th><th>Hinweis</th></tr>${r.signals.slice(0,15).map(x=>`<tr><td>${x.mhz.toFixed(3)}</td><td>${x.db} dB</td><td>${x.width_khz} kHz</td><td>${x.dauer}</td><td>${esc(x.label)}</td></tr>`).join('')}</table></div>`:''}`).join('')||'Keine Bänder gemessen.'}}
async function refresh(){try{render(await api('api/status'))}catch(e){$('stMsg').innerHTML='<span class="err">Seite nicht erreichbar</span>'}}
refresh();setInterval(()=>refresh(),2000);
</script></body></html>
"""


def main() -> None:
    global SCANNER
    SCANNER = Scanner()
    srv = ThreadingHTTPServer((CONFIG["bind"], CONFIG["port"]), Handler)
    print(f"Funk-Suchlauf auf http://{CONFIG['bind']}:{CONFIG['port']}/", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SCANNER.stop()


if __name__ == "__main__":
    sys.exit(main())
