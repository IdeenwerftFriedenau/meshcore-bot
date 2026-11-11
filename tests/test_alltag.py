"""Everyday data for #fragdenbot: warnings, disruptions, space weather, football, air, pollen, holidays, fuel."""

import json
from datetime import date, datetime

import httpx
import pytest

from meshcore_ai_bot import alltag
from meshcore_ai_bot.alltag import (
    air_quality,
    alltag_request,
    disruptions,
    football,
    fuel_prices,
    holidays,
    pollen,
    space_weather,
    warnings,
)

TZ = alltag.ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 11, 17, 0, tzinfo=TZ)
MS = 1000


def ms(moment: datetime) -> int:
    return int(moment.timestamp() * MS)


DWD = {"time": ms(NOW), "warnings": {"111000000": [
    {"state": "Berlin", "level": 2, "start": ms(datetime(2026, 11, 11, 20, 0, tzinfo=TZ)),
     "end": ms(datetime(2026, 11, 12, 10, 0, tzinfo=TZ)), "regionName": "Berlin",
     "headline": "Amtliche WARNUNG vor FROST", "description": "Es tritt leichter Frost um -2 °C auf."},
    {"state": "Berlin", "level": 2, "start": ms(datetime(2026, 11, 11, 20, 0, tzinfo=TZ)),
     "end": ms(datetime(2026, 11, 12, 10, 0, tzinfo=TZ)), "regionName": "Berlin-Mitte",
     "headline": "Amtliche WARNUNG vor FROST", "description": "Es tritt leichter Frost um -2 °C auf."},
    {"state": "Berlin", "level": 1, "start": ms(datetime(2026, 11, 10, 8, 0, tzinfo=TZ)),
     "end": ms(datetime(2026, 11, 10, 12, 0, tzinfo=TZ)), "regionName": "Berlin", "headline": "Alt"}]},
    "111000001": [{"state": "Brandenburg", "level": 3, "headline": "Sturm", "end": ms(NOW) + 10_000}]}
NINA = [{"id": "mow.1", "payload": {"data": {"provider": "MOWAS", "severity": "Severe", "msgType": "Alert",
                                             "headline": "Großbrand"}}, "i18nTitle": {"de": "Großbrand in Moabit"}},
        {"id": "dwd.1", "payload": {"data": {"provider": "DWD", "headline": "Frost"}}, "i18nTitle": {"de": "Frost"}}]


def board(*rows):
    return {"departures": list(rows)}


def dep(line, product="suburban", cancelled=False, remarks=()):
    return {"line": {"name": line, "product": product}, "cancelled": cancelled, "tripId": f"{line}-{len(remarks)}"
            f"-{cancelled}", "remarks": list(remarks)}


S1_WARNING = {"type": "warning", "text": "S1: Wegen einer Reparatur an einem Signal in Schönholz kommt es zu "
                                         "Verspätungen. Bitte benutzen Sie eine andere Verbindung."}
KP = [{"time_tag": "2026-11-11T09:00:00", "kp": 2.33, "observed": "observed"},
      {"time_tag": "2026-11-11T12:00:00", "kp": 1.0, "observed": "observed"},
      {"time_tag": "2026-11-12T00:00:00", "kp": 6.67, "observed": "predicted"}]
SCALES = {"0": {"G": {"Scale": "0"}, "R": {"Scale": "0"}, "S": {"Scale": "0"}},
          "1": {"R": {"MinorProb": "60"}, "G": {"Scale": "0"}}}
TABLE = [{"teamInfoId": 40, "teamName": "FC Bayern München", "shortName": "Bayern", "points": 21},
         {"teamInfoId": 80, "teamName": "1. FC Union Berlin", "shortName": "Union Berlin", "points": 7, "matches": 9,
          "won": 1, "draw": 4, "lost": 4, "goals": 7, "opponentGoals": 21}]


def match(t1, t2, when, finished, score=None):
    return {"team1": {"shortName": t1}, "team2": {"shortName": t2}, "matchDateTimeUTC": when,
            "matchIsFinished": finished, "group": {"groupName": "9. Spieltag"},
            "matchResults": [{"resultTypeID": 2, "pointsTeam1": score[0], "pointsTeam2": score[1]}] if score else []}


