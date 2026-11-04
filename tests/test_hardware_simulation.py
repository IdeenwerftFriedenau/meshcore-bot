"""Real SDK + real serial transport + virtual companion; no physical USB/RF."""

import asyncio
import re
import time
from types import SimpleNamespace

import httpx
import pytest

from meshcore_ai_bot.assistant import Assistant
from meshcore_ai_bot.config import Channel, Config
from meshcore_ai_bot.engine import Engine, Pacer
from meshcore_ai_bot.protocol import Incoming
from meshcore_ai_bot.radio import RadioTransport, channel_key
from meshcore_ai_bot.store import Store

from .serial_companion import Clock, Companion

# The radio extra is optional for core-only installs. CI always installs .[dev,radio].
pytest.importorskip("meshcore")


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():  # noqa: ASYNC110 -- bounded observation of SDK callback/SQLite state
            await asyncio.sleep(0.005)


@pytest.fixture
async def rig(monkeypatch, tmp_path):
    import serial
    from meshcore import MeshCore

    # PTYs have no modem control pins. The framing/parser/transport stay unmodified.
    monkeypatch.setattr(serial.Serial, "_update_dtr_state", lambda self: None)
    monkeypatch.setattr(serial.Serial, "_update_rts_state", lambda self: None)
    clock = Clock()
    device = await Companion(clock).start()
    config = Config(
        channels=[
            Channel("Public"),
            Channel("#fragdenbot", "all"),
            Channel("#bot", "mention", mentions=["Ideenwerft"]),
        ]
    )
    config.radio.port = device.port
    config.storage.path = str(tmp_path / "state.sqlite3")
    for index, ch in enumerate(config.channels):
        device.channels[index] = (ch.name, channel_key(ch))
    # Fail closed: this test cannot open an existing physical serial port by mistake.
    original = MeshCore.create_serial

    async def private_pty_only(port, **kwargs):
        assert port == device.port
        return await original(port, **kwargs)

    monkeypatch.setattr(MeshCore, "create_serial", private_pty_only)
    store = Store(config.storage.path)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("No HTTP in simulation")))
    pacer = Pacer(config, monotonic=clock.monotonic, wall=clock.wall, sleep=clock.sleep, jitter=lambda: 0)
    engine = Engine(config, store, Assistant(config, store, client, offline=True), pacer=pacer)
    transport = RadioTransport(config, store, engine)
    state = SimpleNamespace(
        config=config, store=store, device=device, clock=clock, engine=engine, transport=transport
    )
    try:
        yield state
    finally:
        await transport.disconnect()
        await client.aclose()
        store.close()
        await device.close()


async def queue_question(rig, text="ping"):
    stamp = int(time.time())
    msg = Incoming("#fragdenbot", "Tester", text, stamp)
    assert rig.engine.receive(msg) == "queued"
    assert await rig.engine.process_one()
    return msg


async def test_real_sdk_handshake_and_passive_catalogue(rig):
    await rig.transport.connect()
    assert rig.transport.mc.self_info["name"] == rig.device.name
    assert rig.transport.indices == {0: "Public", 1: "#fragdenbot", 2: "#bot"}
    assert rig.device.name.casefold() in rig.engine.own_names
    assert not rig.device.transmissions  # Opening/reading does not produce an RF greeting.
    rows = rig.store.repeaters()
    assert len(rows) == 1 and rows[0]["advert"] == rig.device.advert_at
    # Catalogue refresh time is not fabricated as a fresh over-the-air advert.
    assert rows[0]["advert"] < rows[0]["checked"] - 3000


async def test_wire_receive_duplicate_and_own_echo(rig):
    await rig.transport.connect()
    stamp = int(time.time())
    await rig.device.incoming(1, "Tester", "ping", stamp)
    await eventually(lambda: rig.store.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1)
    assert await rig.engine.process_one()
    assert await rig.engine.deliver_one(rig.transport)
    assert "Pong" in rig.device.transmissions[0]["text"]
    assert rig.store.counts() == {"accepted": 1}
    await rig.device.incoming(1, "Tester", "ping", stamp)
    await rig.device.incoming(1, rig.device.name, "ping", stamp)
    await eventually(lambda: not rig.device.pending)
    await asyncio.sleep(0.03)
    assert rig.store.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
    assert not await rig.engine.process_one()
    assert not await rig.engine.deliver_one(rig.transport)


