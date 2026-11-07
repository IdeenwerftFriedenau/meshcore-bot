"""Jarvis: answer calls from approved numbers on the Motorola and talk with the AI.

The phone stays the telephone. The Pi joins the call as Bluetooth hands-free
(HFP), hears the caller over SCO, recognises speech with the local Vosk model,
asks the configured OpenAI-compatible AI and speaks the answer with Piper.

Privacy: no recordings, no transcripts on disk, no numbers or texts in logs.
Calls from numbers that are not approved are never answered.
Only standard library at import time; Vosk is imported lazily so this file
runs with the existing DAB/Vosk virtual environment on the Pi.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

LOG = logging.getLogger("jarvis")
DEVICE = Path("/var/lib/meshcore-ai-bot/phone/device.json")
AUDIO_ROOT = Path("/var/lib/meshcore-gateway/phone-remote")
# Shared with phone_audio.py so the browser bridge and Jarvis never own SCO together.
LOCK = AUDIO_ROOT / "audio.lock"
HFP = "0000111f-0000-1000-8000-00805f9b34fb"
ADB = ["/usr/bin/adb", "-H", "127.0.0.1", "-P", "18137"]
LISTEN_RATE = 16000
CHUNK = LISTEN_RATE // 4 * 2  # 250 ms of s16 mono
MAX_CALL_SECONDS = 15 * 60
SILENCE_SECONDS = 60
MAX_HISTORY = 10
BYE = re.compile(r"\b(tschüss|tschüs|auf wiederhören|auf wiedersehen|leg auf|auflegen|ende|beenden)\b")

SYSTEM_PROMPT = (
    "Du bist Jarvis, ein freundlicher Sprachassistent am Telefon der Ideenwerft Friedenau. "
    "Antworte immer auf Deutsch, kurz und gesprochen: höchstens drei Sätze, keine Listen, "
    "keine Aufzählungszeichen, keine Links, kein Markdown, keine Emojis. "
    "Die Fragen kommen aus einer Spracherkennung und können Hörfehler enthalten; "
    "wenn etwas unklar ist, frag kurz nach. Du hast keinen Zugriff auf aktuelle Nachrichten, "
    "außer die KI liefert sie selbst; erfinde keine Fakten."
)
GREETING = "Hallo, hier ist Jarvis. Was kann ich für dich tun?"
GOODBYE = "Tschüss, bis bald."
TIMEOUT_TEXT = "Ich höre nichts mehr und lege jetzt auf. Tschüss."
AI_ERROR = "Entschuldige, die KI ist gerade nicht erreichbar. Versuch es bitte später noch einmal."


def normalize_number(value):
    """German numbers: '+49 152 …', '0049152…' and '0152…' compare equal."""
    digits = re.sub(r"[^0-9+]", "", str(value or ""))
    if digits.startswith("+49"):
        digits = "0" + digits[3:]
    elif digits.startswith("0049"):
        digits = "0" + digits[4:]
    elif digits.startswith("+"):
        digits = "00" + digits[1:]
    return digits if re.fullmatch(r"0[0-9]{5,20}", digits) else ""


def allowed_numbers(raw):
    numbers = {normalize_number(part) for part in str(raw or "").split(",")}
    numbers.discard("")
    return frozenset(numbers)


def parse_call_state(raw):
    """Same reading as the dashboard: ringing wins, number only if unambiguous."""
    states = re.findall(r"\bmCallState\s*=\s*([012])\b", raw)
    state = "ringing" if "1" in states else "active" if "2" in states else "idle" if states else "unknown"
    numbers = set(re.findall(r"\bmCallIncomingNumber\s*=\s*(\+?[0-9]{1,30})\s*$", raw, re.M))
    number = next(iter(numbers)) if state == "ringing" and len(numbers) == 1 else None
    return state, number


def speakable(text, limit=600):
    """Strip what a voice should not read aloud."""
    text = re.sub(r"https?://\S+", "", str(text or ""))
    text = re.sub(r"[*_#`>|~\[\]()]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        cut = text[:limit]
        end = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
        text = cut[: end + 1] if end > limit // 2 else cut
    return text


def wants_goodbye(text):
    return bool(BYE.search(text.casefold()))


def audio_nodes(objects, address):
    """SCO nodes of exactly this phone (mirrors phone_audio.audio_nodes)."""
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
            if media == "Stream/Output/Audio" and props.get("factory.name") == "api.bluez5.sco.source":
                found["Audio/Source"] = name
            elif media == "Stream/Input/Audio" and props.get("factory.name") == "api.bluez5.sco.sink":
                found["Audio/Sink"] = name
    return found


def pcm_links(objects, own_name, target, mode):
    """Link only named phone/Jarvis ports; never a sink monitor or a default device."""
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


async def command(*args, stdin=None, deadline=10):
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(stdin), deadline)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode or len(output) > 8 * 1024 * 1024:
        raise ValueError("Befehl fehlgeschlagen: " + os.path.basename(args[0]))
    return output


async def terminate(process):
    if process is None:
        return
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
    try:
        await asyncio.wait_for(process.communicate(), 2)
    except (TimeoutError, ValueError):
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        with contextlib.suppress(TimeoutError, ValueError):
            await asyncio.wait_for(process.wait(), 2)


class AudioLock:
    """Non-blocking flock shared with the browser audio bridge."""

    def __init__(self, path=LOCK):
        self.path, self.handle = Path(path), None

    def acquire(self):
        try:
            handle = open(self.path, "a+b")  # noqa: SIM115 - held for the whole call
        except OSError:
            LOG.warning("Audio lock file not available")
            return False
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self):
        if self.handle:
            with contextlib.suppress(OSError):
                fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


class Phone:
    """ADB access to the configured phone only."""

    def __init__(self, device=DEVICE, run=command):
        self.device, self.run = Path(device), run

    def serial(self):
        try:
            serial = json.loads(self.device.read_text())["serial"]
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError("Motorola-Verbindung noch nicht eingerichtet") from None
        if not isinstance(serial, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", serial):
            raise ValueError("Ungültige Gerätekonfiguration")
        return serial

    async def shell(self, script, deadline=10):
        return await self.run(*ADB, "-s", self.serial(), "shell", "-T", "sh",
                              stdin=script.encode(), deadline=deadline)

    async def call_state(self):
        raw = await self.shell("dumpsys telephony.registry\n")
        return parse_call_state(raw.decode(errors="replace"))

    async def answer(self):
        # KEYCODE_CALL answers a ringing call; HEADSETHOOK is the fallback some builds need.
        await self.shell("input keyevent 5\n")
        for _ in range(8):
            await asyncio.sleep(0.5)
            if (await self.call_state())[0] == "active":
                return True
        await self.shell("input keyevent 79\n")
        for _ in range(8):
            await asyncio.sleep(0.5)
            if (await self.call_state())[0] == "active":
                return True
        return False

    async def hang_up(self):
        if (await self.call_state())[0] in ("active", "ringing"):
            await self.shell("input keyevent 6\n")


class Route:
    """Bluetooth HFP to the paired phone (same profile file as phone_audio.py)."""

    def __init__(self, root=AUDIO_ROOT, run=command):
        self.root, self.run = Path(root), run

    def address(self):
        try:
            row = json.loads((self.root / "audio-device.json").read_text())
            address, path = row["address"].upper(), row["device_path"]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise ValueError("Motorola für Gesprächston zuerst mit dem Pi koppeln") from None
        if not re.fullmatch(r"[0-9A-F]{2}(:[0-9A-F]{2}){5}", address):
            raise ValueError("Ungültige Bluetooth-Adresse")
        if not re.fullmatch(r"/org/bluez/hci[0-9]+/dev_" + address.replace(":", "_"), path):
            raise ValueError("Ungültiger Bluetooth-Pfad")
        return address, path

    async def nodes(self):
        address, _ = self.address()
        return audio_nodes(json.loads(await self.run("/usr/bin/pw-dump")), address)

    async def connect(self):
        address, path = self.address()
        with contextlib.suppress(ValueError, TimeoutError):
            await self.run("/usr/bin/busctl", "call", "org.bluez", path, "org.bluez.Device1",
                           "ConnectProfile", "s", HFP, deadline=10)
        cards = json.loads(await self.run("/usr/bin/pactl", "--format=json", "list", "cards"))
        identifier = address.replace(":", "_").casefold()
        for card in cards:
            if identifier not in str(card.get("name", "")).casefold():
                continue
            gateway = next((name for name in card.get("profiles", {})
                            if "audio-gateway" in name or "audio_gateway" in name), None)
            if gateway and card.get("active_profile") != gateway:
                await self.run("/usr/bin/pactl", "set-card-profile", str(card["name"]), gateway)

    async def disconnect(self):
        with contextlib.suppress(ValueError, OSError, TimeoutError):
            _, path = self.address()
            await self.run("/usr/bin/busctl", "call", "org.bluez", path, "org.bluez.Device1",
                           "DisconnectProfile", "s", HFP, deadline=10)

    async def wait_nodes(self, seconds=10):
        for _ in range(int(seconds * 4)):
            nodes = await self.nodes()
            if "Audio/Source" in nodes and "Audio/Sink" in nodes:
                return nodes
            await asyncio.sleep(0.25)
        raise ValueError("Bluetooth-Gesprächston nicht verfügbar")


async def pcm_process(mode, target, rate):
    name = "jarvis-" + mode + "-" + secrets.token_hex(8)
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/pw-cat", "--record" if mode == "record" else "--playback",
        "--target", "0", "--rate", str(rate), "--channels", "1", "--format", "s16",
        "--latency", "40ms", "--properties",
        "{ node.name = " + name + " media.role = Communication node.autoconnect = false "
        "node.dont-fallback = true node.dont-reconnect = true }",
        "-",
        stdin=asyncio.subprocess.PIPE if mode == "playback" else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE if mode == "record" else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        for _ in range(30):
            if process.returncode is not None:
                break
            links = pcm_links(json.loads(await command("/usr/bin/pw-dump")), name, target, mode)
            if links:
                for output, input_port in links:
                    await command("/usr/bin/pw-link", output, input_port)
                return process
            await asyncio.sleep(0.1)
        raise ValueError("Telefon-Audioports nicht verfügbar")
    except BaseException:
        await terminate(process)
        raise


class Brain:
    """OpenAI-compatible chat completion via the configured gateway."""

    def __init__(self, base_url, model, api_key, path="/chat/completions", timeout=30):
        parts = urlsplit(base_url)
        if parts.scheme != "https" and parts.hostname not in ("127.0.0.1", "::1"):
            raise ValueError("JARVIS_AI_BASE_URL muss https verwenden")
        if not model or not api_key:
            raise ValueError("JARVIS_AI_MODEL und JARVIS_AI_API_KEY sind erforderlich")
        # Some gateways (e.g. the Ideenwerft one) take requests at base_url itself: path "".
        self.url = base_url.rstrip("/") + path
        self.model, self.key, self.timeout = model, api_key, timeout
        self.opener = build_opener(ProxyHandler({}))

    def ask_sync(self, history):
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *history[-MAX_HISTORY:]],
            "max_tokens": 300,
            "temperature": 0.4,
        }).encode()
        request = Request(self.url, data=body, method="POST", headers={
            "Authorization": "Bearer " + self.key, "Content-Type": "application/json",
            "Accept": "application/json"})
        with self.opener.open(request, timeout=self.timeout) as response:
            data = json.loads(response.read(200000))
        return str(data["choices"][0]["message"]["content"] or "")

    async def ask(self, history):
        return await asyncio.to_thread(self.ask_sync, history)


class Voice:
    """Piper text to speech, raw s16 mono at the voice's sample rate."""

    def __init__(self, binary, model):
        self.binary, self.model = str(binary), str(model)
        config = json.loads(Path(self.model + ".json").read_text())
        self.rate = int(config["audio"]["sample_rate"])

    async def synthesize(self, text):
        return await command(self.binary, "--model", self.model, "--output_raw", "--quiet",
                             stdin=(text + "\n").encode(), deadline=60)