TEAM_MATCHES = [match("Freiburg", "Union Berlin", "2026-11-08T14:30:00Z", True, (2, 0)),
                match("Union Berlin", "Bielefeld", "2026-10-24T11:00:00Z", False),  # stale entry
                match("Union Berlin", "Leipzig", "2026-11-21T14:30:00Z", False)]
POLLEN = {"content": [{"region_id": 50, "partregion_id": -1, "region_name": "Brandenburg und Berlin",
                       "Pollen": {"Hasel": {"today": "0-1", "tomorrow": "1-2", "dayafter_to": "0"},
                                  "Erle": {"today": "0", "tomorrow": "0", "dayafter_to": "0"}}}]}
HOLI_PUBLIC = [{"startDate": "2026-12-25", "endDate": "2026-12-25", "name": [{"language": "DE",
                                                                               "text": "1. Weihnachtsfeiertag"}]},
               {"startDate": "2026-12-26", "endDate": "2026-12-26", "name": [{"language": "DE",
                                                                               "text": "2. Weihnachtsfeiertag"}]}]
HOLI_SCHOOL = [{"startDate": "2026-10-19", "endDate": "2026-10-31", "name": [{"language": "DE",
                                                                              "text": "Herbstferien"}]},
               {"startDate": "2026-12-23", "endDate": "2027-01-02", "name": [{"language": "DE",
                                                                              "text": "Weihnachtsferien"}]}]
STATIONS = {"ok": True, "stations": [
    {"brand": "Sprint", "street": "Körnerstr.", "houseNumber": "48a", "dist": 0.7, "diesel": 1.599, "e5": 1.759,
     "e10": 1.699, "isOpen": True},
    {"brand": "Aral", "street": "Hauptstr.", "houseNumber": "1", "dist": 1.2, "diesel": 1.579, "e5": 1.789,
     "e10": 1.729, "isOpen": True},
    {"brand": "Shell", "street": "Rheinstr.", "houseNumber": "2", "dist": 0.4, "diesel": 1.499, "isOpen": False}]}


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "warnings.json" in url:
        return httpx.Response(200, text="warnWetter.loadWarnings(" + json.dumps(DWD) + ");")
    if "warnung.bund.de" in url:
        return httpx.Response(200, json=NINA)
    if "/stops/900100003/" in url:
        return httpx.Response(200, json=board(dep("S1", remarks=[S1_WARNING]), dep("S5", cancelled=True),
                                              dep("ICE 552", "nationalExpress", True), dep("U5", "subway")))
    if "/stops/" in url:
        return httpx.Response(200, json=board(dep("S1"), dep("M4", "tram", cancelled=True)))
    if "k-index-forecast" in url:
        return httpx.Response(200, json=KP)
    if "noaa-scales" in url:
        return httpx.Response(200, json=SCALES)
    if "10cm-flux" in url:
        return httpx.Response(200, json=[{"flux": 132}])
    if "getbltable/bl1" in url:
        return httpx.Response(200, json=TABLE)
    if "getbltable" in url:
        return httpx.Response(200, json=[])
    if "getmatchesbyteamid/80" in url:
        return httpx.Response(200, json=TEAM_MATCHES)
    if "getmatchdata/bl1" in url:
        return httpx.Response(200, json=[match("HSV", "Gladbach", "2026-11-06T19:30:00Z", True, (3, 1))])
    if "s31fg" in url:
        return httpx.Response(200, json=POLLEN)
    if "air-quality" in url:
        return httpx.Response(200, json={"current": {"european_aqi": 49, "pm10": 21, "pm2_5": 18,
                                                     "nitrogen_dioxide": 41, "ozone": 18},
                                         "hourly": {"european_aqi": [49, 50, 75]}})
    if "PublicHolidays" in url:
        return httpx.Response(200, json=HOLI_PUBLIC)
    if "SchoolHolidays" in url:
        return httpx.Response(200, json=HOLI_SCHOOL)
    if "tankerkoenig" in url:
        assert request.url.params["apikey"] == KEY
        return httpx.Response(200, json=STATIONS)
    return httpx.Response(404)


