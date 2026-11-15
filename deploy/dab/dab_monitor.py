#!/usr/bin/env python3
"""Passive DAB+ ASA/Journaline receiver; reports status only, never transmits MeshCore."""

import asyncio
import logging
import os
import signal
import time
from pathlib import Path

import aiohttp
import yaml
from dab_control import effective_channel, load_settings, news_window

ROOT = Path(__file__).resolve().parent
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
DAB = CFG.get("dab", {})
CHANNEL = load_settings()["channel"]
PORT = 7979
WELLE = str((ROOT / str(DAB.get("welle_cli_path", "welle.io/build/welle-cli"))).resolve())
POST_URL = os.environ["DAB_INGEST_URL"]
TOKEN = os.environ["INGEST_TOKEN"]
LOG = logging.getLogger("dab-monitor")
# welle-cli verliert bei abgebrochenen Stream-Verbindungen gelegentlich Dateideskriptoren. Ist das
# Limit erreicht („accept failed: Too many open files“), nimmt es keine Verbindung mehr an und
# bleibt so, bis es neu startet: Aufnahmen und Live-Radio enden dann mit ConnectTimeout.
# Der Monitor startet welle-cli in diesem Fall selbst neu.
FD_EXHAUSTED_LIMIT = 5        # so viele „Too many open files“ in Folge
FAILED_CYCLES_LIMIT = 6       # oder so viele fehlgeschlagene Abfragen in Folge (~70 s)
HEALTH = {"fd_errors": 0}
CHANNEL_RECHECK = 30          # Sekunden; dazwischen gilt der zuletzt bestätigte Kanal

FD_PREEMPTIVE = 800           # vorsorglich neu starten, bevor das Limit (meist 1024) erreicht ist


def open_fds(pid):
    try:
        return len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        return None


def preemptive_restart(fds, now=None):
    """Vorsorglicher Neustart nur außerhalb der Nachrichtenfenster (HH:59:30 bis HH:05:50)."""
    return fds is not None and fds >= FD_PREEMPTIVE and not news_window(now)


def snapshot(mux, journaline, last_signal):
    asa = mux.get("asa", {}) if isinstance(mux, dict) else {}
    demod = mux.get("demodulator", {}) if isinstance(mux, dict) else {}
    receiver = mux.get("receiver", {}).get("hardware", {}) if isinstance(mux, dict) else {}
    ensemble = mux.get("ensemble", {}).get("label", {}) if isinstance(mux, dict) else {}
    objects = journaline.get("objects", []) if isinstance(journaline, dict) else []
    safe_objects = []
    for item in objects[:5]:
        if isinstance(item, dict):
            try:
                object_id = int(item.get("object_id", -1))
                object_type = int(item.get("type", 0))
                received = float(item.get("received", 0))
            except (TypeError, ValueError):
                continue
            safe_objects.append(
                {
                    "object_id": object_id,
                    "type": object_type,
                    "received": received,
                    "title": str(item.get("title", ""))[:180],
                    "body": str(item.get("body", ""))[:1600],
                }
            )
    snr = demod.get("snr")
    try:
        snr = float(snr) if snr is not None else None
    except (TypeError, ValueError):
        snr = None
    ensemble_name = str(ensemble.get("label", ""))[:100]
    ensemble_locked = bool(ensemble_name)
    signal_state = (
        "lost" if snr is None or snr < 5.0 or not ensemble_locked else ("weak" if snr < 10.0 else "ok")
    )
    payload = {
        "online": signal_state in ("ok", "weak"),
        "ensemble_locked": ensemble_locked,
        "monitor_alive": True,
        "signal_state": signal_state,
        "channel": CHANNEL,
        "snr": snr,
        "ensemble": ensemble_name,
        "receiver": str(receiver.get("name", ""))[:120],
        "ews_ensemble": asa.get("ews_ensemble") is True,
        "asa_active": asa.get("active") is True,
        "asa_is_test": asa.get("is_test") is True,
        "asa_iid": int(asa.get("iid", 0) or 0),
        "asa_level": int(asa.get("level", 0) or 0),
        "asa_status": str(asa.get("status", ""))[:16],
        "region_zone": int(asa.get("region_zone", 0) or 0),
        "region_cc": int(asa.get("region_cc", 0) or 0),
        "region_num_digits": int(asa.get("region_num_digits", 0) or 0),
        "journaline_present": journaline.get("present") is True if isinstance(journaline, dict) else False,
        "journaline_type": str(journaline.get("service_type", ""))[:32]
        if isinstance(journaline, dict)
        else "",
        "journaline_objects": safe_objects,
        "last_signal": last_signal,
    }
    return payload


