"""Validated, secret-free TOML configuration."""

from __future__ import annotations

import dataclasses
import ipaddress
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

PUBLIC_CHANNELS = frozenset({"Public", "#public"})


@dataclass
class Station:
    name: str = "MeshCore AI Bot"
    location: str = "nicht konfiguriert"
    own_names: list[str] = field(default_factory=list)
    timezone: str = "Europe/Berlin"
    scopes: dict[str, str] = field(default_factory=dict)


@dataclass
class Radio:
    port: str = "/dev/ttyUSB0"
    create_channels: bool = False
    send_interval_seconds: float = 30
    receive_quiet_seconds: float = 4
    max_rx_wait_seconds: float = 60
    packet_bytes: int = 110


@dataclass
class AI:
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "google/gemini-2.5-flash"
    api_key_env: str = "MESHCORE_AI_API_KEY"
    allow_local_http: bool = False
    timeout_seconds: float = 45
    requests_per_minute: int = 6
    requests_per_day: int = 250
    max_tokens: int = 600

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")


@dataclass
class Storage:
    path: str = "data/bot.sqlite3"
    retention_hours: int = 24
    message_max_age_seconds: int = 300
    reply_ttl_seconds: int = 1800
    history_turns: int = 4


@dataclass
class Weather:
    enabled: bool = True
    location: str = "Berlin-Friedenau"
    latitude: float = 52.472
    longitude: float = 13.331


@dataclass
class Broadcasts:
    warnings_enabled: bool = False
    warning_regions: list[str] = field(default_factory=lambda: ["BER", "BB"])
    warning_poll_seconds: int = 300
    include_instructions: bool = False
    news_enabled: bool = False
    news_poll_seconds: int = 3600
    rss_feeds: list[str] = field(default_factory=list)


@dataclass
class Channel:
    name: str
    mode: str = "off"
    keyword: str = "KI"
    mentions: list[str] = field(default_factory=list)
    respond_to_checks: bool = False
    broadcasts: list[str] = field(default_factory=list)
    secret_env: str = ""


@dataclass
class Config:
    station: Station = field(default_factory=Station)
    radio: Radio = field(default_factory=Radio)
    ai: AI = field(default_factory=AI)
    storage: Storage = field(default_factory=Storage)
    weather: Weather = field(default_factory=Weather)
    broadcasts: Broadcasts = field(default_factory=Broadcasts)
    channels: list[Channel] = field(default_factory=lambda: [Channel("#fragdenbot", "all")])

    def channel(self, name: str) -> Channel | None:
        return next((ch for ch in self.channels if ch.name == name), None)

    def validate(self) -> Config:
        if not self.station.name.strip() or len(self.station.name.encode()) > 32:
            raise ValueError("station.name needs 1–32 UTF-8 bytes")
        ZoneInfo(self.station.timezone)
        for section, fields in [
            (self.radio, ["create_channels"]),
            (self.ai, ["allow_local_http"]),
            (self.weather, ["enabled"]),
            (self.broadcasts, ["warnings_enabled", "news_enabled", "include_instructions"]),
        ]:
            for name in fields:
                if type(getattr(section, name)) is not bool:
                    raise ValueError(f"{name} must be true or false")
        ranges = [
            (self.radio.send_interval_seconds, 5, 3600, "send_interval_seconds"),
            (self.radio.receive_quiet_seconds, 0, 30, "receive_quiet_seconds"),
            (self.radio.max_rx_wait_seconds, 0, 300, "max_rx_wait_seconds"),
            (self.radio.packet_bytes, 50, 110, "packet_bytes"),
            (self.storage.retention_hours, 1, 24 * 30, "retention_hours"),
            (self.storage.message_max_age_seconds, 10, 3600, "message_max_age_seconds"),
            (self.storage.reply_ttl_seconds, 30, 7200, "reply_ttl_seconds"),
            (self.storage.history_turns, 0, 12, "history_turns"),
            (self.ai.requests_per_minute, 1, 60, "requests_per_minute"),
            (self.ai.requests_per_day, 1, 10000, "requests_per_day"),
            (self.ai.max_tokens, 32, 2048, "max_tokens"),
            (self.ai.timeout_seconds, 1, 180, "timeout_seconds"),
            (self.broadcasts.warning_poll_seconds, 300, 86400, "warning_poll_seconds"),
            (self.broadcasts.news_poll_seconds, 300, 86400, "news_poll_seconds"),
        ]
        for value, low, high, name in ranges:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise ValueError(f"{name} must be between {low} and {high}")
        for value in [self.radio.packet_bytes, self.storage.history_turns, self.ai.max_tokens]:
            if type(value) is not int:
                raise ValueError("packet_bytes, history_turns and max_tokens must be integers")
        validate_url(self.ai.base_url, self.ai.allow_local_http)
        if not self.ai.model.strip():
            raise ValueError("ai.model is required")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.ai.api_key_env):
            raise ValueError("ai.api_key_env must be an environment variable name")
        if not -90 <= self.weather.latitude <= 90 or not -180 <= self.weather.longitude <= 180:
            raise ValueError("Invalid weather coordinates")
        if not set(self.broadcasts.warning_regions) <= {"BER", "BB"}:
            raise ValueError("Only BER and BB warning regions are supported")
        if len({ch.name for ch in self.channels}) != len(self.channels):
            raise ValueError("Duplicate channel names")
        if not self.channels:
            raise ValueError("At least one channel must be configured")
        for ch in self.channels:
            if not ch.name or len(ch.name.encode()) > 32 or any(ord(c) < 32 for c in ch.name):
                raise ValueError("Channel names need 1–32 UTF-8 bytes without control characters")
            if ch.mode not in {"off", "all", "keyword", "mention"}:
                raise ValueError(f"Unknown mode for {ch.name}")
            if type(ch.respond_to_checks) is not bool:
                raise ValueError("respond_to_checks must be boolean")
            if not set(ch.broadcasts) <= {"warnings", "news"}:
                raise ValueError("Unknown broadcast stream")
            if ch.name in PUBLIC_CHANNELS and (ch.mode != "off" or ch.broadcasts):
                raise ValueError("Public and #public are protected: replies and broadcasts must stay off")
            if ch.name.startswith("#") and ch.secret_env:
                raise ValueError(
                    "Hashtag keys are derived from the name; use a non-hashtag private channel with secret_env"
                )
            if ch.mode == "keyword" and not ch.keyword.strip():
                raise ValueError("A keyword channel needs a keyword")
        for url in self.broadcasts.rss_feeds:
            validate_url(url)
        return self