KEY = "12345678-1234-1234-1234-123456789abc"


@pytest.fixture
def client():
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def no_env(monkeypatch, tmp_path):
    monkeypatch.delenv(alltag.FUEL_KEY, raising=False)
    monkeypatch.setattr(alltag, "ENV_FILE", str(tmp_path / "missing.env"))


@pytest.mark.asyncio
async def test_warnings_merge_dwd_and_nina(client):
    result = await warnings(client, now=NOW)
    text = result["text"]
    assert text.startswith("Amtliche Warnungen für Berlin: Großbrand in Moabit (BBK); ")
    assert "Warnung vor FROST (Stufe 2) ab heute 20 Uhr bis morgen 10 Uhr" in text
    assert text.count("FROST") == 1 and "Alt" not in text and "Sturm" not in text


@pytest.mark.asyncio
async def test_warnings_quiet_and_partial(client, monkeypatch):
    monkeypatch.setattr(alltag, "NINA", "https://example.invalid/{ars}")
    monkeypatch.setitem(DWD, "warnings", {})
    result = await warnings(client, now=NOW)
    assert result["text"] == "Für Berlin gibt es gerade keine amtlichen Warnungen (DWD und NINA) (NINA gerade " \
                             "nicht erreichbar)."


@pytest.mark.asyncio
async def test_disruptions_one_line_and_all(client):
    one = await disruptions(client, "https://bvg.test", "S1")
    assert one["text"] == "S1: Wegen einer Reparatur an einem Signal in Schönholz kommt es zu Verspätungen."
    calm = await disruptions(client, "https://bvg.test", "u5")
    assert calm["text"].startswith("U5: keine Störung gemeldet")
    everything = (await disruptions(client, "https://bvg.test"))["text"]
    assert everything.startswith("Störungen jetzt: S1: Wegen einer Reparatur")
    assert "S5: 1 Fahrt fällt aus" in everything and "M4: 1 Fahrt fällt aus" in everything
    assert "ICE" not in everything
    missing = await disruptions(client, "https://bvg.test", "X99")
    assert "sehe ich in der nächsten Stunde" in missing["text"]


@pytest.mark.asyncio
async def test_space_weather(client):
    text = (await space_weather(client, now=NOW))["text"]
    assert text.startswith("Weltraumwetter: Kp jetzt 1,0 (ruhig), letzte 24 Stunden max. 2,3")
    assert "Vorhersage bis Do max. 6,7 (morgen 1 Uhr)" in text
    assert "NOAA-Skalen G0 R0 S0, Funkausfall-Risiko (R1) morgen 60 %." in text
    assert "Solarer Flux 132, Kurzwelle ordentlich für 15–20 m." in text and "Polarlicht" in text


@pytest.mark.asyncio
async def test_football_team_and_league(client):
    union = (await football(client, "union", now=NOW))["text"]
    assert union.startswith("1. FC Union Berlin: Platz 2 in der Bundesliga, 7 Punkte aus 9 Spielen")
    assert "Zuletzt Freiburg–Union Berlin 2:0 (08.11.)." in union
    assert "Nächstes Spiel: Union Berlin–Leipzig Sa 21.11. 15:30 Uhr." in union  # the stale October entry is skipped
    league = (await football(client, now=NOW))["text"]
    assert league == "Bundesliga, Tabelle: 1. Bayern 21, 2. Union Berlin 7. 9. Spieltag: HSV–Gladbach 3:1."
    unknown = (await football(client, "Real Madrid", now=NOW))["text"]
    assert "finde ich" in unknown


@pytest.mark.asyncio
async def test_air_and_pollen(client):
    air = (await air_quality(client, 52.47, 13.33, "Friedenau"))["text"]
    assert air.startswith("Luft in Friedenau: mäßig (Europäischer Luftqualitätsindex 49)")
    assert "bis zu 75 (schlecht)" in air
    pollen_text = (await pollen(client))["text"]
    assert pollen_text == "Pollenflug Brandenburg und Berlin (DWD): heute Hasel kaum; morgen Hasel gering bis mittel."


