#!/usr/bin/env python3
"""Capture Dlf hourly bulletins, retain private MP3s briefly and queue cautious summaries."""
import json
import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

PORT = 7979
SERVICE_ID = "0xd210"  # Dlf on DR Deutschland, ensemble 5C
STREAM_URL = f"http://127.0.0.1:{PORT}/mp3/{SERVICE_ID}"
MODEL_PATH = os.environ.get("VOSK_MODEL", "/home/marc/dab-warnings-meshcore/models/vosk-model-small-de-0.15")
RECORDINGS_DIR = Path(os.environ.get("DAB_RECORDINGS_DIR", "/home/marc/dab-warnings-meshcore/recordings"))
INGEST_URL = os.environ.get("DAB_INGEST_URL", "")
TOKEN = os.environ.get("INGEST_TOKEN", "")
CAPTURE_SECONDS = 300
RETENTION_SECONDS = 24 * 3600
BERLIN = ZoneInfo("Europe/Berlin")
LOG = logging.getLogger("dab-news")


def endpoint(suffix):
    parts = urlsplit(INGEST_URL)
    path = parts.path.rsplit("/", 1)[0] + "/" + suffix.lstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def post_json(url, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body, headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    with urlopen(req, timeout=75) as response:
        return json.loads(response.read(24000))


def upload_recording(path, hour):
    body = path.read_bytes()
    req = Request(endpoint("recording"), data=body, headers={
        "Authorization": "Bearer " + TOKEN,
        "Content-Type": "audio/mpeg",
        "X-Recording-Hour": str(hour),
        "Accept": "application/json",
    }, method="POST")
    with urlopen(req, timeout=75) as response:
        return json.loads(response.read(4000))


def prune_local_recordings(now=None):
    now = time.time() if now is None else now
    RECORDINGS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in RECORDINGS_DIR.glob("dlf-*.mp3"):
        try:
            recorded_hour = int(path.stem[4:])
            if recorded_hour > now + 120 or now - recorded_hour > RETENTION_SECONDS:
                path.unlink()
        except (ValueError, OSError):
            continue


def get_dlf_service():
    req = Request(f"http://127.0.0.1:{PORT}/mux.json", headers={"Accept": "application/json"})
    with urlopen(req, timeout=5) as response:
        mux = json.loads(response.read(300000))
    return any(s.get("sid", "").lower() == SERVICE_ID and any(c.get("transportmode") == "audio" for c in s.get("components", [])) for s in mux.get("services", []))


def wait_for_dlf_service(hour, checker=None, clock=None, sleep=None):
    checker = checker or get_dlf_service
    clock = clock or time.time
    sleep = sleep or time.sleep
    deadline = hour + 30
    attempts = 0
    while clock() <= deadline:
        attempts += 1
        try:
            if checker() and clock() <= deadline:
                return
        except (OSError, ValueError, TimeoutError):
            pass
        remaining = deadline-clock()
        if remaining <= 0:
            break
        LOG.warning("Dlf receiver not ready at top of hour; retrying (%d)", attempts)
        sleep(min(2,remaining))
    raise RuntimeError("Dlf not ready within the first 30 seconds; skipping stale bulletin")


def capture_transcript(model, hour):
    from vosk import KaldiRecognizer

    wait_for_dlf_service(hour)
    prune_local_recordings()
    path = RECORDINGS_DIR / f"dlf-{hour}.mp3"
    temporary = RECORDINGS_DIR / f".dlf-{hour}.mp3.tmp"
    command = ["/usr/bin/ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", STREAM_URL, "-t", str(CAPTURE_SECONDS), "-vn", "-c:a", "copy", "-f", "mp3", str(temporary)]
    result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=CAPTURE_SECONDS + 50, check=False)
    if result.returncode != 0 or not temporary.exists() or not 16_000 <= temporary.stat().st_size <= 12 * 1024 * 1024:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("DAB audio stream unavailable or invalid recording")
    temporary.replace(path)

    decode = ["/usr/bin/ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path), "-t", str(CAPTURE_SECONDS), "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"]
    audio_result = subprocess.run(decode, capture_output=True, timeout=CAPTURE_SECONDS + 50, check=False)
    audio = audio_result.stdout
    if audio_result.returncode != 0 or len(audio) < 16000 * 2 * 25:
        raise RuntimeError("Recorded audio could not be decoded")
    recognizer = KaldiRecognizer(model, 16000)
    recognizer.SetWords(False)
    phrases = []
    for pos in range(0, len(audio), 8000):
        if recognizer.AcceptWaveform(audio[pos:pos + 8000]):
            phrase = json.loads(recognizer.Result()).get("text", "").strip()
            if phrase:
                phrases.append(phrase)
    tail = json.loads(recognizer.FinalResult()).get("text", "").strip()
    if tail:
        phrases.append(tail)
    transcript = " ".join(" ".join(phrases).split())
    if len(transcript) < 350 or len(transcript) > 9000 or len(transcript.split()) < 55:
        raise RuntimeError("Transcript failed length check")
    return transcript, len(audio), path


def retry_post(suffix, payload):
    for attempt in range(4):
        try:
            return post_json(endpoint(suffix), payload)
        except Exception as exc:
            code = exc.code if isinstance(exc, HTTPError) else type(exc).__name__
            LOG.warning("%s attempt %d/4 failed: %s", suffix, attempt + 1, code)
            if isinstance(exc, HTTPError) and exc.code not in (408, 429, 500, 502, 503, 504):
                raise
            if attempt == 3:
                raise
            time.sleep((15, 30, 60)[attempt])


def summarize_and_queue(transcript, hour):
    summary = retry_post("news-summary", {"transcript": transcript})
    items = summary.get("items")
    if not isinstance(items, list) or not items:
        raise RuntimeError("No usable news summary returned")
    label = datetime.fromtimestamp(hour, BERLIN).strftime("DLF %H Uhr · ")
    headlines = []
    rejected = 0
    for item in items[:8]:
        if not isinstance(item, str):
            rejected += 1
            continue
        text = " ".join(item.split()).strip()
        if text and len(text) <= 95 and len(text.encode("utf-8")) <= 90:
            headlines.append(text)
        else:
            rejected += 1
    messages = []
    total = len(headlines)
    for index, headline in enumerate(headlines, 1):
        text = f"{label}[{index}/{total}] {headline}"
        if len(text.encode("utf-8")) <= 110:
            messages.append(text)
        else:
            rejected += 1
    if rejected:
        LOG.warning("Omitted %d headline(s) that exceeded the MeshCore message limit", rejected)
    if not messages:
        LOG.info("Summary did not fit the MeshCore message limit; nothing sent")
        return
    result = retry_post("news-outbox", {"broadcast_hour": hour, "items": messages})
    LOG.info("Dlf digest queued: %d message(s), status=%s", len(result.get("messages", [])), result.get("status", "unknown"))


def next_hour(now=None):
    now = time.time() if now is None else now
    candidate = (int(now // 3600) + 1) * 3600
    if os.environ.get("DAB_NEWS_EVEN_HOURS_ONLY") == "1":
        while datetime.fromtimestamp(candidate, BERLIN).hour % 2:
            candidate += 3600
    return candidate


def main():
    from vosk import Model, SetLogLevel

    SetLogLevel(-1)
    logging.info("Loading offline German speech model")
    model = Model(MODEL_PATH)
    RECORDINGS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    prune_local_recordings()
    logging.info("Ready: Dlf on 5C; recordings retained locally for at most 24 hours")
    while True:
        hour = next_hour()
        while time.time() < hour:
            time.sleep(min(5, hour - time.time()))
        try:
            transcript, audio_bytes, path = capture_transcript(model, hour)
            LOG.info("Dlf audio captured: %d seconds, transcript=%d characters", audio_bytes // (16000 * 2), len(transcript))
            try:
                stored = upload_recording(path, hour)
                LOG.info("Dlf recording stored for admin playback: %d bytes", stored.get("bytes", path.stat().st_size))
            except Exception as exc:
                LOG.warning("Recording upload failed; private Pi copy retained (%s)", type(exc).__name__)
            summarize_and_queue(transcript, hour)
        except Exception as exc:
            LOG.warning("No Dlf news sent for this hour: %s", type(exc).__name__)
        time.sleep(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
