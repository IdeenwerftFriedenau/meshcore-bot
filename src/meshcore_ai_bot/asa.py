"""Offline ASA adapter inbox. This is not an RF/DAB decoder."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from .broadcasts import date_value, plain, warning_text
from .protocol import allows_send, split_text
from .regions import STATES, canonical, ordered


def normalize(event, config, now):
    if not isinstance(event, dict) or event.get("version") != 1 or event.get("protocol") != "DAB+ ASA":
        raise ValueError("Expected version 1 DAB+ ASA event")
    if event.get("status") != "Actual" or event.get("kind") not in {"alert", "update", "cancel"}:
        raise ValueError("Only actual ASA alerts, updates and cancellations")
    received = date_value(event.get("received_at"))
    if received is None or not -30 <= now - received <= config.asa.max_age_seconds:
        raise ValueError("ASA event is stale or lacks receiver time")
    identity = event.get("id")
    if not isinstance(identity, str) or not 1 <= len(identity) <= 250:
        raise ValueError("Missing source event id")
    codes = event.get("regions")
    if not isinstance(codes, list) or not codes or any(not isinstance(c, str) for c in codes):
        raise ValueError("Missing target states")
    codes = ordered(canonical(c) for c in codes) if all(canonical(c) in STATES for c in codes) else []
    if not codes:
        raise ValueError("Invalid target states")
    onset, expires = date_value(event.get("onset")), date_value(event.get("expires"))
    if onset is None or expires is None or expires <= now or onset >= expires:
        raise ValueError("Missing or invalid source validity interval")
    for key in ["title", "description", "area", "severity"]:
        if not isinstance(event.get(key), str) or not event[key].strip() or len(event[key]) > 12000:
            raise ValueError("Missing or oversized ASA warning content")
    return {
        "identity": "asa:" + hashlib.sha256(identity.encode()).hexdigest(),
        "source": "DAB+ ASA",
        "title": plain(event["title"]),
        "description": plain(event["description"]),
        "area": plain(event["area"]),
        "severity": plain(event["severity"]),
        "instruction": plain(str(event.get("instruction", ""))[:12000]),
        "onset": onset,
        "expires": expires,
        "codes": codes,
        "url": "DAB+ ASA (terrestrisch)",
        "kind": event["kind"],
    }


def ingest(config, raw):
    if len(raw) > 65536:
        raise ValueError("ASA event exceeds 64 KB")
    event = json.loads(raw)
    normalize(event, config, time.time())
    root = Path(config.asa.inbox)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / (hashlib.sha256(raw).hexdigest() + ".json")
    temp = target.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, target)
    return str(target)


def poll(config, store):
    if not config.asa.enabled or not config.broadcasts.warnings_enabled:
        return 0
    root = Path(config.asa.inbox)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    now, queued = time.time(), 0
    for folder in ["processed", "rejected"]:
        (root / folder).mkdir(exist_ok=True, mode=0o700)
        for old in (root / folder).glob("*.json"):
            if old.stat().st_mtime < now - config.storage.retention_hours * 3600:
                old.unlink()
    for path in sorted(root.glob("*.json"))[:100]:
        if path.is_symlink():
            continue
        destination = "rejected"
        try:
            if path.stat().st_size > 65536:
                raise ValueError("Oversized ASA event")
            event = json.loads(path.read_bytes())
            warning = normalize(event, config, now)
            warning["codes"] = ordered(set(warning["codes"]).intersection(config.asa.regions))
            if warning["codes"]:
                batches = {
                    ch.name: split_text(
                        warning_text(warning, config.broadcasts.include_instructions),
                        config.radio.packet_bytes,
                        "/".join(warning["codes"]),
                    )
                    for ch in config.channels
                    if allows_send(config, ch.name, "warnings")
                }
                revision = hashlib.sha256(json.dumps(warning, sort_keys=True).encode()).hexdigest()
                queued += store.broadcast(
                    warning["identity"],
                    revision,
                    now if warning["kind"] == "cancel" else warning["expires"],
                    batches,
                    "warnings",
                    now,
                )
                store.meta_set("asa:last_received", event["received_at"])
            destination = "processed"
        except (ValueError, KeyError, TypeError, UnicodeError):
            store.meta_set("asa:last_rejected", now)
        path.replace(root / destination / path.name)
    return queued
