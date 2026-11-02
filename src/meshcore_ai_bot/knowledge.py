"""Small, source-backed MeshCore topic cards, reviewed 2026-11-02.
Only relevant cards enter the prompt. No radio operations are performed here.
"""

import re

ROOT = "https://github.com/meshcore-dev/MeshCore"
FAQ = ROOT + "/blob/main/docs/faq.md"
README = ROOT + "/blob/main/README.md"
PAYLOADS = ROOT + "/blob/main/docs/payloads.md"
PACKETS = ROOT + "/blob/main/docs/packet_format.md"
CLI = ROOT + "/blob/main/docs/cli_commands.md"
SCOPE = "https://blog.meshcore.io/2026/04/17/default-scope"
ACK = "https://meshcore-dev.github.io/meshcore-ha/docs/ha/messaging/"

CARDS = {
    "roles": (
        r"\b(?:nodes?|knoten|companion\w*|repeater\w*|relay\w*|rollen?|firmware)\b",
        "Node/Knoten ist der Oberbegriff für ein Funkgerät im Mesh. Companion ist der persönliche Nachrichtenzugang über eine App, z.B. per Bluetooth oder USB. Die offizielle Companion-Rolle leitet fremde Pakete nicht als Repeater weiter. Ein Repeater ist für die Weiterleitung und Netzerweiterung da; nicht jeder Node ist ein Repeater. Ein Room Server ist eine eigene Rolle für gemeinsame Posts. Andere Rollen können je nach Firmware ebenfalls Weiterleitung unterstützen; nicht pauschal „ausschließlich Repeater“ behaupten. Keine pauschale Gleichsetzung aller Rollen oder Behauptung, jedes eingeschaltete Gerät vergrößere das Mesh.",
        README,
    ),
    "basics": (
        r"\b(?:meshcore|lora|meshtastic|internet|offline|off-grid|bluetooth|ble|usb)\b",
        "MeshCore ist LoRa-basierte Firmware mit Mehrsprung-Weiterleitung. Das Funknetz kann ohne Internet arbeiten. Smartphone-App und Companion sind unterschiedliche Bestandteile: Bluetooth/USB verbindet die App mit dem Funkgerät; LoRa übernimmt die Funkstrecke. MeshCore und Meshtastic sind verschiedene Systeme; gleiches Funkmodul bedeutet nicht, dass Nachrichten ohne Weiteres kompatibel sind.",
        README,
    ),
    "routing": (
        r"\b(?:hops?|pfad\w*|route\w*|routing|flood\w*|direkt\w*|dm|repeater\w*)\b",
        "Ein Hop ist eine Weiterleitungsstation im Pfad. Ohne bekannten Pfad können Direktnachrichten zunächst per Flood verbreitet werden; Rückmeldungen ermöglichen einen gespeicherten Pfad für weitere direkte Übertragung. Bei MeshCore leiten nur dafür vorgesehene Rollen die Flood-Pakete weiter, nicht pauschal alle empfangenden Nodes; normale Companions wiederholen fremde Pakete nicht. Gruppenkanäle verwenden Flooding statt eines einzelnen Empfängerpfads. Flooding ist durch Repeater-Regeln begrenzbar, kein Versand an garantiert alle Geräte. Wiederholungen und Pfad-Reset hängen auch vom verwendeten Client ab; keine feste Anzahl von Versuchen für alle Apps versprechen.",
        FAQ,
    ),
    "channels": (
        r"\b(?:kan[aä]l\w*|channels?|public|hashtag|privat\w*|schl[üu]ssel|verschl[üu]ssel\w*)\b|#\w+",
        "Ein Gruppenkanal benötigt übereinstimmende Kanaleinstellungen und den gemeinsamen Schlüssel. Der lokale Kanalindex ist ein Gerätespeicherplatz und muss bei anderen Teilnehmern nicht dieselbe Zahl haben. Kanalname und Scope sind verschiedene Dinge. Public ist kein privater Kanal. Ein benannter Gruppenkanal ist kein automatisch angelegter Room Server; normale Kanalnachrichten sind keine zentrale Offline-Mailbox.",
        FAQ,
    ),
    "rooms": (
        r"\b(?:room\s*server|raumserver\w*|bbs|mailbox|store.and.forward|speicher\w*)\b",
        "Room Server bieten gemeinsame Posts mit Nachholen gespeicherter Nachrichten. Speicherung und Aufbewahrung sind implementierungsabhängig und begrenzt. Repeater, Gruppenkanal und Room Server unterscheiden; aus einem empfangenen Repeater-Advert keine Nachrichten-Mailbox ableiten.",
        README,
    ),
    "identity": (
        r"\b(?:adverts?|advertisement|entdeck\w*|kontakte?|identit[aä]t\w*|public.key|privat\w*|schl[üu]ssel|node.?id|namen?)\b",
        "Ein Advert macht einen Node bekannt: unter anderem öffentlicher Schlüssel und optional Name/Position. Ein empfangener Advert ist keine Garantie für dauernde oder beidseitige Erreichbarkeit. Gerätename und kryptografische Identität unterscheiden. Der sichtbare Name in Gruppenmeldungen ist nicht als Absenderidentität signiert und kann von Kanalteilnehmern gewählt werden; daraus keine gesicherte Identitätsprüfung ableiten. Private Schlüssel niemals erfragen oder zur Veröffentlichung auffordern.",
        PAYLOADS,
    ),
    "delivery": (
        r"\b(?:ack|best[aä]tig\w*|empfang\w*|angekommen|gesendet|send\w*|zugestellt|zustell\w*|antwort\w*|erreich\w*|timeout|handshake)\b",
        "„Gesendet“ kann je nach App nur die Annahme durch das eigene Companion bedeuten. Daraus weder erfolgreiche Abstrahlung noch Empfang ableiten; nicht pauschal „erfolgreich in die Funkstrecke geschickt“ sagen. Bei Direktnachrichten sind Empfänger-ACKs und Rückwege zu unterscheiden. Gruppenkanäle bieten keine individuelle ACK-Bestätigung für jeden Leser. Ein gehörtes wiederholtes Paket ist kein Beleg, dass alle Empfänger es gelesen haben. Verzögerung kann auch aus der lokalen Sendewarteschlange entstehen. Ohne Logs/Messwerte keine Ursache als erwiesen ausgeben und keinen angeblichen WLAN-/TCP-Handshake im LoRa-Mesh erfinden.",
        ACK,
    ),
    "repeater_status": (
        r"\b(?:teufelsberg|ausgefallen|ausfall|offline|repeater.status|kaputt|störung|stoerung|erreichbar)\b",
        "Bei einer unbenannten Repeater-Frage nur die grundsätzliche Unsicherheit erklären und kurz nach dem Gerätenamen oder öffentlichen Schlüssel fragen; keine Werkzeugnamen als Benutzerbefehle ausgeben. Für konkrete Repeater-Zustände get_repeater_status verwenden. Das gilt für alle Repeater im eigenen lokalen Katalog. Geräte über öffentlichen Schlüssel unterscheiden. Für unbekannte Namen oder eine Auswahl list_repeaters mit passendem Orts-/Namensfilter verwenden; Keine vollständige Kenntnis des weltweiten Mesh behaupten. Die Daten sind beobachterabhängig und nicht weltweit vollständig. Keine langen Kataloglisten ungefragt über Funk ausgeben. Der Abgleich liest aktuelle eigene Kontakt-/Advert-Daten und datierte manuelle Beobachtungen. Ein fehlender Advert, ein alter Kontakt, eine grüne Anzeige oder eine aus Pfaden abgeleitete Weiterleitung beweisen jeweils keinen vollständigen aktuellen Betriebszustand. Der eigene Datenbank-Refresh ist kein neues Lebenszeichen des Repeaters. Analyzer-Snapshots nur mit ihrem tatsächlichen Prüfdatum nennen und nie als gerade live abgerufen ausgeben. Die Analyzer-API wird nicht automatisiert abgefragt.",
        ACK,
    ),
    "scopes": (
        r"\b(?:scopes?|region\w*|de-bebb|de-be|de-bb)\b",
        "Ein Scope grenzt die Verbreitung von Flood-Paketen anhand von Regionen und Repeater-Regeln ein; er ist weder Verschlüsselungsschlüssel noch amtliches Warngebiet. Kanal-Scope kann den Default-Scope übersteuern. Unbekannte Direktpfade, Adverts und Anfragen können ebenfalls scoped Floods verwenden. Ein ungeeigneter Scope kann die Pfadsuche oder den Rückweg behindern, doch Scope allein beweist weder Zustellung noch einen konkreten Fehler. Region-Namen sind lokale Vereinbarungen, keine weltweite feste Ortsliste.",
        SCOPE,
    ),
    "paths": (
        r"\b(?:hops?|hash\w*|kollision\w*|path|pfad\w*|paket\w*|utf-?8|zeichen|bytes?)\b",
        "Pfadkennungen sind verkürzte Kennungen aus öffentlichen Schlüsseln; ein kurzer Hash ist nicht immer eindeutig. Pfad-Hash-Breite und Hop-Anzahl sind unterschiedliche Größen. Größere Pfadkennungen benötigen mehr Paketplatz und setzen passende Firmwareunterstützung voraus. Nutztextlänge hängt von Paketformat, Overhead, Absendername und Client ab; Umlaute/Emoji benötigen teils mehrere UTF-8-Bytes. Eine lokale Textgrenze nicht als unveränderliches Limit aller MeshCore-Geräte ausgeben.",
        PACKETS,
    ),
    "radio": (
        r"\b(?:frequenz\w*|bandbreite|spreading|coding|sf\d*|bw|cr|antenne\w*|reichweite|rssi|snr|sendeleistung|airtime|duty.?cycle|kollision\w*)\b",
        "Passende Frequenz, Bandbreite, Spreading Factor und Coding Rate sind wichtig für die gemeinsame Funkstrecke. Größere Reichweite ist nicht allein eine Frage maximaler Sendeleistung; Antenne, Aufbau und Hindernisse spielen mit. Längere Pakete und häufigere Wiederholungen belegen mehr Airtime. RSSI beschreibt Empfangspegel, SNR das Verhältnis zum Rauschen. Keine Garantie aus einem Einzelwert ableiten. Konkrete Befehle hängen von Rolle und Firmware ab; Einstellungen nicht ohne Geräte-/Versionsangaben behaupten. Zulässige Funkparameter vor Änderungen anhand aktueller Vorgaben prüfen.",
        CLI,
    ),
}