def validate_url(value: str, allow_local_http: bool = False) -> None:
    parsed = urlsplit(value)
    local = parsed.hostname == "localhost"
    try:
        local = local or ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        pass
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise ValueError("Service URLs need a hostname and no embedded credentials/query/fragment")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local and allow_local_http):
        raise ValueError("HTTPS is required; HTTP may be explicitly enabled for loopback AI services")


def _section(cls, values):
    if not isinstance(values, dict):
        raise ValueError(f"{cls.__name__} must be a TOML table")
    valid = {f.name for f in dataclasses.fields(cls)}
    unknown = set(values) - valid
    if unknown:
        raise ValueError(f"Unknown {cls.__name__} settings: {', '.join(sorted(unknown))}")
    return cls(**values)


def load_config(path: str | Path) -> Config:
    path = Path(path).expanduser().resolve()
    with path.open("rb") as stream:
        raw = tomllib.load(stream)
    known = {"station", "radio", "ai", "storage", "weather", "broadcasts", "channels"}
    if set(raw) - known:
        raise ValueError("Unknown configuration section")
    config = Config(
        station=_section(Station, raw.get("station", {})),
        radio=_section(Radio, raw.get("radio", {})),
        ai=_section(AI, raw.get("ai", {})),
        storage=_section(Storage, raw.get("storage", {})),
        weather=_section(Weather, raw.get("weather", {})),
        broadcasts=_section(Broadcasts, raw.get("broadcasts", {})),
        channels=[
            _section(Channel, ch) for ch in raw.get("channels", [{"name": "#fragdenbot", "mode": "all"}])
        ],
    )
    for name, key in [("base_url", "MESHCORE_AI_BASE_URL"), ("model", "MESHCORE_AI_MODEL")]:
        if os.environ.get(key):
            setattr(config.ai, name, os.environ[key])
    config.radio.port = os.environ.get("MESHCORE_AI_RADIO_PORT", config.radio.port)
    config.storage.path = str(
        (path.parent / os.environ.get("MESHCORE_AI_DB_PATH", config.storage.path)).resolve()
    )
    return config.validate()