@pytest.mark.asyncio
async def test_holidays(client):
    both = (await holidays(client, today=date(2026, 11, 11)))["text"]
    assert both.startswith("Nächster Feiertag in Berlin: 1. Weihnachtsfeiertag, Fr 25.12.2026 (in 44 Tagen)")
    assert "Nächste Schulferien: Weihnachtsferien vom 23.12. bis 02.01.2027." in both
    tomorrow = (await holidays(client, "feiertag", "morgen", today=date(2026, 11, 11)))["text"]
    assert tomorrow.startswith("Morgen ist in Berlin kein Feiertag.")
    christmas = (await holidays(client, "feiertag", "morgen", today=date(2026, 12, 24)))["text"]
    assert christmas.startswith("Morgen ist in Berlin Feiertag: 1. Weihnachtsfeiertag.")
    during = (await holidays(client, "ferien", today=date(2026, 10, 20)))["text"]
    assert during.startswith("Gerade sind Herbstferien in Berlin, bis 31.10.2026.")


@pytest.mark.asyncio
async def test_fuel_needs_key_then_lists_cheapest_open(client, monkeypatch):
    assert "noch nicht eingerichtet" in (await fuel_prices(client, 52.47, 13.33))["text"]
    monkeypatch.setenv(alltag.FUEL_KEY, KEY)
    diesel = (await fuel_prices(client, 52.47, 13.33, "diesel"))["text"]
    assert "Diesel ab 1,579 € bei Aral Hauptstr. 1 (1,2 km); Sprint Körnerstr. 1,599 €" in diesel
    assert "Shell" not in diesel  # closed
    every = (await fuel_prices(client, 52.47, 13.33))["text"]
    assert "Super E5 ab 1,759 €" in every and "Super E10 ab 1,699 €" in every


@pytest.mark.parametrize("question,tool,args", [
    ("Gibt es Unwetterwarnungen?", "get_warnings", {}),
    ("Hochwasser Spree?", "get_warnings", {}),
    ("Störungen S-Bahn", "get_transit_disruptions", {"line": ""}),
    ("Fährt die S1?", "get_transit_disruptions", {"line": "S1"}),
    ("Fällt die M41 aus?", "get_transit_disruptions", {"line": "M41"}),
    ("Ist die Ringbahn gestört?", "get_transit_disruptions", {"line": "S41"}),
    ("Fährt der Bus 248?", "get_transit_disruptions", {"line": "248"}),
    ("Kp-Index", "get_space_weather", {}),
    ("Gibt es heute Polarlicht?", "get_space_weather", {}),
    ("Wie sind die Bandbedingungen?", "get_space_weather", {}),
    ("Bundesliga Tabelle", "get_football", {"team": "", "league": ""}),
    ("Wie hat Union gespielt?", "get_football", {"team": "union", "league": ""}),
    ("Wann spielt St. Pauli", "get_football", {"team": "st. pauli", "league": ""}),
    ("Tabelle 2. Liga", "get_football", {"team": "", "league": "bl2"}),
    ("Wie ist die Luftqualität?", "get_air_quality", {}),
    ("Hab Heuschnupfen, wie sind die Pollen?", "get_pollen", {}),
    ("Ist morgen Feiertag?", "get_holidays", {"kind": "feiertag", "day": "morgen"}),
    ("Wann sind Herbstferien?", "get_holidays", {"kind": "ferien", "day": ""}),
    ("Wo ist Diesel am billigsten?", "get_fuel_prices", {"fuel": "diesel"}),
    ("Was kostet Super E10?", "get_fuel_prices", {"fuel": "e10"}),
])
def test_routing(question, tool, args):
    assert alltag_request(question) == (tool, args)


@pytest.mark.parametrize("question", [
    "Was ist ein Kp-Index?", "Warum gibt es Polarlicht?", "Wetter morgen", "Abfahrten S Friedenau",
    "Ich habe eine Störung am Funkgerät", "Was macht die Europäische Union?", "Alarm Rheinmetall unter 800",
    "Was ist ein Feiertag?",
])
def test_not_routed(question):
    assert alltag_request(question) is None
