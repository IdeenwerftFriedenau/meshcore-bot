"""Kennzahlen-Test auf dem Pi mit echten Quellen (SEC, Wikidata, Yahoo): kein Funk, keine KI."""

import asyncio
import sys

import httpx

from meshcore_ai_bot.fundamentals import fundamentals
from meshcore_ai_bot.markets import market_request

QUESTIONS = ["KGV von Nvidia", "Umsatz Apple", "Wie viel ist Microsoft wert", "Kennzahlen SAP",
             "Gewinn Siemens", "Umsatz und Gewinn Rheinmetall", "Dividende Allianz", "Vergleich KGV SAP und Apple",
             "Wer ist Chef von SAP?", "ISIN Siemens", "Wo sitzt Rheinmetall"]


async def main() -> None:
    ok = 0
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for question in QUESTIONS:
            route = market_request(question, channel=True)
            if not route or not route.get("facts"):
                print(f"FEHLER Weg  {question} -> {route}")
                continue
            try:
                result = await fundamentals(client, route["assets"], about=bool(route.get("about")))
            except Exception as exc:  # noqa: BLE001 - every failure is reported, none stops the test
                print(f"FEHLER {question} -> {type(exc).__name__}: {exc}")
                continue
            print(f"OK  {question} -> {result['text']}  [{len(result['text'])} Zeichen]")
            ok += 1
    print(f"{ok}/{len(QUESTIONS)} Kennzahlen-Fragen beantwortet.")
    if ok == 0:
        sys.exit(1)


asyncio.run(main())
