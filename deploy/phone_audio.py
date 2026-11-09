"""Private, on-demand WebRTC bridge to the paired phone's HFP audio nodes.

No recordings, cloud relays, default audio devices, or automatic calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import hmac
import json
import re
import secrets
import time
from fractions import Fraction
from pathlib import Path

import av
from aiohttp import web
from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError

ROOT = Path("/var/lib/meshcore-gateway/phone-remote")
DEVICE = Path("/var/lib/meshcore-ai-bot/phone/device.json")
# Shared with jarvis.py: whoever holds it owns the phone's SCO audio.
LOCK = ROOT / "audio.lock"
HFP = "0000111f-0000-1000-8000-00805f9b34fb"
RATE, SAMPLES = 48000, 960


async def command(*args, deadline=8):
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), deadline)
        if process.returncode or len(output) > 4 * 1024 * 1024:
            raise ValueError("Audioverbindung nicht verfügbar")
        return output
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise


def profile(root=ROOT, device=DEVICE):
    try:
        row = json.loads((root / "audio-device.json").read_text())
        serial = json.loads(device.read_text())["serial"]
        address, path = row["address"].upper(), row["device_path"]
        if not re.fullmatch(r"[0-9A-F]{2}(:[0-9A-F]{2}){5}", address):
            raise ValueError()
        if not re.fullmatch(r"/org/bluez/hci[0-9]+/dev_" + address.replace(":", "_"), path):
            raise ValueError()
        if not hmac.compare_digest(row["serial_hash"], hashlib.sha256(serial.encode()).hexdigest()):
            raise ValueError()
        return row | {"address": address}
    except (OSError, KeyError, TypeError, ValueError):
        raise ValueError("Motorola für Gesprächston zuerst mit dem Pi koppeln") from None


def audio_nodes(objects, address):
    """Select only this phone, never a system microphone or another headset."""
    identifier = address.replace(":", "_").casefold()
    device_ids = set()
    for obj in objects:
        props = obj.get("info", {}).get("props", {})
        if str(props.get("api.bluez5.address", "")).casefold() == address.casefold():
            device_ids.add(str(obj.get("id")))
    found = {}
    for obj in objects:
        props = obj.get("info", {}).get("props", {})
        name = str(props.get("node.name", ""))
        ours = identifier in name.casefold() or str(props.get("device.id", "")) in device_ids
        if not ours or not name.startswith(("bluez_input.", "bluez_output.")):
            continue
        media = props.get("media.class")
        if media in ("Audio/Source", "Audio/Sink"):
            found[media] = name
        elif props.get("api.bluez5.profile") == "headset-audio-gateway":
            # PipeWire 1.0 SCO factories publish streams even if policy overrides media.class.
            if media == "Stream/Output/Audio" and props.get("factory.name") == "api.bluez5.sco.source":
                found["Audio/Source"] = name
            elif media == "Stream/Input/Audio" and props.get("factory.name") == "api.bluez5.sco.sink":
                found["Audio/Sink"] = name
    return found


class Route:
    def __init__(self, root=ROOT, device=DEVICE):
        self.root, self.device = root, device

    async def nodes(self):
        row = profile(self.root, self.device)
        objects = json.loads(await command("/usr/bin/pw-dump"))
        return audio_nodes(objects, row["address"])

    async def connect(self):
        row = profile(self.root, self.device)
        with contextlib.suppress(ValueError):
            await command(
                "/usr/bin/busctl",
                "call",
                "org.bluez",
                row["device_path"],
                "org.bluez.Device1",
                "ConnectProfile",
                "s",
                HFP,
                deadline=10,
            )
        cards = json.loads(await command("/usr/bin/pactl", "--format=json", "list", "cards"))
        identifier = row["address"].replace(":", "_").casefold()
        gateway_found = False
        for card in cards:
            if identifier not in str(card.get("name", "")).casefold():
                continue
            choices = card.get("profiles", {})
            gateway = next(
                (name for name in choices if "audio-gateway" in name or "audio_gateway" in name), None
            )
            gateway_found = bool(gateway)
            if gateway and card.get("active_profile") != gateway:
                await command("/usr/bin/pactl", "set-card-profile", str(card["name"]), gateway)
        if not gateway_found:
            raise ValueError("Freisprechprofil nicht verbunden")
        for _ in range(5):
            nodes = await self.nodes()
            if "Audio/Source" in nodes and "Audio/Sink" in nodes:
                return nodes
            await asyncio.sleep(0.2)
        # Android exposes SCO nodes only while a call/ring is actually using Bluetooth.
        return {}

    async def disconnect(self):
        with contextlib.suppress(ValueError, OSError, asyncio.TimeoutError):
            row = profile(self.root, self.device)
            await command(
                "/usr/bin/busctl",
                "call",
                "org.bluez",
                row["device_path"],
                "org.bluez.Device1",
                "DisconnectProfile",
                "s",
                HFP,
            )


class Capture(AudioStreamTrack):
    def __init__(self, process=None):
        super().__init__()
        self.process, self.queue = None, asyncio.Queue(maxsize=3)
        self.pts, self.started, self.blocks = 0, None, 0
        self.reader = None
        if process:
            self.attach(process)

    def attach(self, process):
        self.detach()
        self.process = process
        self.reader = asyncio.create_task(self.read_pcm(process))

    def detach(self):
        if self.reader:
            self.reader.cancel()
        self.reader, self.process = None, None
        while not self.queue.empty():
            self.queue.get_nowait()

    async def read_pcm(self, process):
        try:
            while True:
                pcm = await process.stdout.readexactly(SAMPLES * 2)
                self.blocks += 1
                if self.queue.full():
                    self.queue.get_nowait()
                self.queue.put_nowait(pcm)
        except (asyncio.IncompleteReadError, asyncio.CancelledError):
            return

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError
        if self.started is None:
            self.started = time.monotonic()
        await asyncio.sleep(max(0, self.started + self.pts / RATE - time.monotonic()))
        pcm = self.queue.get_nowait() if not self.queue.empty() else bytes(SAMPLES * 2)
        frame = av.AudioFrame(format="s16", layout="mono", samples=SAMPLES)
        frame.planes[0].update(pcm)
        frame.sample_rate, frame.pts, frame.time_base = RATE, self.pts, Fraction(1, RATE)
        self.pts += SAMPLES
        return frame

    def stop(self):
        self.detach()
        super().stop()


def pcm_links(objects, own_name, target, mode):
    """Link only named phone/bridge ports; never use a sink monitor or defaults."""
    nodes = {obj.get("info", {}).get("props", {}).get("node.name"): str(obj["id"])
             for obj in objects if obj.get("type", "").endswith(":Node")}
    if own_name not in nodes or target not in nodes:
        return []

    def ports(name, direction):
        found = []
        for obj in objects:
            props = obj.get("info", {}).get("props", {})
            if (obj.get("type", "").endswith(":Port")
                    and str(props.get("node.id")) == nodes[name]
                    and props.get("port.direction") == direction
                    and str(props.get("port.monitor", False)).lower() != "true"):
                found.append((str(props.get("audio.channel", "")), str(obj["id"])))
        return [identifier for _, identifier in sorted(found)]

    if mode == "record":
        outputs, inputs = ports(target, "out"), ports(own_name, "in")
        return [(outputs[0], inputs[0])] if outputs and inputs else []
    outputs, inputs = ports(own_name, "out"), ports(target, "in")
    return [(outputs[0], port) for port in inputs] if outputs and inputs else []


async def pcm_process(mode, target):
    # Start disconnected and explicitly link SCO ports. A remote HFP gateway is
    # represented as streams, which session policy cannot target like devices.
    name = "meshcore-phone-" + mode + "-" + secrets.token_hex(8)
    args = [
        "/usr/bin/pw-cat",
        "--record" if mode == "record" else "--playback",
        "--target", "0",
        "--rate", str(RATE),
        "--channels", "1",
        "--format", "s16",
        "--latency", "40ms",
        "--properties",
        "{ node.name = " + name + " media.role = Communication node.autoconnect = false "
        "node.dont-fallback = true node.dont-reconnect = true }",
        "-",
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if mode == "playback" else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE if mode == "record" else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        for _ in range(30):
            if process.returncode is not None:
                break
            objects = json.loads(await command("/usr/bin/pw-dump"))
            links = pcm_links(objects, name, target, mode)
            if links:
                for output, input_port in links:
                    await command("/usr/bin/pw-link", output, input_port)
                return process
            await asyncio.sleep(0.1)
        raise ValueError("Telefon-Audioports nicht verfügbar")
    except BaseException:
        await terminate_pcm(process)
        raise


async def terminate_pcm(process):
    # Draining pipes is necessary: wait() can hang after exit with buffered PCM.
    if process.returncode is None:
        process.terminate()
    try:
        await asyncio.wait_for(process.communicate(), 2)
    except TimeoutError:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.communicate(), 2)


def claim_audio(path=LOCK):
    """Non-blocking shared lock; a missing lock file means Jarvis is not installed."""
    try:
        handle = open(path, "rb")  # noqa: SIM115 - held for the whole browser session
    except FileNotFoundError:
        return None
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise ValueError("Jarvis führt gerade ein Telefonat") from None
    return handle


def release_audio(handle):
    if handle:
        with contextlib.suppress(OSError):
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


class Bridge:
    def __init__(self, route=None, lock_path=None):
        self.route = route or Route()
        self.current = None
        self.lock = asyncio.Lock()
        self.lock_path = lock_path or Path(getattr(self.route, "root", ROOT)) / "audio.lock"

    async def status(self, owner):
        try:
            profile(self.route.root, self.route.device)
            nodes = await self.route.nodes()
            ready = "Audio/Source" in nodes and "Audio/Sink" in nodes
            ours = self.current if self.current and self.current["owner"] == owner else None
            return {
                "received_samples": ours["capture"].blocks * SAMPLES if ours and ours.get("capture") else 0,
                "sent_samples": ours.get("sent_samples", 0) if ours else 0,
                "paired": True,
                "ready": ready,
                "busy": self.current is not None,
                "owned": bool(self.current and self.current["owner"] == owner),
                "message": "Freisprechprofil bereit"
                if ready
                else "Gekoppelt; Audio wird beim Verbinden vorbereitet",
            }
        except (TimeoutError, ValueError, OSError):
            return {
                "paired": False,
                "ready": False,
                "busy": bool(self.current),
                "owned": False,
                "message": "Motorola per Bluetooth mit dem Pi koppeln",
            }

    async def offer(self, owner, data):
        sdp = data.get("sdp")
        if data.get("type") != "offer" or not isinstance(sdp, str) or not 30 <= len(sdp) <= 60000:
            raise ValueError("Ungültiges Audioangebot")
        media = [line for line in sdp.splitlines() if line.startswith("m=")]
        if len(media) != 1 or not media[0].startswith("m=audio "):
            raise ValueError("Nur eine Gesprächstonspur ist erlaubt")
        async with self.lock:
            if self.current:
                raise ValueError("Gesprächston ist bereits in einem Browser verbunden")
            claim = claim_audio(self.lock_path)
            try:
                await self.route.connect()
            except BaseException:
                await self.route.disconnect()
                release_audio(claim)
                raise
            pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            row = {
                "owner": owner,
                "id": secrets.token_hex(16),
                "pc": pc,
                "processes": [],
                "tasks": [],
                "capture": None,
                "lease": time.monotonic() + 45,
                "claim": claim,
            }
            self.current = row
            try:
                row["capture"] = Capture()
                row["playback"] = None
                pc.addTrack(row["capture"])

                @pc.on("track")
                def track_received(track):
                    if track.kind == "audio":
                        row["tasks"].append(asyncio.create_task(self.microphone(track, row)))

                @pc.on("connectionstatechange")
                async def state_changed():
                    if pc.connectionState in ("failed", "closed"):
                        asyncio.create_task(self.close(row))

                await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
                await pc.setLocalDescription(await pc.createAnswer())
                row["tasks"].append(asyncio.create_task(self.watchdog(row)))
                row["tasks"].append(asyncio.create_task(self.io_loop(row)))
                return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type, "id": row["id"]}
            except BaseException:
                await self.close(row)
                raise

    async def io_loop(self, row):
        try:
            while self.current is row and not row.get("closing"):
                nodes = await self.route.nodes()
                ready = "Audio/Source" in nodes and "Audio/Sink" in nodes
                ended = any(p.returncode is not None for p in row["processes"])
                if row["processes"] and (not ready or ended):
                    row["capture"].detach()
                    row["playback"] = None
                    await self.stop_processes(row)
                if ready and not row["processes"]:
                    record = await pcm_process("record", nodes["Audio/Source"])
                    row["processes"].append(record)
                    playback = await pcm_process("playback", nodes["Audio/Sink"])
                    row["processes"].append(playback)
                    row["capture"].attach(record)
                    row["playback"] = playback
                await asyncio.sleep(1)
        except (TimeoutError, ValueError, OSError):
            asyncio.create_task(self.close(row))

    async def microphone(self, track, row):
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        try:
            while True:
                frame = await track.recv()
                process = row.get("playback")
                if not process or process.returncode is not None:
                    continue
                for audio in resampler.resample(frame):
                    process.stdin.write(bytes(audio.planes[0])[: audio.samples * 2])
                    await asyncio.wait_for(process.stdin.drain(), 3)
                    row["sent_samples"] = row.get("sent_samples", 0) + audio.samples
        except (TimeoutError, MediaStreamError, OSError):
            asyncio.create_task(self.close(row))

    async def watchdog(self, row):
        while self.current is row:
            await asyncio.sleep(5)
            if time.monotonic() > row["lease"]:
                await self.close(row)
                break

    def owned(self, owner, identifier):
        row = self.current
        if not row or row["owner"] != owner or row["id"] != identifier:
            raise ValueError("Diese Audioverbindung gehört nicht zu deiner Sitzung")
        return row

    async def close(self, row=None):
        row = row or self.current
        if not row or self.current is not row:
            return
        # Keep ownership reserved until routing and all child processes are released.
        if row.get("closing"):
            return
        row["closing"] = True
        for task in row["tasks"]:
            if task is not asyncio.current_task():
                task.cancel()
        if row["capture"]:
            row["capture"].stop()
        await row["pc"].close()
        await self.stop_processes(row)
        await self.route.disconnect()
        release_audio(row.get("claim"))
        if self.current is row:
            self.current = None

    async def stop_processes(self, row):
        for process in row["processes"]:
            await terminate_pcm(process)
        row["processes"] = []


def application(key, bridge=None):
    if not re.fullmatch("[a-f0-9]{64}", key):
        raise ValueError("Private audio service key required")
    bridge = bridge or Bridge()

    async def handle(request):
        if not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + key):
            return web.json_response({"error": "Unauthorized"}, status=403)
        owner = request.headers.get("X-Audio-Owner", "")
        if not re.fullmatch("[a-f0-9]{64}", owner):
            return web.json_response({"error": "Invalid owner"}, status=403)
        try:
            if request.method == "GET" and request.path == "/status":
                result = await bridge.status(owner)
            elif request.method == "POST" and request.path in ("/offer", "/heartbeat", "/stop"):
                data = await request.json()
                if not isinstance(data, dict):
                    raise ValueError("JSON-Objekt erforderlich")
                if request.path == "/offer":
                    result = await bridge.offer(owner, data)
                else:
                    row = bridge.owned(owner, data.get("id"))
                    if request.path == "/stop":
                        await bridge.close(row)
                    else:
                        row["lease"] = time.monotonic() + 45
                    result = {"ok": True}
            else:
                return web.json_response({"error": "Not found"}, status=404)
            return web.json_response(result, headers={"Cache-Control": "no-store"})
        except (TimeoutError, ValueError, OSError):
            return web.json_response(
                {"error": "Audioverbindung nicht bereit oder bereits belegt. Bluetooth-Anrufaudio prüfen."},
                status=409,
            )

    async def cleanup(app):
        await bridge.close()

    app = web.Application(client_max_size=64000)
    app.router.add_route("*", "/{path:.*}", handle)
    app.on_cleanup.append(cleanup)
    return app


def main():
    key = (ROOT / "audio.key").read_text().strip()
    # No access log: SDP and microphone data are never logged or recorded.
    web.run_app(application(key), host="127.0.0.1", port=18181, access_log=None, print=None)


if __name__ == "__main__":
    main()
