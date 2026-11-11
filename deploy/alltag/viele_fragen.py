"""Großer Alltagstest: viele Formulierungen und Folgefragen über Assistant.answer (echte Quellen).

Sendet nichts über Funk und schreibt nicht in die Bot-Datenbank. Jede Frage hat das erwartete Werkzeug
(None = soll NICHT als Alltagsfrage behandelt werden) und ein Muster für die Antwort. „XX“ markiert Abweichungen.

    python viele_fragen.py /etc/meshcore-ai-bot/config.toml        # auf dem Pi, mit KI für den Rest
    python viele_fragen.py --ohne-ki                                # ohne Konfig und KI: nur Wege prüfen
"""

import asyncio
import re
import sys
import time

import httpx

from meshcore_ai_bot.alltag import ALLTAG_TOOLS
from meshcore_ai_bot.assistant import Assistant
from meshcore_ai_bot.config import Config, load_config
from meshcore_ai_bot.store import Store

ASK = "Welche Sorte"
FUEL = "get_fuel_prices"
# (Absender, Frage, erwartetes Werkzeug oder None, erwartete Argumente (Teilmenge) oder None, Muster)
CASES = [
    # ---------- Sprit: Sorte nachfragen, Orte, PLZ ----------
    ("a", "günstig tanken in 12157", None, None, ASK),
    ("a", "Diesel", FUEL, {"fuel": "diesel", "place": "12157"}, r"12157 Schöneberg.*Diesel ab"),
    ("b", "Wo kann ich billig tanken?", None, None, ASK),
    ("b", "E10", FUEL, {"fuel": "e10"}, r"Friedenau.*E10 ab"),
    ("c", "Tanken in Spandau", None, None, ASK),
    ("c", "alle", FUEL, {"fuel": "alle", "place": "Spandau"}, r"Spandau.*Diesel"),
    ("d", "wo tanke ich günstig", None, None, ASK),
    ("d", "super bitte", FUEL, {"fuel": "e5"}, r"E5 ab"),
    ("e", "günstig tanken", None, None, ASK),
    ("e", "Wie wird das Wetter morgen?", None, None, r"°"),
    ("f", "Spritpreise in Steglitz", FUEL, {"fuel": "alle", "place": "Steglitz"}, r"Steglitz"),
    ("f", "Was kostet Diesel in Potsdam?", FUEL, {"fuel": "diesel", "place": "Potsdam"}, r"Potsdam.*Diesel ab"),
    ("f", "Benzinpreis Hamburg", FUEL, {"fuel": "e5", "place": "Hamburg"}, r"Hamburg.*E5 ab"),
    ("f", "Diesel 10115", FUEL, {"fuel": "diesel", "place": "10115"}, r"10115 Mitte"),
    ("f", "Super E10 in Köln", FUEL, {"fuel": "e10", "place": "Köln"}, r"Köln.*E10 ab"),
    ("f", "Diesel bei Prenzlauer Berg", FUEL, {"fuel": "diesel", "place": "Prenzlauer Berg"}, r"Prenzlauer Berg"),
    ("f", "Diesel in Frankfurt am Main", FUEL, {"fuel": "diesel"}, r"Frankfurt"),
    ("f", "Diesel in Xyzqdorf", FUEL, {"fuel": "diesel"}, r"finde ich nicht"),
    ("f", "Diesel PLZ 14163", FUEL, {"fuel": "diesel", "place": "14163"}, r"14163 Zehlendorf"),
    ("f", "Wo ist E5 am billigsten?", FUEL, {"fuel": "e5"}, r"Friedenau.*E5 ab"),
    ("f", "Warum ist Diesel so teuer?", None, None, r"\w"),
    ("f", "Wie steht der Ölpreis?", None, None, r"Brent|WTI|Öl"),
    # ---------- Luft ----------
    ("g", "Luft in Hamburg", "get_air_quality", {"place": "Hamburg"}, r"Hamburg.*Luftqualitätsindex"),
    ("g", "Wie ist die Luft in Neukölln?", "get_air_quality", {"place": "Neukölln"}, r"Neukölln"),
    ("g", "Feinstaub heute", "get_air_quality", {}, r"Friedenau.*PM10"),
    ("g", "Luftqualität 80331", "get_air_quality", {"place": "80331"}, r"80331"),
    ("g", "Wie hoch ist die Ozonbelastung?", "get_air_quality", {}, r"Ozon"),
    # ---------- Warnungen ----------
    ("h", "Gibt es Unwetter?", "get_warnings", {}, r"Warnung"),
    ("h", "Warnungen Berlin", "get_warnings", {}, r"Warnung"),
    ("h", "Gibt es eine Sturmwarnung?", "get_warnings", {}, r"Warnung"),
    ("h", "Gibt es Glätte heute Nacht?", None, None, r"\w"),
    # ---------- Störungen ----------
    ("i", "Störungen U-Bahn", "get_transit_disruptions", {}, r"Störung|keine"),
    ("i", "Fährt die U9?", "get_transit_disruptions", {"line": "U9"}, r"^U9"),
    ("i", "Ist die M41 pünktlich?", "get_transit_disruptions", {"line": "M41"}, r"M41"),
    ("i", "Probleme auf der Ringbahn?", "get_transit_disruptions", {"line": "S41"}, r"S41"),
    ("i", "Fällt der 187 aus?", "get_transit_disruptions", {"line": "187"}, r"187"),
    ("i", "BVG Störungen", "get_transit_disruptions", {}, r"Störung|keine"),
    # ---------- Weltraumwetter ----------
    ("j", "Gibt es heute Polarlicht?", "get_space_weather", {}, r"Kp"),
    ("j", "Sonnensturm?", "get_space_weather", {}, r"Kp"),
    ("j", "Wie ist die Ausbreitung auf Kurzwelle?", "get_space_weather", {}, r"Kurzwelle|Flux"),
    ("j", "Was ist ein Kp-Index?", None, None, r"\w"),
    # ---------- Fußball ----------
    ("k", "Wie hat Hertha gespielt?", "get_football", {"team": "hertha"}, r"Hertha"),
    ("k", "Wann spielt Union?", "get_football", {}, r"Union"),
    ("k", "2. Liga Tabelle", "get_football", {"league": "bl2"}, r"2\. Bundesliga|Tabelle"),
    ("k", "Wie hat Bayern gespielt?", "get_football", {}, r"Bayern"),
    ("k", "Bundesliga Ergebnisse", "get_football", {}, r"Bundesliga"),
    ("k", "Wer ist Tabellenführer?", "get_football", {}, r"1\."),
    # ---------- Pollen ----------
    ("l", "Pollen morgen", "get_pollen", {}, r"Pollen"),
    ("l", "Fliegen Birkenpollen?", "get_pollen", {}, r"Pollen"),
    ("l", "Heuschnupfen heute?", "get_pollen", {}, r"Pollen"),
    # ---------- Feiertage, Ferien ----------
    ("m", "Wann ist der nächste Feiertag?", "get_holidays", {"kind": "feiertag"}, r"Feiertag"),
    ("m", "Ist heute Feiertag?", "get_holidays", {"day": "heute"}, r"Feiertag"),
    ("m", "Wann sind Osterferien?", "get_holidays", {"kind": "osterferien"}, r"Osterferien in Berlin"),
    ("m", "Wann sind Sommerferien?", "get_holidays", {"kind": "sommerferien"}, r"Sommerferien in Berlin"),
    ("m", "Brückentage 2026", "get_holidays", {}, r"Feiertag|Brückentag"),
    # ---------- darf NICHT als Alltag laufen ----------
    ("n", "Was ist ein Feiertag?", None, None, r"\w"),
    ("n", "Ich habe Heuschnupfen, was hilft?", None, None, r"\w"),
    ("n", "Wie spät ist es?", None, None, r"\d"),
]