GUARD = (
    "Verwende das folgende geprüfte MeshCore-Grundwissen passend zur Frage, nicht als Liste zum Aufsagen. "
    "Frage nach genau einer fehlenden Angabe, wenn Gerät, Rolle oder Version entscheidend sind. "
    "Bei Versionsdetails, genauen CLI-Befehlen oder neuen Funktionen offizielle aktuelle MeshCore-Dokumentation "
    "mit search_web prüfen statt Menüs/Kommandos zu erfinden. Alte falsche Antworten im Verlauf sind keine Quelle. "
    "Ohne aktuelle Stationsdaten keine Online-Nodes, Routen, Firmwarestände, Frequenzen, Scopes, "
    "GPS-Positionen oder Messwerte behaupten. Du führst keine Funkgeräteänderungen aus. "
    "Interne Werkzeugnamen wie get_repeater_status und list_repeaters nicht als Befehle an Benutzer weitergeben. Werkzeuge selbst aufrufen; fehlt ein Gerätename, natürlich danach fragen. Keine Reichweiten- oder Zustellgarantie. Bei ausdrücklich anderen Systemen wie WLAN-Repeatern "
    "oder Node.js deren Bedeutung erklären und nicht ungefragt auf MeshCore umdeuten."
)


def meshcore_context(text, history=()):
    explicit = bool(re.search(r"\bmeshcore\b", text, re.I))
    if not explicit and re.search(r"\b(?:node\.?js|javascript|wlan|wi-?fi|youtube)\b", text, re.I):
        return {"text": "", "sources": [], "topics": []}
    query = text
    if re.search(r"\b(?:das|dabei|damit|dann|er|die|den|und)\b", text, re.I) and len(text) < 160:
        previous = next((str(m.get("content", "")) for m in reversed(history) if m.get("role") == "user"), "")
        if any(re.search(pattern, previous, re.I) for pattern, _, _ in CARDS.values()):
            query += " " + previous
    chosen = [name for name, (pattern, _, _) in CARDS.items() if re.search(pattern, query, re.I)]
    if not chosen:
        return {"text": "", "sources": [], "topics": []}
    if explicit and chosen == ["basics"]:
        chosen = ["basics", "roles", "channels", "routing", "scopes"]
    # Facts are static trusted notes; never interpolate question text as instructions.
    notes = [GUARD]
    sources = []
    for name in chosen:
        _, fact, url = CARDS[name]
        notes.append(name + ": " + fact)
        if url not in sources:
            sources.append(url)
    return {"text": "\n".join(notes), "sources": sources, "topics": chosen}


def meshcore_short_answer(text):
    if re.fullmatch(
        r"\s*(?:was ist|was bedeutet|erkläre|erklaere|wie funktioniert)\s+(?:das\s+)?(?:meshcore[- ]+)?flood(?:ing)?[?.! ]*",
        text,
        re.I,
    ):
        return {
            "answer": "Bei MeshCore wird ein Flood-Paket über dafür eingerichtete Weiterleitungs-Nodes verbreitet, typischerweise Repeater. Normale Companions leiten fremde Pakete nicht weiter. Scope, Hop-Grenzen und Repeater-Regeln begrenzen die Verbreitung; es ist keine Zustellgarantie.",
            "sources": [FAQ],
            "radio_shortcut": True,
        }
    return None
