"""Amateur-radio reading aids; never change channel permissions or station settings.
References: RSGB RadCom abbreviations; ARRL Communicating with Other Hams Q Signals;
OeVSV/DARC wiki Abkuerzungen. Definitions describe usage, not live measurements.
"""

import re
from datetime import UTC

TERMS = {
    "WX": "Wetter",
    "QTH": "Stationsstandort",
    "QRA": "Stationsname",
    "QRG": "Frequenzangabe",
    "QRL": "Frequenz belegt / beschäftigt",
    "QRM": "Störungen durch andere Sender",
    "QRN": "atmosphärische Störungen",
    "QRO": "Sendeleistung erhöhen",
    "QRP": "Sendeleistung verringern / Kleinleistungsbetrieb",
    "QRQ": "schneller geben",
    "QRS": "langsamer geben",
    "QRT": "Sendebetrieb beenden",
    "QRU": "keine weiteren Nachrichten",
    "QRV": "betriebsbereit",
    "QRX": "warten / späterer Rückruf",
    "QRZ": "wer ruft?",
    "QSB": "schwankende Signalstärke / Fading",
    "QSK": "Zwischenhören beim Senden",
    "QSL": "Empfang bestätigen / Empfangsbestätigung",
    "QSO": "Funkverbindung / Funkgespräch",
    "QSP": "Nachricht weiterleiten",
    "QST": "Nachricht an alle Stationen",
    "QSX": "auf angegebener Frequenz hören",
    "QSY": "Frequenz wechseln",
    "QTC": "Nachrichten übermitteln / Anzahl der Nachrichten",
    "QTR": "Uhrzeit",
    "CQ": "allgemeiner Anruf",
    "DE": "von / hier ist, vor einem Rufzeichen",
    "DX": "Fernverbindung / weit entfernte Station",
    "RST": "Lesbarkeit, Signalstärke, Tonqualität",
    "RPRT": "Empfangsrapport",
    "RX": "Empfänger / Empfang",
    "TX": "Sender / Senden",
    "TRX": "Sendeempfänger",
    "RIG": "Funkgerät / Stationstechnik",
    "ANT": "Antenne",
    "PWR": "Sendeleistung",
    "OM": "Funkfreund / Funkamateur",
    "YL": "Funkamateurin",
    "XYL": "Ehefrau, im Funkjargon",
    "SWL": "Kurzwellenhörer",
    "HAM": "Funkamateur",
    "AFU": "Amateurfunk",
    "TNX": "danke",
    "TKS": "danke",
    "TU": "danke",
    "PSE": "bitte",
    "AGN": "noch einmal",
    "CFM": "bestätigen",
    "CPY": "aufnehmen / verstanden",
    "CPI": "aufnehmen / verstanden",
    "BK": "Unterbrechung / zurück zum Gegenüber",
    "BTU": "zurück zu dir",
    "K": "Aufforderung zum Senden",
    "KN": "nur die angesprochene Station soll senden",
    "AR": "Ende einer Nachricht",
    "SK": "Ende der Funkverbindung; auch verstorbener Funkamateur",
    "CL": "Station schließt / Rufzeichen, je nach Kontext",
    "VA": "Ende der Funkverbindung",
    "73": "beste Grüße",
    "88": "herzliche Grüße / Küsse, je nach Kontext",
    "55": "viel Erfolg, im deutschsprachigen Funkgebrauch",
    "GL": "viel Erfolg",
    "GD": "guten Tag / gut, je nach Kontext",
    "GM": "guten Morgen",
    "GA": "guten Nachmittag",
    "GE": "guten Abend",
    "GN": "gute Nacht",
    "GB": "auf Wiederhören",
    "CUL": "bis später",
    "CUAGN": "bis zum nächsten Mal",
    "CU": "bis dann",
    "HPE": "hoffen",
    "VY": "sehr",
    "FB": "sehr gut",
    "HW": "wie / wie wird aufgenommen?",
    "HWCPY": "wie nimmst du mich auf?",
    "UR": "dein / du bist, je nach Kontext",
    "HR": "hier",
    "NR": "Nummer / nahe, je nach Kontext",
    "NIL": "nichts / kein Empfang",
    "ABT": "etwa / bezüglich",
    "NW": "jetzt",
    "TMW": "morgen",
    "TMR": "morgen",
    "TMRW": "morgen",
    "TMRW?": "morgen?",
    "2NITE": "heute Abend",
    "2NIGHT": "heute Abend",
    "CW": "Morsetelegrafie",
    "SSB": "Einseitenbandmodulation",
    "USB": "oberes Seitenband / USB-Anschluss, je nach Kontext",
    "LSB": "unteres Seitenband",
    "FM": "Frequenzmodulation",
    "AM": "Amplitudenmodulation",
    "PTT": "Sprechtaste / Sendetaste",
    "VOX": "sprachgesteuertes Senden",
    "HF": "Kurzwelle / Hochfrequenz, je nach Kontext",
    "VHF": "Ultrakurzwelle",
    "UHF": "Dezimeterwellenbereich",
    "SWR": "Stehwellenverhältnis, zeigt wie gut Antenne und Funkgerät zusammenpassen; nahe 1 ist gut",
    "SNR": "Signal-Rausch-Verhältnis in dB; je höher, desto klarer hebt sich das Signal vom Rauschen ab. LoRa kommt sogar unter 0 dB noch durch",
    "RSSI": "Empfangsstärke in dBm; -60 ist sehr stark, um -120 wird es für LoRa knapp",
    "FT8": "digitale Amateurfunk-Betriebsart, die noch sehr schwache Signale in 15-Sekunden-Takten dekodiert",
    "FT4": "digitale Kurznachrichten-Betriebsart",
    "WSPR": "digitale Funkbake zur Ausbreitungsbeobachtung",
    "RTTY": "Funkfernschreiben",
    "PSK": "Phasenumtastung",
    "FSK": "Frequenzumtastung",
    "AFSK": "Ton-Frequenzumtastung",
    "APRS": "Automatic Packet Reporting System, Amateurfunk-Netz für Positionen, Wetter und Kurznachrichten",
    "DMR": "Digital Mobile Radio, digitaler Sprechfunk, auch im Amateurfunk über Repeater und Internet verbunden",
    "DSTAR": "digitale Amateurfunk-Sprach- und Datenübertragung",
    "DV": "digitale Sprachübertragung",
    "DVBS": "digitaler Satellitenrundfunk",
    "EME": "Erde-Mond-Erde-Verbindung",
    "LOS": "Sichtverbindung",
    "NLOS": "keine direkte Sichtverbindung",
    "LORA": "Long Range, eine Funktechnik für große Reichweite bei wenig Strom. MeshCore schickt damit Textnachrichten von Gerät zu Gerät, in Europa auf 868 MHz",
    "ISM": "lizenzfreie Frequenzbereiche, z.B. 868 MHz, die jeder mit begrenzter Sendeleistung nutzen darf; dort funkt auch MeshCore",
    "BBS": "Mailboxsystem",
    "DIGI": "Digipeater / digitale Weiterleitung",
    "RPT": "Rapport / Wiederholung / Repeater, je nach Kontext",
    "SOTA": "Summits on the Air",
    "POTA": "Parks on the Air",
    "IOTA": "Islands on the Air",
    "AGC": "automatische Verstärkungsregelung",
    "AFC": "automatische Frequenznachführung",
    "ATU": "Antennenanpassgerät",
    "BFO": "Überlagerungsoszillator",
    "IF": "Zwischenfrequenz",
    "RF": "Hochfrequenz",
    "AF": "Tonfrequenz",
    "RFI": "Hochfrequenzstörung",
    "EMC": "elektromagnetische Verträglichkeit",
    "UTC": "koordinierte Weltzeit",
    "LOC": "Locator / Standort, je nach Kontext",
    "DOK": "Distrikts- und Ortsverbandskenner",
    "OV": "Ortsverband",
    "DARC": "Deutscher Amateur-Radio-Club",
}
DISPLAY = {"LORA": "LoRa"}
# Whole tokens only: do not reinterpret callsigns, e.g. DL1WX, URLs or ordinary
# German words such as "am", "de", "k" as abbreviations without radio context.
TOKENS = re.compile(r"(?<![\w/@.-])(?:[A-Za-z][A-Za-z0-9]*|73|88|55)(?![\w/.-])")
AMBIGUOUS = {
    "DE",
    "K",
    "AR",
    "SK",
    "CL",
    "VA",
    "AM",
    "FM",
    "USB",
    "HF",
    "NR",
    "GD",
    "GA",
    "GE",
    "GM",
    "GN",
    "OV",
    "LOC",
    "TU",
    "UR",
    "CU",
    "IF",
    "AF",
}


