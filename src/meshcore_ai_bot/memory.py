"""Conservative history admission; personal facts are data, never global rules."""

import re

OVERRIDE = re.compile(
    r"(?:ignorier|umgeh|überschreib|ueberschreib|vergiss).{0,70}(?:regeln|anweisungen|system.?prompt|schutz)"
    r"|(?:system.?prompt).{0,50}(?:zeige|verrat|ausgeben)"
    r"|(?:zeige|verrat|ausgeben).{0,50}(?:system.?prompt)"
    r"|(?:ignore|override).{0,50}(?:instructions|rules|system prompt)"
    r"|\b(?:du bist jetzt|tu so als|spiele die rolle|act as|pretend to be|you are now)\b"
    r"|(?:<\|?system\|?>|\[system\]|###\s*(?:system|instruction))"
    r"|\b(?:antworte|schreibe|verwende|benutze|füge|du musst|du sollst|ab jetzt|ab sofort|von nun an)"
    r".{0,100}\b(?:immer|stets|alle[nmrs]?|jede[nmrs]?|regel|vorgabe|anweisung|stil|ton)\b",
    re.I | re.S,
)


def safe_memory(text: str) -> bool:
    return not OVERRIDE.search(text)


def control(text):
    value = " ".join(text.casefold().strip(" ?!.").split())
    if value in {"vergiss mich", "/forget", "memory aus", "erinnerung aus"}:
        return "forget"
    if value in {"memory an", "erinnerung an"}:
        return "enable"
    if value in {"was weisst du über mich", "was weisst du ueber mich", "/memory", "mein memory"}:
        return "inspect"
    return None