async def log_welle(proc):
    if proc.stdout is None:
        return
    while line := await proc.stdout.readline():
        text = line.decode("utf-8", "replace").strip()
        if "too many open files" in text.lower():
            HEALTH["fd_errors"] += 1
            if HEALTH["fd_errors"] in (1, FD_EXHAUSTED_LIMIT):
                LOG.info("welle: %s", text[:320])
            continue
        if any(
            token in text.lower()
            for token in (
                "rtl_sdr",
                "tuner",
                "found sync",
                "permission",
                "no device",
                "error",
                "failed",
                "fig 0/15",
            )
        ):
            LOG.info("welle: %s", text[:320])


def welle_stuck(fd_errors, failed_cycles):
    return fd_errors >= FD_EXHAUSTED_LIMIT or failed_cycles >= FAILED_CYCLES_LIMIT


async def stop_welle(proc):
    if proc is None or proc.returncode is not None:
        return
    proc.send_signal(signal.SIGINT)
    try:
        await asyncio.wait_for(proc.wait(), timeout=8)
    except TimeoutError:
        proc.kill()
        await proc.wait()


async def follow_tuning(stop):
    """Local tuning remains responsive even while an Internet upload times out."""
    global CHANNEL
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
        checked = 0.0
        while not stop.is_set():
            try:
                desired = effective_channel(load_settings())
                # Nicht jede Sekunde bei welle-cli nachfragen: Jede Anfrage kann dort eine
                # Verbindung offen lassen. Nachgefragt wird bei einem Wechsel und alle 30 s.
                if desired == CHANNEL and time.monotonic() - checked < CHANNEL_RECHECK:
                    current = desired
                else:
                    async with session.get(f"http://127.0.0.1:{PORT}/channel") as response:
                        current = (await response.text()).strip()
                    checked = time.monotonic()
                if current != desired:
                    async with session.post(f"http://127.0.0.1:{PORT}/channel", data=desired) as response:
                        response.raise_for_status()
                        await response.read()
                    async with session.get(f"http://127.0.0.1:{PORT}/channel") as response:
                        if (await response.text()).strip() != desired:
                            raise RuntimeError("DAB retune not confirmed")
                    LOG.info("DAB channel switched to %s", desired)
                CHANNEL = desired
            except (OSError, ValueError, RuntimeError, aiohttp.ClientError):
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except TimeoutError:
                pass