@pytest.mark.parametrize(
    "channel,sender,body",
    [(0, "Tester", "KI ping"), (2, "KI", "ping"), (2, "Tester", "Kiste"), (15, "Tester", "KI ping")],
)
async def test_wire_channel_filters(rig, channel, sender, body):
    await rig.transport.connect()
    before = len(rig.device.commands)
    await rig.device.incoming(channel, sender, body)
    await eventually(lambda: any(c[0] == 10 for c in rig.device.commands[before:]))
    await asyncio.sleep(0.03)
    assert not await rig.engine.process_one()
    assert not await rig.engine.deliver_one(rig.transport)
    assert not rig.device.transmissions


async def test_split_frames_and_uart_console_noise(rig):
    rig.device.fragmented = True
    rig.device.junk = True
    await rig.transport.connect()
    assert len(rig.store.repeaters()) == 1
    await queue_question(rig)
    assert await rig.engine.deliver_one(rig.transport)
    assert rig.store.counts() == {"accepted": 1}


async def test_delayed_local_ack_and_complete_utf8_parts(rig):
    await rig.transport.connect()
    answer = "Grüße vom simulierten Repeater 🙂. " * 8

    class AssistantStub:
        async def answer(self, *_):
            return SimpleNamespace(text=answer)

    rig.engine.assistant = AssistantStub()
    rig.device.tx_results.append(("ok", 0.04))
    await queue_question(rig, "Mehrteilige Antwort bitte")
    count = rig.store.counts()["queued"]
    assert count > 1
    for _ in range(count):
        assert await rig.engine.deliver_one(rig.transport)
    transmissions = rig.device.transmissions
    assert len(transmissions) == count
    assert all(len(tx["text"].encode()) <= 110 for tx in transmissions)
    rebuilt = "".join(re.sub(r"^\[\d+/\d+\] ", "", tx["text"]) for tx in transmissions)
    assert rebuilt == "@[Tester] " + answer
    for first, second in zip(transmissions, transmissions[1:], strict=False):
        assert second["started"] - first["acknowledged"] >= rig.config.radio.send_interval_seconds - 0.01
    assert rig.store.counts() == {"accepted": count}


async def test_device_rejection_is_not_local_acceptance(rig):
    await rig.transport.connect()
    rig.device.tx_results.append(("reject", 0))
    await queue_question(rig)
    assert await rig.engine.deliver_one(rig.transport)
    assert rig.store.counts() == {"failed": 1}
    assert not await rig.engine.deliver_one(rig.transport)
    assert len(rig.device.transmissions) == 1


@pytest.mark.parametrize("failure", ["silent", "unplug"])
async def test_missing_ack_is_uncertain_not_retried(rig, failure):
    await rig.transport.connect()
    rig.transport.mc.commands.default_timeout = 0.08
    rig.device.tx_results.append((failure, 0))
    await queue_question(rig)
    assert await rig.engine.deliver_one(rig.transport)
    assert rig.store.counts() == {"uncertain": 1}
    assert rig.transport.needs_reconnect
    assert len(rig.device.transmissions) == 1
    rig.store.recover()
    assert not await rig.engine.deliver_one(rig.transport)