class Ears:
    """Streaming Vosk recognition."""

    def __init__(self, model_path):
        from vosk import KaldiRecognizer, Model, SetLogLevel

        SetLogLevel(-1)
        self.model = Model(str(model_path))
        self.factory = KaldiRecognizer

    def recognizer(self):
        return self.factory(self.model, LISTEN_RATE)


class Call:
    """One answered call: listen, think, speak until goodbye, silence or hang-up."""

    def __init__(self, phone, nodes, ears, brain, voice, clock=time.monotonic):
        self.phone, self.nodes, self.ears, self.brain, self.voice = phone, nodes, ears, brain, voice
        self.clock = clock
        self.history = []
        self.record = self.playback = self.reader = None
        self.heard = asyncio.Queue(maxsize=240)  # at most one minute of 250 ms chunks
        self.muted = False

    async def read_audio(self):
        try:
            while chunk := await self.record.stdout.read(CHUNK):
                # Half duplex: drop what arrives while Jarvis speaks (echo and talk-over).
                if self.muted:
                    continue
                if self.heard.full():
                    self.heard.get_nowait()
                self.heard.put_nowait(chunk)
        finally:
            if self.heard.full():
                self.heard.get_nowait()
            self.heard.put_nowait(b"")

    async def say(self, text):
        text = speakable(text)
        if not text:
            return
        pcm = await self.voice.synthesize(text)
        self.muted = True
        try:
            self.playback.stdin.write(pcm)
            await asyncio.wait_for(self.playback.stdin.drain(), 30)
            # pw-cat buffers; wait until the caller has actually heard it.
            await asyncio.sleep(len(pcm) / 2 / self.voice.rate + 0.3)
        finally:
            while not self.heard.empty():
                if self.heard.get_nowait() == b"":
                    self.heard.put_nowait(b"")
                    break
            self.muted = False

    async def hear(self, deadline):
        recognizer = self.ears.recognizer()
        while self.clock() < deadline:
            try:
                chunk = await asyncio.wait_for(self.heard.get(), 2)
            except TimeoutError:
                continue
            if not chunk:
                raise ConnectionError("Gesprächston beendet")
            if await asyncio.to_thread(recognizer.AcceptWaveform, chunk):
                text = json.loads(recognizer.Result()).get("text", "").strip()
                if text:
                    return text
        text = json.loads(recognizer.FinalResult()).get("text", "").strip()
        return text or None

    async def still_connected(self):
        with contextlib.suppress(ValueError, OSError, TimeoutError):
            return (await self.phone.call_state())[0] == "active"
        return False

    async def run(self):
        try:
            self.record = await pcm_process("record", self.nodes["Audio/Source"], LISTEN_RATE)
            self.playback = await pcm_process("playback", self.nodes["Audio/Sink"], self.voice.rate)
            self.reader = asyncio.create_task(self.read_audio())
            return await self.converse()
        finally:
            if self.reader:
                self.reader.cancel()
            await terminate(self.record)
            await terminate(self.playback)

    async def converse(self):
        await self.say(GREETING)
        ends = self.clock() + MAX_CALL_SECONDS
        while self.clock() < ends:
            text = await self.hear(min(ends, self.clock() + SILENCE_SECONDS))
            if not await self.still_connected():
                return "remote"
            if text is None:
                await self.say(TIMEOUT_TEXT)
                return "silence"
            if wants_goodbye(text):
                await self.say(GOODBYE)
                return "goodbye"
            LOG.info("Frage erkannt (%d Zeichen)", len(text))
            self.history.append({"role": "user", "content": text})
            try:
                answer = speakable(await self.brain.ask(self.history))
            except Exception:  # noqa: BLE001 - any AI failure is spoken, never fatal
                LOG.warning("KI nicht erreichbar")
                self.history.pop()
                await self.say(AI_ERROR)
                continue
            self.history.append({"role": "assistant", "content": answer})
            await self.say(answer or "Dazu habe ich leider keine Antwort.")
        await self.say(GOODBYE)
        return "limit"


