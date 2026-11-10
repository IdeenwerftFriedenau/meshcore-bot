"""Add one hashtag channel (e.g. #börsenbot) to a free companion slot. Run with the gateway stopped.

    sudo systemctl stop meshcore-gateway
    python add_hashtag_channel.py /dev/serial/by-id/... '#börsenbot'
    sudo systemctl start meshcore-gateway

Existing slots are never overwritten. A slot that already carries the name with the right key is
left alone. This only writes the channel table over USB; it sends nothing on air.
"""

import asyncio
import hashlib
import sys

from meshcore import EventType, MeshCore


def hashtag_key(name: str) -> bytes:
    return hashlib.sha256(name.encode()).digest()[:16]


async def add(port: str, name: str) -> int:
    if not name.startswith("#") or not 2 <= len(name.encode()) <= 32:
        raise SystemExit("Name must be a hashtag channel with at most 32 bytes, e.g. '#börsenbot'")
    key = hashtag_key(name)
    mc = await MeshCore.create_serial(port, default_timeout=5, auto_reconnect=False)
    if mc is None:
        raise SystemExit("No MeshCore companion answered on " + port)
    try:
        empty = []
        for slot in range(16):
            info = await mc.commands.get_channel(slot)
            if info.type != EventType.CHANNEL_INFO:
                continue
            current = info.payload.get("channel_name", "")
            if current == name:
                if info.payload.get("channel_secret") != key:
                    raise SystemExit(f"Slot {slot} has {name} with a different key; not touched")
                print(f"{name} already in slot {slot}")
                return slot
            if not current:
                empty.append(slot)
        if not empty:
            raise SystemExit("No free channel slot; nothing changed")
        slot = empty[0]
        result = await mc.commands.set_channel(slot, name, key)
        if result.type != EventType.OK:
            raise SystemExit("Companion rejected the channel")
        check = await mc.commands.get_channel(slot)
        if (
            check.type != EventType.CHANNEL_INFO
            or check.payload.get("channel_name") != name
            or check.payload.get("channel_secret") != key
        ):
            raise SystemExit("Verification failed after writing slot " + str(slot))
        print(f"{name} added in slot {slot}")
        return slot
    finally:
        await mc.disconnect()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    asyncio.run(add(sys.argv[1], sys.argv[2]))
