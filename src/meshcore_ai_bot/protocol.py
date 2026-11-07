"""Pure channel policy and UTF-8 framing. No network or hardware side effects."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .config import PUBLIC_CHANNELS, Config


@dataclass(frozen=True)
class Incoming:
    channel: str
    sender: str
    text: str
    timestamp: int

    @property
    def identity(self) -> str:
        raw = f"{self.channel}\0{self.sender}\0{self.timestamp}\0{self.text}"
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def from_radio(cls, channel: str, text: str, timestamp: int) -> Incoming:
        sender, body = text.split(": ", 1) if ": " in text else ("unknown", text)
        return cls(channel, sender.strip(), body, timestamp)


def _mention_pattern(names: list[str], hashtags: bool = False) -> re.Pattern:
    alternatives = [
        re.escape(name).replace(r"\ ", r"[\s-]+") for name in sorted(names, key=len, reverse=True)
    ]
    # Accept @[Node Name], @Node Name and @shortname; do not match a longer word.
    pattern = (
        r"(?<![\w@])@(?:\[(?:" + "|".join(alternatives) + r")\]|(?:" + "|".join(alternatives) + r")(?![\w-]))"
    )
    if hashtags:
        pattern += r"|(?<![\w@#])#(?:ideenwerft-friedenau|ideenwerft)(?![\w-])"
    return re.compile(pattern, re.I)


def radio_check(text: str) -> str | None:
    if text.strip().casefold() in {"help", "/help", "hilfe", "befehle", "?"}:
        return (
            "Hilfe: wx, wx morgen, ping, mehr, Quelle? In #bot mit @Ideenwerft ansprechen; "
            "in #fragdenbot einfach fragen."
        )
    body = re.sub(r"[?!.,:;]+$", "", " ".join(text.casefold().split())).strip()
    if re.fullmatch(r"ping(?: ping){0,2}(?: ?(?:[0-9]{1,3}|1 2 3))?", body):
        return "Pong! Deine Nachricht ist hier angekommen."
    if re.fullmatch(
        r"test(?: test){0,2}(?: ?(?:[0-9]{1,3}|1 2 3))?|123|1 2 3|funk[ -]?(?:check|test)|"
        r"radio[ -]?check|check(?: check)?|hört mich jemand|hoert mich jemand|"
        r"ist jemand da|bist du da|bist du noch da|noch da",
        body,
    ):
        return "Test angekommen – deine Textnachricht ist hier."
    return None


def may_remember(message: Incoming, config: Config, now: float) -> bool:
    channel = config.channel(message.channel)
    own = {name.strip().casefold() for name in [config.station.name, *config.station.own_names]}
    if not channel or message.channel in PUBLIC_CHANNELS:
        return False
    if message.sender.strip().casefold() in own:
        return False
    if (
        type(message.timestamp) is not int
        or not now - config.storage.message_max_age_seconds <= message.timestamp <= now + 120
    ):
        return False
    if not isinstance(message.text, str) or len(message.text.encode()) > 600 or not message.text.strip():
        return False
    return True


def question_for(message: Incoming, config: Config, now: float) -> str | None:
    if not may_remember(message, config, now):
        return None
    channel = config.channel(message.channel)
    if channel.mode == "off":
        return None
    body = message.text.strip()
    if channel.mode == "keyword":
        pattern = r"(?<!\w)" + re.escape(channel.keyword) + r"(?!\w)"
        if not re.search(pattern, body, re.I):
            return None
        # Strip an invocation, not semantic occurrences such as 'Was ist KI?'.
        body = re.sub(r"^\s*@?" + re.escape(channel.keyword) + r"\b[\s,:!?-]*", "", body, flags=re.I)
    elif channel.mode == "mention":
        pattern = _mention_pattern(channel.mentions or [config.station.name], channel.name == "#bot")
        if not pattern.search(body) and not (channel.respond_to_checks and radio_check(body)):
            return None
        body = pattern.sub("", body, count=1).strip(" ,:")
    return body.strip() or "Hallo"


def ignored_reason(message, config, now):
    channel = config.channel(message.channel)
    if not channel:
        return "unknown_channel"
    if message.channel in PUBLIC_CHANNELS:
        return "public_protected"
    if message.sender.strip().casefold() in {
        name.strip().casefold() for name in [config.station.name, *config.station.own_names]
    }:
        return "own_message"
    if type(message.timestamp) is not int:
        return "invalid_timestamp"
    if message.timestamp < now - config.storage.message_max_age_seconds:
        return "old_message"
    if message.timestamp > now + 120:
        return "future_message"
    if not isinstance(message.text, str) or not message.text.strip() or len(message.text.encode()) > 600:
        return "invalid_text"
    if channel.mode == "off":
        return "channel_off"
    return "missing_keyword" if channel.mode == "keyword" else "missing_mention"


def allows_send(config: Config, channel_name: str, stream: str) -> bool:
    channel = config.channel(channel_name)
    if not channel or channel_name in PUBLIC_CHANNELS:
        return False
    if stream == "reply":
        return channel.mode != "off"
    enabled = {"warnings": config.broadcasts.warnings_enabled, "news": config.broadcasts.news_enabled}
    return enabled.get(stream, False) and stream in channel.broadcasts


MORE_ENDING = " … (weiter mit „mehr“)"
SHORT_ENDING = " …"


def followup_command(question: str) -> str | None:
    """'mehr' continues the last answer, 'Quelle?' names its sources."""
    body = re.sub(r"[?!.,:;]+$", "", " ".join(question.casefold().split())).strip()
    if body in {"mehr", "weiter", "mehr bitte", "bitte mehr", "weiter bitte", "erzähl mehr", "mehr details"}:
        return "more"
    if re.fullmatch(r"(?:und )?(?:die |deine )?(?:quelle|quellen|link|links)(?: bitte)?|woher weißt du das|woher weisst du das", body):
        return "source"
    return None


def remaining_text(full: str, shown: str) -> str:
    """The part of a fitted reply that did not go out on air."""
    if shown == full:
        return ""
    for ending in (MORE_ENDING, SHORT_ENDING):
        if shown.endswith(ending):
            head = shown[: -len(ending)]
            if full.startswith(head):
                return full[len(head):].strip()
    return ""


def fit_reply(prefix: str, text: str, limit: int, max_parts: int) -> tuple[str, list[str]]:
    """Keep line breaks, including drawings, within a hard packet and character budget."""
    parts = split_text(prefix + text, limit)
    if len(text) <= 420 and len(parts) <= max_parts:
        return text, parts
    ending = MORE_ENDING
    if len(split_text(prefix + ending, limit)) > max_parts:
        ending = SHORT_ENDING
    low, high, best = 0, min(420 - len(ending), len(text)), ending
    while low <= high:
        middle = (low + high) // 2
        candidate = text[:middle].rstrip() + ending
        if len(split_text(prefix + candidate, limit)) <= max_parts:
            best, low = candidate, middle + 1
        else:
            high = middle - 1
    return best, split_text(prefix + best, limit)


def split_text(text: str, limit: int = 110, label: str = "") -> list[str]:
    """Preserve every character; reserve all labels and part numbers in byte budget."""
    if not text or limit < 24:
        return []
    prefix = label.strip() + " " if label.strip() else ""
    if len((prefix + text).encode()) <= limit:
        return [prefix + text]
    count = 1
    while True:
        budget = limit - len(f"{prefix}[{count}/{count}] ".encode())
        if budget < 4:
            raise ValueError("Packet prefix leaves no room for UTF-8 text")
        chunks, rest = [], text
        while rest:
            if len(rest.encode()) <= budget:
                chunks.append(rest)
                break
            end = len(rest.encode()[:budget].decode("utf-8", "ignore"))
            separator = rest.rfind(" | ", 0, end)
            space = rest.rfind(" ", 0, end)
            cut = separator + 3 if separator > 0 else space + 1 if space > 0 else end
            chunks.append(rest[:cut])
            rest = rest[cut:]
        if len(chunks) == count:
            return [f"{prefix}[{i}/{count}] " + part for i, part in enumerate(chunks, 1)]
        count = len(chunks)
