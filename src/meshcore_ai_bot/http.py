"""Bounded reads; no redirect forwarding of credentials."""

from __future__ import annotations

import json

import httpx


async def read_bytes(client: httpx.AsyncClient, method: str, url: str, *, limit: int = 2_000_000, **kwargs):
    async with client.stream(method, url, **kwargs) as response:
        response.raise_for_status()
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > limit:
                raise ValueError("Remote response exceeds the size limit")
        return bytes(data)


async def read_json(client: httpx.AsyncClient, method: str, url: str, **kwargs):
    return json.loads(await read_bytes(client, method, url, **kwargs))
