"""Whole-message delivery and user-controlled memory regressions; never open a radio."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from meshcore_ai_bot.config import Channel, Config
from meshcore_ai_bot.engine import Engine
from meshcore_ai_bot.protocol import Incoming
from meshcore_ai_bot.snapshot import publish, read
from meshcore_ai_bot.store import Store


def prepared(store, answer="Antwort", parts=None, sender="Alice", channel="#fragdenbot", stamp=None):
    now = int(time.time()) if stamp is None else stamp
    message = Incoming(channel, sender, "Frage " + str(now), now)
    store.remember(message, message.text, now)
    store.answer(message.identity, channel, answer, parts or ["Teil 1", "Teil 2"], now, now + 1000)
    return message


@pytest.mark.parametrize("failure", ["failed", "uncertain", "cancelled", "expired"])
def test_any_failed_part_stops_remaining_batch_only(failure):
    store = Store(":memory:")
    try:
        first = prepared(store)
        other = prepared(store, sender="Bob")
        packet = store.claim(time.time(), lambda *_: True)
        assert packet["message_id"] in {first.identity, other.identity}
        affected = packet["message_id"]
        store.finish(packet["id"], failure, time.time())
        assert store.message(affected)["status"] == "aborted"
        following = store.claim(time.time(), lambda *_: True)
        assert following and following["message_id"] != affected
        assert not any(item["role"] == "assistant" for item in store.history(first.channel, "Alice", 0, 12))
    finally:
        store.close()


def test_memory_answer_only_after_every_packet_accepted_and_survives_restart(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    message = prepared(store)
    assert [item["role"] for item in store.history(message.channel, message.sender, 0, 12)] == ["user"]
    packet = store.claim(time.time(), lambda *_: True)
    store.finish(packet["id"], "accepted", time.time())
    assert [item["role"] for item in store.history(message.channel, message.sender, 0, 12)] == ["user"]
    packet = store.claim(time.time(), lambda *_: True)
    store.finish(packet["id"], "accepted", time.time())
    assert store.message(message.identity)["status"] == "accepted"
    store.close()
    store = Store(path)
    try:
        assert store.history(message.channel, message.sender, 0, 12)[-1] == {
            "role": "assistant",
            "content": "Antwort",
        }
    finally:
        store.close()


def test_crash_after_first_part_stops_suffix_and_does_not_replay(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    message = prepared(store)
    packet = store.claim(time.time(), lambda *_: True)
    store.finish(packet["id"], "accepted", time.time())
    store.claim(time.time(), lambda *_: True)
    store.close()
    store = Store(path)
    try:
        store.recover()
        assert store.claim(time.time(), lambda *_: True) is None
        assert store.counts() == {"accepted": 1, "uncertain": 1}
        assert store.message(message.identity)["status"] == "aborted"
        assert not any(item["role"] == "assistant" for item in store.history(message.channel, "Alice", 0, 12))
    finally:
        store.close()


class Answerer:
    def __init__(self):
        self.histories = []

    async def answer(self, question, history):
        self.histories.append(history)
        return SimpleNamespace(text="Eine normale Antwort.")


async def invoke(engine, text, sender="Alice", channel="#fragdenbot"):
    # Different text keeps test identities unique without a real radio clock.
    assert engine.receive(Incoming(channel, sender, text, int(time.time()))) == "queued"
    await engine.process_one()
    return engine.store.db.execute("SELECT answer FROM messages ORDER BY rowid DESC LIMIT 1").fetchone()[0]


async def test_forget_inspect_and_opt_in_are_local_and_persistent(tmp_path):
    config = Config()
    config.radio.incoming_per_sender_per_minute = 10
    config.radio.incoming_per_minute = 60
    path = tmp_path / "memory.db"
    store = Store(path)
    assistant = Answerer()
    engine = Engine(config, store, assistant)
    await invoke(engine, "Ich wohne in Berlin.")
    inspection = await invoke(engine, "Was weißt du über mich?")
    assert "Ich wohne in Berlin." in inspection and "Wohnort: Berlin" in inspection
    assert len(assistant.histories) == 0  # a plain self-description is confirmed without the model
    assert "gelöscht" in await invoke(engine, "Vergiss mich")
    assert store.db.execute("SELECT count(*) FROM memory").fetchone()[0] == 0
    assert store.db.execute("SELECT count(*) FROM messages WHERE raw_text LIKE '%Berlin%'").fetchone()[0] == 0
    assert store.db.execute("SELECT count(*) FROM packets WHERE text LIKE '%Berlin%'").fetchone()[0] == 0
    store.close()
    store = Store(path)
    engine = Engine(config, store, assistant)
    try:
        await invoke(engine, "Ich wohne jetzt in Bonn.")
        assert store.db.execute("SELECT count(*) FROM memory").fetchone()[0] == 0
        await invoke(engine, "memory an")
        await invoke(engine, "Ich mag Radfahren.")
        assert store.db.execute("SELECT count(*) FROM memory").fetchone()[0] == 1
    finally:
        store.close()


async def test_private_and_disabled_channels_never_leak_into_shared_context():
    config = Config(
        channels=[
            Channel("Privat", "all"),
            Channel("#fragdenbot", "all"),
            Channel("#off", "all", memory_scope="off"),
        ]
    )
    config.radio.incoming_per_sender_per_minute = 10
    store = Store(":memory:")
    assistant = Answerer()
    engine = Engine(config, store, assistant)
    try:
        await invoke(engine, "Geheimer privater Wohnort", channel="Privat")
        await invoke(engine, "Nicht merken", channel="#off")
        await invoke(engine, "Öffentliche Frage")
        assert assistant.histories[-1] == []
        config.channels[0].memory_scope = "shared"
        await invoke(engine, "Nächste öffentliche Frage")
        assert any("Geheimer" in item["content"] for item in assistant.histories[-1])
        assert not any("Nicht merken" in item["content"] for item in assistant.histories[-1])
    finally:
        store.close()


async def test_diagnostics_explain_limits_without_publishing_chat_text(tmp_path):
    config = Config()
    config.radio.incoming_per_sender_per_minute = 1
    store = Store(tmp_path / "state.db")
    engine = Engine(config, store, Answerer())
    try:
        await invoke(engine, "Vertrauliche Frage")
        assert (
            engine.receive(Incoming("#fragdenbot", "Alice", "Noch eine Frage", int(time.time()))) == "limited"
        )
        engine.receive(Incoming("#fragdenbot", "Bob", "x", int(time.time()) - 5000))
        publish(store)
        snapshot = read(store.path)
        assert "Vertrauliche Frage" not in str(snapshot)
        decisions = [entry["decision"] for entry in snapshot["reception"]]
        assert "limited" in decisions and "ignored" in decisions
        prepared_entry = next(entry for entry in snapshot["reception"] if entry["status"] == "prepared")
        assert prepared_entry["answered_at"] and prepared_entry["packets"]
    finally:
        store.close()


async def test_full_disk_answer_transaction_recovers_without_partial_packets():
    store = Store(":memory:")
    config = Config()
    engine = Engine(config, store, Answerer())
    import sqlite3

    original = store._insert_parts

    def full_disk(*args, **kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    try:
        engine.receive(Incoming("#fragdenbot", "Alice", "Frage bei vollem Datenträger", int(time.time())))
        store._insert_parts = full_disk
        with pytest.raises(sqlite3.OperationalError):
            await engine.process_one()
        assert store.counts() == {}
        store._insert_parts = original
        store.recover()
        await engine.process_one()
        assert store.counts()["queued"] >= 1
    finally:
        store.close()


async def test_policy_change_during_pacing_cancels_whole_reply():
    config = Config()
    store = Store(":memory:")
    entered = asyncio.Event()
    proceed = asyncio.Event()

    class Waiting:
        def received(self):
            pass

        async def transmit(self, send, valid):
            entered.set()
            await proceed.wait()
            return await send() if valid() else None

    class Transport:
        async def send(self, *args):
            raise AssertionError("Changed policy must prevent RF")

    engine = Engine(config, store, Answerer(), pacer=Waiting())
    try:
        await invoke(engine, "Frage vor Regelwechsel")
        task = asyncio.create_task(engine.deliver_one(Transport()))
        await entered.wait()
        config.channels[0].mode = "off"
        proceed.set()
        await task
        assert not store.counts().get("queued")
        assert store.db.execute("SELECT status FROM messages").fetchone()[0] == "aborted"
    finally:
        store.close()


def test_forget_matches_unicode_names_and_clears_queued_text():
    store = Store(":memory:")
    try:
        prepared(store, answer="Privater Text", parts=["Privater Text"], sender="Änne")
        store.forget_memory("änne", time.time())
        assert store.db.execute("SELECT raw_text,answer FROM messages").fetchone()[:] == ("", "")
        assert store.db.execute("SELECT text FROM packets").fetchone()[0] == ""
        assert store.counts() == {"cancelled": 1}
    finally:
        store.close()


async def test_asa_shutdown_waits_for_the_database_worker(monkeypatch):
    import threading

    from meshcore_ai_bot.radio import poll_asa_once

    started, release = threading.Event(), threading.Event()
    store = Store(":memory:")

    def slow_poll(config, state):
        started.set()
        assert release.wait(2)
        state.meta_set("worker:completed", 1)

    monkeypatch.setattr("meshcore_ai_bot.radio.asa.poll", slow_poll)
    task = asyncio.create_task(poll_asa_once(Config(), store))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.meta_get("worker:completed") == "1"
    finally:
        release.set()
        store.close()
