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
from dab_control import load_settings

ROOT = Path(__file__).resolve().parent
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
DAB = CFG.get("dab", {})
CHANNEL = load_settings()["channel"]
PORT = 7979
WELLE = str((ROOT / str(DAB.get("welle_cli_path", "welle.io/build/welle-cli"))).resolve())
POST_URL = os.environ["DAB_INGEST_URL"]
TOKEN = os.environ["INGEST_TOKEN"]
LOG = logging.getLogger("dab-monitor")


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
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        while not stop.is_set():
            try:
                if proc is None or proc.returncode is not None:
                    if proc is not None:
                        LOG.warning("welle-cli ended; restarting decoder")
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
                    asyncio.create_task(log_welle(proc))
                    LOG.info("DAB receiver started on %s; HTTP access is loopback-only", CHANNEL)
                    await asyncio.sleep(3)
                desired = load_settings()["channel"]
                if desired != CHANNEL:
                    async with session.post(f"http://127.0.0.1:{PORT}/channel", data=desired) as response:
                        if response.status != 200:
                            raise RuntimeError("DAB retune rejected")
                        await response.read()
                    async with session.get(f"http://127.0.0.1:{PORT}/channel") as response:
                        if (await response.text()).strip() != desired:
                            raise RuntimeError("DAB retune not confirmed")
                    CHANNEL = desired
                    was_active = False
                    last_signal = None
                    LOG.info("DAB channel switched to %s", CHANNEL)
                async with session.get(f"http://127.0.0.1:{PORT}/mux.json") as response:
                    if response.status != 200:
                        raise RuntimeError(f"welle-cli mux HTTP {response.status}")
                    mux = await response.json()
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
            try:
                await asyncio.wait_for(stop.wait(), timeout=10)
            except TimeoutError:
                pass
    if proc and proc.returncode is None:
        proc.send_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(proc.wait(), timeout=8)
        except TimeoutError:
            proc.kill()
            await proc.wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())
