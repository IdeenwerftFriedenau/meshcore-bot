"""Read every occupied companion slot without changing channel keys."""

import hashlib

from meshcore import EventType

MANAGED = {
    "#ideenwerft",
    "#test",
    "#mc-treffen",
    "#warnings",
    "#news",
    "#fragdenbot",
    "#bot",
    "Public",
    "#public",
}


def valid_name(name):
    return (
        isinstance(name, str)
        and bool(name.strip())
        and len(name.encode()) <= 32
        and not any(ord(c) < 32 or ord(c) == 127 for c in name)
    )


async def read_receive_channels(mc, previous=None):
    indices = {}
    for slot in range(16):
        channel = await mc.commands.get_channel(slot)
        if channel.type != EventType.CHANNEL_INFO:
            if previous and slot in previous:
                raise RuntimeError("Existing channel could not be refreshed")
            continue
        name = channel.payload.get("channel_name", "")
        if not name:
            continue
        if not valid_name(name):
            raise RuntimeError("Invalid channel name in companion slot")
        if name in indices.values():
            raise RuntimeError("Duplicate companion channel name")
        if name in MANAGED:
            expected = (
                bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")
                if name == "Public"
                else hashlib.sha256(name.encode()).digest()[:16]
            )
            if channel.payload.get("channel_secret") != expected:
                raise RuntimeError(f"Channel configuration mismatch at slot {slot}")
        indices[slot] = name
    return indices