async def main() -> None:
    no_ai = "--ohne-ki" in sys.argv
    paths = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    config = load_config(paths[0]) if paths else Config()
    config.research.enabled = True
    store = Store(":memory:")
    histories: dict[str, list[dict]] = {}
    ok = 0
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        assistant = Assistant(config, store, client)
        real_run = assistant.tools.run
        calls: list[tuple[str, dict]] = []

        async def recording_run(name, args, **kwargs):
            calls.append((name, dict(args)))
            return await real_run(name, args, **kwargs)

        assistant.tools.run = recording_run
        if no_ai:
            async def no_completion(*_args, **_kwargs):
                return {"choices": [{"message": {"role": "assistant", "content": "(KI)"}}]}
            assistant._completion = no_completion
        for sender, question, tool, args, pattern in CASES:
            calls.clear()
            history = histories.setdefault(sender, [])
            t = time.monotonic()
            try:
                answer = (await assistant.answer(question, history=list(history))).text
            except Exception as exc:  # noqa: BLE001 - every failure is reported, none stops the test
                answer = f"FEHLER {type(exc).__name__}: {exc}"
            history += [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
            del history[:-12]
            used = next(((n, a) for n, a in calls if n in ALLTAG_TOOLS), None)
            route_ok = (used is None) if tool is None else (
                used is not None and used[0] == tool and all(used[1].get(k, "") == v for k, v in (args or {}).items()))
            text_ok = no_ai or re.search(pattern, answer, re.S) is not None
            if no_ai and tool is None and pattern == ASK:
                text_ok = answer.startswith(ASK)
            good = route_ok and text_ok and not answer.startswith("FEHLER")
            ok += good
            way = f"{used[0]} {used[1]}" if used else "kein Alltagswerkzeug"
            print(f"{'OK ' if good else 'XX '}{question!r} ({time.monotonic() - t:.1f}s; {way}) -> "
                  f"{' '.join(answer.split())[:230]}", flush=True)
    store.close()
    print(f"\n{ok}/{len(CASES)} OK in {(time.monotonic() - started) / 60:.1f} min")


asyncio.run(main())
