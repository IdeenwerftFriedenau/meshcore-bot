"""Welche Funktion bekommt eine Frage? Ohne Netz und ohne KI.

``route_of`` lässt den echten ``Assistant.answer`` laufen und hält ihn an der ersten Stelle an,
an der er Daten holen oder das Modell fragen würde. So prüft der Test die tatsächliche Reihenfolge
der Erkennung und keine Kopie davon.

Liste nach einer gewollten Änderung neu schreiben:  python -m tests.routing --update
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from meshcore_ai_bot.assistant import Assistant
from meshcore_ai_bot.config import Channel, Config
from meshcore_ai_bot.store import Store

CASES = Path(__file__).with_name("routing_cases.json")
PROFILE = {"ort": ["Steglitz"], "haltestelle": ["S Friedenau"], "aktien": ["SAP"]}


class _Routed(BaseException):
    """Not an Exception: the assistant's own error handling must not swallow it."""


def _stop(label: str):
    raise _Routed(label)


def make_assistant() -> Assistant:
    config = Config(channels=[Channel("#fragdenbot", "all"), Channel("#börsenbot", "all", topic="boerse")])
    config.research.enabled = True
    config.weather.enabled = True
    config.ai._api_key = "test"
    assistant = Assistant(config, Store(":memory:"), None)

    async def run(name, arguments, **_):
        _stop(name)

    async def completion(messages, **_):
        _stop("ki")

    assistant.tools.run = run
    assistant._completion = completion
    return assistant


async def route_of(assistant: Assistant, question: str, topic: str = "") -> str:
    """'get_weather', 'get_quote', … for a data source, 'ki' for the model, 'direkt' for a fixed reply."""
    try:
        await assistant.answer(question, [], topic, PROFILE)
    except _Routed as routed:
        return str(routed)
    return "direkt"


async def all_routes(cases: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    assistant = make_assistant()
    return {
        topic: {question: await route_of(assistant, question, "" if topic == "fragdenbot" else topic)
                for question in questions}
        for topic, questions in cases.items()
    }


if __name__ == "__main__":
    known = json.loads(CASES.read_text(encoding="utf-8"))
    found = asyncio.run(all_routes(known))
    if "--update" in sys.argv:
        CASES.write_text(json.dumps(found, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print("Liste neu geschrieben. Bitte die Änderungen im Diff prüfen.")
    else:
        for topic, questions in found.items():
            for question, route in questions.items():
                if known[topic][question] != route:
                    print(f"{topic}: {question!r}: erwartet {known[topic][question]}, ist {route}")