def glossary_context(text):
    found = {}
    for match in TOKENS.finditer(text):
        token = match.group()
        key = token.upper()
        if key not in TERMS:
            continue
        if key in {"73", "88", "55"} and text[match.end() :].strip(" ?.!,"):
            continue
        if key in AMBIGUOUS and token != key and text.strip().upper() != key + "?":
            continue
        found[key] = TERMS[key]
    if not found and not re.search(r"\bQ[A-Z]{2}\b", text, re.I):
        return ""
    definitions = "; ".join(key + ": " + value for key, value in found.items())
    return (
        "Funkkürzel der aktuellen Nachricht, kontextabhängig lesen: " + definitions + ". "
        "Das Original bleibt maßgeblich. Erkläre bei einer Bedeutungsfrage das Kürzel; "
        "bei einer Sachfrage beantworte das Anliegen statt nur die Abkürzung aufzulösen. "
        "Q-Codes mit Fragezeichen als Frage verstehen. Unbekannte oder mehrdeutige Kürzel "
        "nicht erfinden: kurz nachfragen oder eine passende Quelle mit search_web nachsehen. "
        "Verwende für den eigenen QTH ausschließlich die Stationskonfiguration; der Standort des Gegenübers ist unbekannt, "
        "wenn er ihn nicht genannt hat. Keine Empfangsrapporte, RST-, RSSI-, SNR-Werte oder "
        "Funkverbindungen erfinden. QSL bestätigt nur, dass diese Nachricht hier angekommen ist. "
        "QSY, QRO, QRP und QRT sind keine Erlaubnis, echte Geräteinstellungen zu ändern. "
        "Nicht ungefragt auf jede Antwort Funkkürzel oder 73 anhängen."
    )


