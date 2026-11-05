"""Explicit Shazam fingerprint lookup; never runs on its own."""

import asyncio
import json
import sys
from pathlib import Path

from shazamio import Shazam


async def main():
    async with Shazam() as shazam:
        result = await asyncio.wait_for(
            shazam.recognize(await asyncio.to_thread(Path(sys.argv[1]).read_bytes)), timeout=35
        )
    track = result.get("track") or {}
    print(
        json.dumps(
            {"title": str(track.get("title", ""))[:300], "artist": str(track.get("subtitle", ""))[:300]}
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