async def main():
    global CHANNEL
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    headers = {"Authorization": "Bearer " + TOKEN, "Accept": "application/json"}
    timeout = aiohttp.ClientTimeout(total=6, connect=2)
    proc = None
    last_signal = None
    was_active = False
    failed_cycles = 0
    tuning = asyncio.create_task(follow_tuning(stop))
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        while not stop.is_set():
            local_ok = False
            try:
                if proc is None or proc.returncode is not None:
                    if proc is not None:
                        LOG.warning("welle-cli ended; restarting decoder")
                    CHANNEL = effective_channel(load_settings())
                    proc = await asyncio.create_subprocess_exec(
                        WELLE,
                        "-c",
                        CHANNEL,
                        "-C",
                        "1",
                        "-w",
                        str(PORT),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    HEALTH["fd_errors"] = 0
                    failed_cycles = 0
                    asyncio.create_task(log_welle(proc))
                    LOG.info("DAB receiver started on %s; HTTP access is loopback-only", CHANNEL)
                    await asyncio.sleep(3)
                async with session.get(f"http://127.0.0.1:{PORT}/mux.json") as response:
                    if response.status != 200:
                        raise RuntimeError(f"welle-cli mux HTTP {response.status}")
                    mux = await response.json()
                # Nur der lokale Empfänger zählt; ein Internet-Ausfall beim Hochladen ist kein Hänger.
                local_ok = True
                failed_cycles = 0
                HEALTH["fd_errors"] = 0
                journaline = {}
                try:
                    async with session.get(f"http://127.0.0.1:{PORT}/journaline.json") as response:
                        if response.status == 200:
                            journaline = await response.json()
                except (TimeoutError, aiohttp.ClientError):
                    pass
                asa = mux.get("asa", {})
                active = asa.get("active") is True
                if active:
                    if not was_active:
                        last_signal = {
                            "started_at": time.time(),
                            "ended_at": None,
                            "iid": int(asa.get("iid", 0) or 0),
                            "is_test": asa.get("is_test") is True,
                            "level": int(asa.get("level", 0) or 0),
                            "region_zone": int(asa.get("region_zone", 0) or 0),
                            "region_cc": int(asa.get("region_cc", 0) or 0),
                            "region_num_digits": int(asa.get("region_num_digits", 0) or 0),
                            "status": str(asa.get("status", ""))[:16],
                        }
                    elif last_signal:
                        last_signal.update(
                            {"iid": int(asa.get("iid", 0) or 0), "level": int(asa.get("level", 0) or 0)}
                        )
                elif was_active and last_signal:
                    last_signal["ended_at"] = time.time()
                was_active = active
                payload = snapshot(mux, journaline, last_signal)
                async with session.post(POST_URL, json=payload) as response:
                    if response.status != 200:
                        LOG.warning("Status upload rejected: HTTP %s", response.status)
                    else:
                        LOG.info(
                            "DAB status uploaded: state=%s SNR=%s EWS=%s ASA active=%s Journaline objects=%d",
                            payload["signal_state"],
                            payload["snr"],
                            payload["ews_ensemble"],
                            payload["asa_active"],
                            len(payload["journaline_objects"]),
                        )
                news_items = [item for item in payload["journaline_objects"] if item["type"] == 2]
                if journaline.get("service_type") == "journaline" and news_items:
                    journaline_url = POST_URL.rsplit("/", 1)[0] + "/journaline"
                    async with session.post(
                        journaline_url,
                        json={
                            "service_type": "journaline",
                            "ensemble": payload["ensemble"],
                            "items": news_items,
                        },
                    ) as response:
                        if response.status != 200:
                            LOG.warning("Journaline forwarding rejected: HTTP %s", response.status)
                        else:
                            result = await response.json()
                            if result.get("queued"):
                                LOG.info("Journaline news forwarded to #news: %d item(s)", result["queued"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("DAB monitor cycle failed: %s", type(exc).__name__)
                if not local_ok:
                    failed_cycles += 1
            if proc and proc.returncode is None:
                fds = open_fds(proc.pid)
                if welle_stuck(HEALTH["fd_errors"], failed_cycles):
                    LOG.warning(
                        "welle-cli unresponsive (open-file errors=%d, failed cycles=%d, fds=%s); restarting decoder",
                        HEALTH["fd_errors"], failed_cycles, fds,
                    )
                    await stop_welle(proc)
                elif preemptive_restart(fds):
                    LOG.warning("welle-cli holds %d open files (leaked connections); restarting decoder", fds)
                    await stop_welle(proc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=10)
            except TimeoutError:
                pass
    await tuning
    await stop_welle(proc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())
