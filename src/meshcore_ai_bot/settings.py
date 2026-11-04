"""Atomic validated dashboard settings; credentials are never returned."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import tempfile
import tomllib
from pathlib import Path

import tomli_w
from dotenv import dotenv_values

from .config import bind_secrets, load_config

EDITABLE = {
    "station": {"name", "location", "own_names", "timezone"},
    "radio": {
        "port",
        "create_channels",
        "send_interval_seconds",
        "receive_quiet_seconds",
        "max_rx_wait_seconds",
        "packet_bytes",
        "reply_max_packets",
        "broadcast_max_packets",
        "max_pending_packets",
        "max_pending_messages",
        "incoming_per_minute",
        "incoming_per_sender_per_minute",
    },
    "ai": {
        "base_url",
        "model",
        "allow_local_http",
        "timeout_seconds",
        "requests_per_minute",
        "requests_per_day",
        "max_tokens",
    },
    "weather": {"enabled", "location", "latitude", "longitude"},
    "storage": {"retention_hours", "message_max_age_seconds", "reply_ttl_seconds", "history_turns"},
    "broadcasts": {
        "warnings_enabled",
        "dwd_enabled",
        "bbk_enabled",
        "warning_regions",
        "dwd_regions",
        "bbk_regions",
        "warning_poll_seconds",
        "include_instructions",
        "news_enabled",
        "news_poll_seconds",
        "rss_feeds",
    },
    "asa": {"enabled", "regions", "max_age_seconds", "receiver", "dab_channel", "device_index"},
}
CHANNEL_FIELDS = {"name", "mode", "keyword", "mentions", "respond_to_checks", "broadcasts"}


def digest(path):
    path = Path(path)
    env = path.parent / ".env"
    return hashlib.sha256(path.read_bytes() + (env.read_bytes() if env.exists() else b"")).hexdigest()


def read_settings(path):
    config = load_config(path)
    values = {
        section: {k: getattr(getattr(config, section), k) for k in sorted(keys)}
        for section, keys in EDITABLE.items()
    }
    values["channels"] = [
        {k: v for k, v in dataclasses.asdict(ch).items() if k in CHANNEL_FIELDS} for ch in config.channels
    ]
    env = dotenv_values(Path(path).parent / ".env")
    return {
        "settings": values,
        "revision": digest(path),
        "api_key_configured": bool(config.ai.api_key or env.get(config.ai.api_key_env)),
        "overrides": [
            key
            for key in ["MESHCORE_AI_BASE_URL", "MESHCORE_AI_MODEL", "MESHCORE_AI_RADIO_PORT"]
            if os.environ.get(key)
        ],
    }


def atomic(path, raw):
    fd, name = tempfile.mkstemp(prefix=".meshcore-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save_settings(path, payload):
    path = Path(path)
    if not isinstance(payload, dict) or set(payload) - {"revision", "settings", "api_key"}:
        raise ValueError("Unknown request fields")
    if payload.get("revision") != digest(path):
        raise FileExistsError("Settings changed. Reload before saving.")
    raw = tomllib.loads(path.read_text())
    changes = payload.get("settings")
    if not isinstance(changes, dict):
        raise ValueError("Settings must be an object")
    for section, values in changes.items():
        if section == "channels":
            if not isinstance(values, list):
                raise ValueError("Channels must be a list")
            existing = {ch["name"]: ch for ch in raw.get("channels", [])}
            if len(values) != len(existing) or {ch.get("name") for ch in values} != set(existing):
                raise ValueError("Dashboard can only edit existing channel policies")
            for channel in values:
                if set(channel) - CHANNEL_FIELDS:
                    raise ValueError("Unknown channel fields")
                existing[channel["name"]].update(channel)
        else:
            if section not in EDITABLE or not isinstance(values, dict) or set(values) - EDITABLE[section]:
                raise ValueError("Unknown or protected settings")
            raw.setdefault(section, {}).update(values)
    key = payload.get("api_key", "")
    if not isinstance(key, str) or len(key) > 4096 or any(c.isspace() or c in "'\\\"" for c in key):
        raise ValueError("Invalid API key")
    data = tomli_w.dumps(raw).encode()
    fd, name = tempfile.mkstemp(suffix=".toml", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        config = load_config(name)
    finally:
        os.unlink(name)
    atomic(path.with_suffix(".toml.bak"), path.read_bytes())
    if key:
        envpath = path.parent / ".env"
        rows = envpath.read_text().splitlines() if envpath.exists() else []
        rows = [
            row
            for row in rows
            if row.strip().removeprefix("export ").split("=", 1)[0].strip() != config.ai.api_key_env
        ]
        rows.append(config.ai.api_key_env + "='" + key + "'")
        atomic(envpath, ("\n".join(rows) + "\n").encode())
    atomic(path, data)
    return read_settings(path)


def refresh_secrets(config, path):
    bind_secrets(config, path)
