"""Daily source check: retries, status file, radio text. No network."""

import importlib.util
import json
import sqlite3
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "quellen_pruefen", Path(__file__).parents[1] / "deploy" / "quellen" / "pruefen.py"
)
pruefen = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pruefen)

CHECKS = [("Wetter (Open-Meteo)", "get_weather", {"location": "Berlin", "period": ""}, r"°"),
          ("Kurse (Yahoo Finance)", "get_quote", {"asset": "SAP"}, r"\d"),
          ("Pollen (DWD)", "get_pollen", {}, r"\w")]


class FakeTools:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    async def run(self, name, args):
        self.calls.append((name, args))
        answer = self.answers[name]
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, Exception):
            raise answer
        return {"text": answer}


async def nap(_seconds):
    return None


async def test_only_sources_that_fail_twice_are_reported():
    tools = FakeTools({"get_weather": "Berlin 8 °C", "get_quote": [ValueError("429"), "SAP 201,10 €"],
                       "get_pollen": "Die Pollendaten sind gerade nicht abrufbar."})
    failed = await pruefen.run_checks(tools, CHECKS, sleep=nap)
    assert list(failed) == ["Pollen (DWD)"]
    assert "Störung" in failed["Pollen (DWD)"]
    assert tools.calls[0] == ("get_weather", {"location": "Berlin"})  # empty arguments are left out
    assert [name for name, _ in tools.calls].count("get_quote") == 2


async def test_unexpected_and_empty_answers_count_as_broken():
    tools = FakeTools({"get_weather": "keine Zahl", "get_quote": "", "get_pollen": "Birke gering"})
    failed = await pruefen.run_checks(tools, CHECKS, sleep=nap)
    assert failed["Wetter (Open-Meteo)"].startswith("unerwartete Antwort")
    assert failed["Kurse (Yahoo Finance)"] == "leere Antwort"
    assert "Pollen (DWD)" not in failed


def test_status_file_keeps_the_first_failure_time_and_reports_changes(tmp_path):
    _status, changed = pruefen.report({"Pollen (DWD)": "x"}, 3, tmp_path, 100.0)
    assert changed
    status, changed = pruefen.report({"Pollen (DWD)": "y"}, 3, tmp_path, 200.0)
    assert not changed and status["since"] == {"Pollen (DWD)": 100.0}
    status, changed = pruefen.report({}, 3, tmp_path, 300.0)
    assert changed and status["failed"] == {}
    assert json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))["checked"] == 300.0


def test_radio_text_fits_one_packet_and_is_queued_like_the_console(tmp_path):
    many = {f"Quelle Nummer {i} (Anbieter)": "x" for i in range(20)}
    assert len(pruefen.radio_text(many).encode()) <= 110
    assert pruefen.radio_text({"Pollen (DWD)": "x"}) == "Quellenprüfung: gestört: Pollen"
    assert "wieder" in pruefen.radio_text({})
    outbox = tmp_path / "local-outbox.sqlite3"
    pruefen.queue_radio("#test", "Hallo", outbox, 5.0)
    with sqlite3.connect(outbox) as db:
        assert db.execute("SELECT channel,text,status FROM messages").fetchall() == [("#test", "Hallo", "queued")]


async def test_every_check_uses_a_tool_and_arguments_the_bot_accepts():
    from meshcore_ai_bot.config import Config
    from meshcore_ai_bot.store import Store
    from meshcore_ai_bot.tools import Tools

    config = Config()
    config.research.enabled = True
    tools = Tools(config, Store(":memory:"), None)
    for label, name, args, _pattern in pruefen.CHECKS:
        try:
            await tools.run(name, {k: v for k, v in args.items() if v != ""})
        except Exception as exc:  # noqa: BLE001 - without a client every call fails; only the reason matters
            assert "Unsupported tool" not in str(exc) and "Invalid tool" not in str(exc), label