def wx_request(text, default_location="Berlin-Friedenau"):
    """Unambiguous short WX commands -> weather arguments, without an LLM hop."""
    match = re.fullmatch(r"\s*wx(?:\s*[:?]\s*|\s+|$)(.*?)\s*[?!.]*\s*", text, re.I)
    if not match:
        return None
    tail = match.group(1).strip(" ?.!,")
    if re.search(
        r"\b(bedeut\w*|heiß\w*|heiss\w*|erklär\w*|erklaer\w*|abkürzung\w*|abkuerzung\w*)\b", tail, re.I
    ):
        return None
    if re.search(r"https?://|[@<>\n]", tail, re.I):
        return None
    period = "now"
    patterns = [
        ("day_after_tomorrow", r"\b(?:übermorgen|uebermorgen)\b"),
        ("tomorrow", r"\b(?:morgen|tmw|tmr|tmrw|tomorrow)\b"),
        ("today", r"\b(?:heute|today)\b"),
        ("now", r"\b(?:jetzt|nw|now)\b"),
    ]
    periods = [value for value, pattern in patterns if re.search(pattern, tail, re.I)]
    if len(set(periods)) > 1:
        return None
    if periods:
        period = periods[0]
    for _value, pattern in patterns:
        tail = re.sub(pattern, " ", tail, flags=re.I)
    date = re.search(r"\b\d{4}-\d{2}-\d{2}\b", tail)
    if date:
        if periods:
            return None
        period = date.group()
        tail = tail[: date.start()] + " " + tail[date.end() :]
    tail = re.sub(r"^(?:in|für|fuer|qth)\s+", "", tail.strip(), flags=re.I)
    location = " ".join(tail.split()).strip(" ?.!,") or default_location
    if len(location) > 100:
        return None
    return {"location": location, "period": period}


