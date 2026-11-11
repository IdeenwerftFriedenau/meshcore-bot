"""Live-Test der Alltagsdaten auf dem Pi: Warnungen, Störungen, Weltraumwetter, Fußball, Luft, Pollen,
Feiertage, Spritpreise. Liest nur, sendet nichts über Funk und schreibt nicht in die Bot-Datenbank."""

import asyncio
import sys

import httpx

from meshcore_ai_bot.alltag import FUEL_KEY, alltag_request, env_value
from meshcore_ai_bot.config import load_config
from meshcore_ai_bot.store import Store
from meshcore_ai_bot.tools import Tools

QUESTIONS = ["Gibt es Unwetterwarnungen?", "Störungen S-Bahn", "Fährt die S1?", "Fällt die U7 aus?", "Kp-Index",
             "Wie sind die Bandbedingungen?", "Wie hat Union gespielt?", "Wann spielt Hertha?", "Bundesliga Tabelle",
             "Wie ist die Luftqualität?", "Pollenflug", "Ist morgen Feiertag?", "Wann sind Weihnachtsferien?",
             "Wo ist Diesel am billigsten?", "Diesel in 12157", "Luft in Hamburg", "E10 Preise München"]


async def main() -> None:
    config = load_config(sys.argv[1] if len(sys.argv) > 1 else "/etc/meshcore-ai-bot/config.toml")
    print("Tankerkönig-Schlüssel:", "gesetzt" if env_value(FUEL_KEY) else "fehlt (Spritpreise sagen das dann)")
    ok = 0
    store = Store(":memory:")
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        tools = Tools(config, store, client)
        for question in QUESTIONS:
            route = alltag_request(question)
            if not route:
                print(f"FEHLER Weg  {question}")
                continue
            try:
                result = await tools.run(*route)
            except Exception as exc:  # noqa: BLE001 - every failure is reported, none stops the test
                print(f"FEHLER {question} -> {type(exc).__name__}: {exc}")
                continue
            print(f"OK  {question} -> {result['text']}  [{len(result['text'])} Zeichen]")
            ok += 1
    store.close()
    print(f"{ok}/{len(QUESTIONS)} Alltagsfragen beantwortet.")
    if ok == 0:
        sys.exit(1)


asyncio.run(main())
