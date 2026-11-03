"""CLI defaults to simulation. Real radio access requires --live or provision."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

from . import __version__
from .assistant import Assistant
from .config import load_config
from .engine import Engine
from .protocol import Incoming
from .store import Store


def parser():
    root = argparse.ArgumentParser(
        prog="meshcore-ai", description="Standalone MeshCore AI bot for Raspberry Pi"
    )
    root.add_argument("--version", action="version", version=__version__)
    sub = root.add_subparsers(dest="command", required=True)
    for name in ["check", "demo", "replay", "run", "provision", "status", "prune", "dashboard", "asa-ingest"]:
        command = sub.add_parser(name)
        command.add_argument("--config", default="config.toml")
        if name == "asa-ingest":
            command.add_argument("event", type=Path)
        if name == "demo":
            command.add_argument("text")
            command.add_argument("--channel", default="#fragdenbot")
            command.add_argument("--sender", default="Demo")
            command.add_argument("--offline", action="store_true")
        if name == "replay":
            command.add_argument("fixture", type=Path)
            command.add_argument("--offline", action="store_true")
        if name == "run":
            command.add_argument(
                "--live", action="store_true", help="Actually connect to USB and transmit replies"
            )
    return root


async def simulate(config, cases, *, offline: bool):
    store = Store(":memory:")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            assistant = Assistant(config, store, client, offline=offline)
            engine = Engine(config, store, assistant)
            results = []
            for index, case in enumerate(cases):
                sender = case.get("sender", "Demo" + str(index))
                received = engine.receive(
                    Incoming(case.get("channel", "#fragdenbot"), sender, case["text"], int(time.time()))
                )
                await engine.process_one()
                # Simulation NEVER calls deliver_one and NEVER imports a MeshCore connection.
                with store.lock:
                    rows = store.db.execute(
                        "SELECT text FROM packets WHERE state='queued' ORDER BY rowid"
                    ).fetchall()
                packets = [row[0] for row in rows]
                results.append(
                    {
                        "input": case["text"],
                        "channel": case.get("channel", "#fragdenbot"),
                        "decision": received,
                        "packets": packets,
                        "passed": (bool(packets) == case["expected_reply"])
                        if "expected_reply" in case
                        else True,
                    }
                )
                with store.transaction() as c:
                    c.execute("UPDATE packets SET state='cancelled' WHERE state='queued'")
            return results
    finally:
        store.close()


async def async_main(args, config):
    if args.command == "demo":
        print(
            json.dumps(
                await simulate(
                    config,
                    [{"text": args.text, "channel": args.channel, "sender": args.sender}],
                    offline=args.offline,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "replay":
        cases = json.loads(args.fixture.read_text())
        results = await simulate(config, cases, offline=args.offline)
        print(json.dumps(results, ensure_ascii=False, indent=2))
        if not all(result["passed"] for result in results):
            return 1
    elif args.command == "run" and args.live:
        from .radio import run_radio

        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        if hasattr(signal, "SIGTERM"):
            loop.add_signal_handler(signal.SIGTERM, task.cancel)
        try:
            await run_radio(config, config_path=args.config)
        except asyncio.CancelledError:
            pass
    elif args.command == "provision":
        from .radio import RadioTransport, instance_lock

        with instance_lock(config.storage.path):
            store = Store(config.storage.path)
            transport = RadioTransport(config, store)
            try:
                await transport.connect(provision=True)
                print(json.dumps(transport.indices, ensure_ascii=False, indent=2))
            finally:
                await transport.disconnect()
                store.close()
    elif args.command in {"status", "prune"}:
        store = Store(config.storage.path)
        try:
            if args.command == "prune":
                store.prune(time.time(), config.storage.retention_hours)
            print(
                json.dumps(
                    {
                        "packets": store.counts(),
                        "known_repeaters": len(store.repeaters()),
                        "last_local_tx": store.last_tx(),
                    },
                    indent=2,
                )
            )
        finally:
            store.close()
    else:
        print(
            json.dumps(
                {
                    "valid": True,
                    "radio_opened": False,
                    "name": config.station.name,
                    "model": config.ai.model,
                    "api_key_configured": bool(config.ai.api_key),
                    "channels": [
                        {"name": ch.name, "mode": ch.mode, "broadcasts": ch.broadcasts}
                        for ch in config.channels
                    ],
                    "send_interval_seconds": config.radio.send_interval_seconds,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if args.command == "run":
            print("Simulation only. Use run --live to connect to a radio.")
    return 0


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    path = Path(args.config).expanduser().resolve()
    env = path.parent / ".env"
    if env.exists() and os.access(env, os.R_OK):
        load_dotenv(env, override=False)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(path)
        if args.command == "dashboard":
            from .dashboard import serve

            serve(path)
            return 0
        if args.command == "asa-ingest":
            from .asa import ingest

            with args.event.open("rb") as stream:
                print(ingest(config, stream.read(65537)))
            return 0
        return asyncio.run(async_main(args, config))
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Configuration/runtime error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