def short_radio_reply(
    text, *, name="MeshCore AI Bot", location="nicht konfiguriert", timezone_name="Europe/Berlin"
):
    """Common brief calls need no LLM and must not invent a measured report."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    body = " ".join(str(text).split()).strip()
    key = body.strip(" ?!.,").upper()
    replies = {
        "QTH": "Konfigurierter QTH: " + location + ".",
        "QRV": "QRV – ich bin bereit für deine Frage.",
        "QRZ": "Hier ist " + name + ".",
        "QSL": "QSL – deine Nachricht ist hier angekommen.",
        "QRM": "QRM = Störungen durch andere Sender. Eine Störungsmessung liegt mir nicht vor.",
        "QRN": "QRN = atmosphärische Funkstörungen. Ich habe dazu keine aktuellen Messwerte.",
        "QSB": "QSB heißt Signalschwankungen. Ohne Messwerte kann ich sie hier nicht beurteilen.",
        "QRL": "QRL = belegt/beschäftigt. Ich bin ansprechbar; die Frequenzbelegung kann ich nicht prüfen.",
        "QSY": "QSY heißt Frequenzwechsel. Ich ändere hier keine Frequenzen oder Kanäle.",
        "QRO": "QRO heißt Sendeleistung erhöhen. Ich ändere keine Funkgeräteinstellungen.",
        "QRP": "QRP steht für geringe Sendeleistung bzw. Leistung verringern. Ich stelle das Funkgerät nicht um.",
        "QRS": "QRS heißt langsamer geben. Der festgelegte Sendeabstand bleibt unverändert.",
        "QRQ": "QRQ heißt schneller geben. Der festgelegte Sendeabstand bleibt unverändert.",
        "QRT": "QRT verstanden. 73, bis zum nächsten Mal!",
        "RST": "Für einen RST-Rapport fehlen mir aktuelle Messdaten. Deine Textnachricht ist angekommen.",
        "73": "73, bis zum nächsten Mal!",
        "55": "55 – viel Erfolg!",
        "88": "88 – herzliche Grüße zurück!",
        "TNX": "Gern geschehen!",
        "TKS": "Gern geschehen!",
        "TU": "Gern geschehen!",
    }
    answer = replies.get(key)
    if key == "QTR":
        now = datetime.now(UTC)
        answer = (
            "QTR: "
            + now.astimezone(ZoneInfo(timezone_name)).strftime("%H:%M Uhr %Z")
            + " / "
            + now.strftime("%H:%M UTC")
            + "."
        )
    if not answer and re.fullmatch(
        r"(?:cq[ ,!?]*){1,3}(?:(?:ist jemand da|hört mich jemand|hoert mich jemand)[?!., ]*)?", body, re.I
    ):
        answer = "Hier ist " + name + ", QRV für deine Frage."
    if not answer and re.search(
        r"^(?:gib mir|kannst du mir).{0,30}(?:rst|rapport)|^wie (?:komme? ich(?: bei dir)? an|nimmst du mich auf|h(?:ö|oe)rst du mich)[?!. ]*$|^bin ich.{0,30}(?:59|5[ /x]9)",
        body,
        re.I,
    ):
        answer = "Für einen Empfangsrapport fehlen mir aktuelle Funkmesswerte. Deine Textnachricht ist hier angekommen."
    if not answer:
        match = re.fullmatch(
            r"(?:was (?:ist|bedeutet)|erkläre|erklaere)\s+(?:das kürzel\s+|das kuerzel\s+)?([a-z0-9]{1,6})[?!., ]*",
            body,
            re.I,
        )
        if match and match.group(1).upper() in TERMS:
            code = match.group(1).upper()
            answer = DISPLAY.get(code, code) + ": " + TERMS[code] + "."
    sources = []
    if (
        not answer
        and re.search(r"kanal|gruppe", body, re.I)
        and re.search(r"gesendet", body, re.I)
        and re.search(r"alle|empfang|bestätig|bestaetig|angekommen", body, re.I)
    ):
        answer = "„Gesendet“ bestätigt je nach App nur die lokale Annahme durch das Companion. Kanalnachrichten haben keine ACKs pro Empfänger; daraus folgt kein Empfang bei allen."
        sources = ["https://meshcore-dev.github.io/meshcore-ha/docs/ha/messaging/"]
    if not answer:
        unknown = re.fullmatch(
            r"(?:(?:was (?:ist|bedeutet)|erkläre|erklaere)\s+)?(Q[A-Z]{2})[?!., ]*", body, re.I
        )
        if unknown and unknown.group(1).upper() not in TERMS:
            answer = (
                "Für "
                + unknown.group(1).upper()
                + " habe ich keine verlässlich hinterlegte Bedeutung. Wo hast du das Kürzel gesehen?"
            )
    return {"answer": answer, "sources": sources, "radio_shortcut": True} if answer else None
