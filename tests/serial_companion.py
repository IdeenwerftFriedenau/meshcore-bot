"""Test-only companion wire emulator using a private POSIX pseudo terminal.

This is deliberately not a MeshCore mock: the real SDK opens the slave TTY,
frames commands, decodes replies, and dispatches events. Only DTR/RTS lines,
which pseudo terminals do not implement, are disabled by the test fixture.
"""

import asyncio
import contextlib
import os
import struct
import time
from collections import deque


class Clock:
    def __init__(self):
        self.offset = 0.0

    def monotonic(self):
        return time.monotonic() + self.offset

    def wall(self):
        return time.time() + self.offset

    async def sleep(self, seconds):
        self.offset += seconds
        await asyncio.sleep(0)


class Companion:
    def __init__(self, clock):
        self.clock = clock
        self.master, self.slave = os.openpty()
        self.port = os.ttyname(self.slave)
        os.set_blocking(self.master, False)
        self.buffer = bytearray()
        self.queue = asyncio.Queue()
        self.channels = {}
        self.commands = []
        self.transmissions = []
        self.pending = deque()
        self.tx_results = deque()
        self.fragmented = False
        self.junk = False
        self.closed = False
        self.name = "Simulated Companion"
        self.repeater_key = bytes.fromhex("42" * 32)
        self.advert_at = int(time.time()) - 3600
        self.worker = None
        self.failure = None
        self.tx_started = asyncio.Event()

    async def start(self):
        asyncio.get_running_loop().add_reader(self.master, self._read)
        self.worker = asyncio.create_task(self._serve())
        return self

    def _read(self):
        try:
            data = os.read(self.master, 65536)
        except (BlockingIOError, OSError):
            return
        self.buffer.extend(data)
        while len(self.buffer) >= 3:
            if self.buffer[0] != 0x3C:
                self.failure = AssertionError("Invalid host frame marker")
                self.buffer.clear()
                return
            size = int.from_bytes(self.buffer[1:3], "little")
            if len(self.buffer) < size + 3:
                return
            self.queue.put_nowait(bytes(self.buffer[3 : 3 + size]))
            del self.buffer[: 3 + size]

    async def frame(self, payload):
        data = b">" + struct.pack("<H", len(payload)) + payload
        if self.junk:
            data = b"console: simulator\r\n" + data
        if self.fragmented:
            for byte in data:
                os.write(self.master, bytes([byte]))
                await asyncio.sleep(0.0001)
        else:
            os.write(self.master, data)

    def contact(self, code=3):
        return (
            bytes([code])
            + self.repeater_key
            + bytes([2, 0, 255])
            + bytes(64)
            + b"Sim-Repeater".ljust(32, b"\0")
            + struct.pack("<IiiI", self.advert_at, 0, 0, int(time.time()))
        )

    def message(self, channel, sender, text, stamp=None):
        stamp = int(time.time()) if stamp is None else stamp
        # v3 channel receive reply including SNR, reserved fields, and timestamp.
        return bytes([17, 0, 0, 0, channel, 255, 0]) + struct.pack("<I", stamp) + f"{sender}: {text}".encode()

    async def incoming(self, channel, sender, text, stamp=None):
        self.pending.append(self.message(channel, sender, text, stamp))
        await self.frame(b"\x83")  # MESSAGES_WAITING: SDK must fetch the message.

    async def _serve(self):
        try:
            while True:
                data = await self.queue.get()
                self.commands.append(data)
                code = data[0]
                if code == 1:  # APP_START -> SELF_INFO
                    payload = (
                        bytes([5, 1, 10, 22])
                        + bytes.fromhex("24" * 32)
                        + bytes(12)
                        + struct.pack("<II", 869525, 250000)
                        + bytes([11, 5])
                        + self.name.encode()
                    )
                    await self.frame(payload)
                elif code == 31:  # GET_CHANNEL
                    index = data[1]
                    name, key = self.channels.get(index, ("", bytes(16)))
                    await self.frame(bytes([18, index]) + name.encode().ljust(32, b"\0") + key)
                elif code == 32:  # SET_CHANNEL
                    index = data[1]
                    name = data[2:34].split(b"\0")[0].decode()
                    self.channels[index] = (name, data[34:50])
                    await self.frame(b"\0")
                elif code == 4:  # GET_CONTACTS; several complete frames in one read are valid.
                    await self.frame(b"\x02" + struct.pack("<I", 1))
                    await self.frame(self.contact())
                    await self.frame(b"\x04" + struct.pack("<I", int(time.time())))
                elif code == 10:  # SYNC_NEXT_MESSAGE
                    await self.frame(self.pending.popleft() if self.pending else b"\x0a")
                elif code == 3:  # SEND_CHANNEL_TXT_MSG
                    tx = {
                        "channel": data[2],
                        "text": data[7:].decode(),
                        "started": self.clock.wall(),
                        "acknowledged": None,
                    }
                    self.transmissions.append(tx)
                    self.tx_started.set()
                    result, delay = self.tx_results.popleft() if self.tx_results else ("ok", 0)
                    await asyncio.sleep(delay)
                    if result == "silent":
                        continue
                    if result == "unplug":
                        self.unplug()
                        continue
                    tx["acknowledged"] = self.clock.wall()
                    await self.frame(b"\0" if result == "ok" else b"\x01\x01")
                else:
                    # A test must catch accidental RF adverts, remote queries, or secret export.
                    self.failure = AssertionError(f"Unexpected command {code}")
                    await self.frame(b"\x01\x01")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure = exc

    def unplug(self):
        if not self.closed:
            asyncio.get_running_loop().remove_reader(self.master)
            os.close(self.master)
            self.closed = True

    async def close(self):
        if self.worker:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker
        self.unplug()
        os.close(self.slave)
        if self.failure:
            raise self.failure