async def test_cancel_mid_send_and_reopen_database(rig):
    await rig.transport.connect()
    rig.device.tx_results.append(("silent", 0))
    await queue_question(rig)
    task = asyncio.create_task(rig.engine.deliver_one(rig.transport))
    await asyncio.wait_for(rig.device.tx_started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rig.store.counts() == {"uncertain": 1}
    assert rig.transport.needs_reconnect
    # Read the on-disk state through a new connection, as after a process restart.
    reopened = Store(rig.config.storage.path)
    try:
        reopened.recover()
        assert reopened.counts() == {"uncertain": 1}
        assert reopened.claim(time.time(), lambda *_: True) is None
        assert reopened.last_tx() is not None
    finally:
        reopened.close()


async def test_reconnect_does_not_resend_accepted_packet(rig):
    await rig.transport.connect()
    await queue_question(rig)
    assert await rig.engine.deliver_one(rig.transport)
    await rig.transport.disconnect()
    rig.store.recover()
    await rig.transport.connect()
    assert not await rig.engine.deliver_one(rig.transport)
    assert len(rig.device.transmissions) == 1


async def test_received_advert_updates_only_known_repeater(rig):
    await rig.transport.connect()
    before = rig.store.repeaters()[0]["advert"]
    await rig.device.frame(b"\x80" + rig.device.repeater_key)
    await eventually(lambda: rig.store.repeaters()[0]["advert"] > before)
    assert not rig.device.transmissions


async def test_keyword_on_wire_accepts_only_explicit_invocation(rig):
    await rig.transport.connect()
    await rig.device.incoming(2, "Tester", "@Ideenwerft ping")
    await eventually(lambda: rig.store.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1)
    assert await rig.engine.process_one()
    assert await rig.engine.deliver_one(rig.transport)
    assert rig.device.transmissions[0]["channel"] == 2
    assert "Pong" in rig.device.transmissions[0]["text"]


async def test_wrong_key_stops_startup_without_rewriting_or_sending(rig):
    rig.device.channels[1] = ("#fragdenbot", bytes.fromhex("ff" * 16))
    with pytest.raises(RuntimeError, match="mismatch"):
        await rig.transport.connect()
    assert all(command[0] != 32 for command in rig.device.commands)
    assert not rig.device.transmissions


async def test_provision_creates_only_free_slot_on_wire(rig):
    del rig.device.channels[1]
    await rig.transport.connect(provision=True)
    assert rig.device.channels[1] == ("#fragdenbot", channel_key(rig.config.channels[1]))
    assert len([c for c in rig.device.commands if c[0] == 32]) == 1
    assert not rig.device.transmissions


async def test_late_ack_session_reconnects_before_next_send(rig):
    await rig.transport.connect()
    rig.transport.mc.commands.default_timeout = 0.04
    rig.device.tx_results.append(("ok", 0.15))
    await queue_question(rig)
    assert await rig.engine.deliver_one(rig.transport)
    assert rig.store.counts() == {"uncertain": 1}
    assert rig.transport.needs_reconnect
    # Block reuse of the old session even if a late OK is now arriving.
    with pytest.raises(ConnectionError, match="reconnect"):
        await rig.transport.send("#fragdenbot", "Must not reach serial")
    await rig.transport.disconnect()
    await asyncio.sleep(0.16)
    await rig.transport.connect()
    assert not rig.transport.needs_reconnect
    await queue_question(rig, "test 123")
    assert await rig.engine.deliver_one(rig.transport)
    assert rig.store.counts() == {"accepted": 1, "uncertain": 1}
    assert len(rig.device.transmissions) == 2


async def test_stale_wire_message_not_answered(rig):
    await rig.transport.connect()
    before = len(rig.device.commands)
    await rig.device.incoming(1, "Tester", "ping", int(time.time()) - 1000)
    await eventually(lambda: any(c[0] == 10 for c in rig.device.commands[before:]))
    await asyncio.sleep(0.03)
    assert not await rig.engine.process_one()
    assert not rig.device.transmissions


async def test_daemon_reconnects_after_missing_ack_without_replaying(rig, monkeypatch):
    from meshcore import MeshCore

    import meshcore_ai_bot.radio as radio_module

    original_factory = MeshCore.create_serial

    async def short_timeout(port, **kwargs):
        kwargs["default_timeout"] = 0.05
        return await original_factory(port, **kwargs)

    def engine_with_test_clock(config, store, assistant):
        pacer = Pacer(
            config,
            store.last_tx(),
            monotonic=rig.clock.monotonic,
            wall=rig.clock.wall,
            sleep=rig.clock.sleep,
            jitter=lambda: 0,
        )
        return Engine(config, store, assistant, pacer=pacer)

    async def supervisor_sleep(seconds):
        # Accelerate the supervisor's polling and 10-second reconnect backoff only.
        # The real SDK, serial transport and dispatcher use the real asyncio module.
        await asyncio.sleep(0 if seconds == 10 else 0.005)

    monkeypatch.setattr(MeshCore, "create_serial", short_timeout)
    monkeypatch.setattr(radio_module, "Engine", engine_with_test_clock)
    monkeypatch.setattr(
        radio_module,
        "asyncio",
        SimpleNamespace(
            create_task=asyncio.create_task,
            Lock=asyncio.Lock,
            CancelledError=asyncio.CancelledError,
            TaskGroup=asyncio.TaskGroup,
            sleep=supervisor_sleep,
        ),
    )
    rig.device.tx_results.append(("silent", 0))
    rig.device.pending.append(rig.device.message(1, "Tester", "ping"))
    task = asyncio.create_task(radio_module.run_radio(rig.config))
    try:
        await eventually(lambda: sum(c[0] == 1 for c in rig.device.commands) >= 2)
        await eventually(lambda: sum(c[0] == 4 for c in rig.device.commands) >= 3)
        assert rig.store.counts() == {"uncertain": 1}
        assert len(rig.device.transmissions) == 1
        await rig.device.incoming(1, "Tester", "test 123")
        await eventually(lambda: rig.store.counts().get("accepted") == 1)
        assert rig.store.counts() == {"accepted": 1, "uncertain": 1}
        assert len(rig.device.transmissions) == 2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
