"""Jede bekannte Frage muss bei der erwarteten Funktion landen.

Die Erkennung prüft viele Muster nacheinander; die Reihenfolge entscheidet. Dieser Test fällt auf,
wenn eine neue Funktion einer bestehenden Fragen wegnimmt. Die Liste steht in ``routing_cases.json``
(Kanal -> Frage -> Ziel). Ziele: Name der Datenquelle, ``ki`` für das Modell, ``direkt`` für eine
feste Antwort. Im Börsenkanal bedeutet ``get_quote`` auch „erst Kurssuche, sonst Modell“.

Nach einer gewollten Änderung: ``python -m tests.routing --update`` und den Diff der Liste prüfen.
"""

import json

import pytest

from tests.routing import CASES, make_assistant, route_of

KNOWN = json.loads(CASES.read_text(encoding="utf-8"))


@pytest.mark.parametrize("topic", sorted(KNOWN))
async def test_questions_reach_the_expected_function(topic):
    assistant = make_assistant()
    wrong = []
    for question, expected in KNOWN[topic].items():
        found = await route_of(assistant, question, "" if topic == "fragdenbot" else topic)
        if found != expected:
            wrong.append(f"{question!r}: erwartet {expected}, ist {found}")
    assert not wrong, "\n".join(wrong)


def test_list_covers_every_route_family():
    routes = {route for questions in KNOWN.values() for route in questions.values()}
    assert {"ki", "direkt", "get_weather", "get_departures", "get_journey", "get_flights", "get_quote",
            "get_fundamentals", "get_fuel_prices", "get_warnings", "get_grill_stands"} <= routes
    assert sum(len(questions) for questions in KNOWN.values()) >= 300