class Jarvis:
    def __init__(self, allowed, phone, route, lock, call_factory, poll=1.0):
        if not allowed:
            raise ValueError("JARVIS_ALLOWED_CALLERS ist leer; Jarvis nimmt keine Anrufe an")
        self.allowed, self.phone, self.route, self.lock = allowed, phone, route, lock
        self.call_factory, self.poll = call_factory, poll
        self.ignored = None

    async def step(self):
        state, number = await self.phone.call_state()
        if state != "ringing":
            self.ignored = None
            return None
        if normalize_number(number) not in self.allowed:
            if self.ignored != number:
                LOG.info("Anruf von nicht freigegebener Nummer ignoriert")
                self.ignored = number
            return "ignored"
        if not self.lock.acquire():
            LOG.info("Gesprächston ist im Browser belegt; Anruf nicht angenommen")
            return "busy"
        try:
            await self.route.connect()
            if not await self.phone.answer():
                LOG.warning("Anruf ließ sich nicht annehmen")
                return "unanswered"
            nodes = await self.route.wait_nodes()
            LOG.info("Anruf angenommen")
            reason = await self.call_factory(nodes).run()
            LOG.info("Gespräch beendet (%s)", reason)
            return reason
        except (ValueError, OSError, TimeoutError, ConnectionError) as error:
            LOG.warning("Gespräch abgebrochen: %s", error)
            return "error"
        finally:
            with contextlib.suppress(ValueError, OSError, TimeoutError):
                await self.phone.hang_up()
            await self.route.disconnect()
            self.lock.release()

    async def serve(self):
        LOG.info("Jarvis wartet auf Anrufe von %d freigegebenen Nummern", len(self.allowed))
        while True:
            try:
                await self.step()
            except (ValueError, OSError, TimeoutError) as error:
                LOG.warning("Anrufstatus nicht verfügbar: %s", error)
                await asyncio.sleep(10)
            await asyncio.sleep(self.poll)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    env = os.environ
    allowed = allowed_numbers(env.get("JARVIS_ALLOWED_CALLERS"))
    brain = Brain(env.get("JARVIS_AI_BASE_URL", ""), env.get("JARVIS_AI_MODEL", ""),
                  env.get("JARVIS_AI_API_KEY", ""),
                  env.get("JARVIS_AI_COMPLETION_PATH", "/chat/completions"))
    voice = Voice(env.get("JARVIS_PIPER", "/opt/meshcore-ai-bot/jarvis/piper/piper"),
                  env.get("JARVIS_VOICE", "/opt/meshcore-ai-bot/jarvis/de_DE-thorsten-medium.onnx"))
    ears = Ears(env.get("VOSK_MODEL", "/home/marc/dab-warnings-meshcore/models/vosk-model-small-de-0.15"))
    phone = Phone()
    jarvis = Jarvis(allowed, phone, Route(), AudioLock(),
                    lambda nodes: Call(phone, nodes, ears, brain, voice))
    asyncio.run(jarvis.serve())


if __name__ == "__main__":
    main()
