# Gerätetest ohne Funkhardware

Die Simulation prüft die Softwareseite der Geräteanbindung mit der echten Bibliothek `meshcore==2.3.15`. Ein virtueller Companion läuft am anderen Ende einer privaten POSIX-Pseudo-Terminal-Verbindung. Der Bot verwendet seinen normalen USB-Adapter und den echten seriellen SDK-Transport: Kommandos und Antworten werden als Binärframes übertragen und vom SDK dekodiert. Es werden keine echten USB-Geräte geöffnet und keine Funkpakete ausgesendet.

## Starten

Auf Linux/Raspberry Pi OS oder macOS im geklonten Repository:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev,radio]'
pytest -q tests/test_hardware_simulation.py
```

Kein API-Schlüssel, kein Cloud-Server, kein Modell und kein Internet während der Tests erforderlich. Zum erstmaligen Installieren der Abhängigkeiten wird Internet benötigt. `pytest -q` führt zusätzlich die übrigen Tests aus. Die GitHub-Prüfung installiert das Radio-Extra und führt dieselbe Simulation auf Python 3.11, 3.12 und 3.13 aus. Bei einer reinen Core-Installation ohne Radio-Extra meldet pytest diese Datei als übersprungen.

## Was geprüft wird

20 Szenarien einschließlich der parametrisierten Fälle:

- APP_START, Stationsname, Kanalabfrage und passives Lesen der Repeaterkontakte über echte serielle Frames.
- Empfang über MESSAGES_WAITING und den anschließenden Nachrichtenabruf des SDK.
- Duplikaterkennung, eigene Echos, alte Nachrichten, unbekannte Kanäle sowie Public-Sperre und KI-Regel.
- Kanalprüfung mit falschem Schlüssel und Anlegen ausschließlich in freien Plätzen.
- Fragmentierte Frames und eingestreute serielle Konsolenausgaben.
- Verzögerte lokale Bestätigung, vollständige nummerierte UTF-8-Teilnachrichten und mindestens 30 Sekunden Abstand nach der Bestätigung.
- Explizite Geräteablehnung, fehlende Bestätigung und Unterbrechung der virtuellen Verbindung.
- Abbruch während einer Sendung, erneutes Öffnen der gespeicherten Datenbank und Wiederverbinden ohne erneutes Senden bereits bearbeiteter Pakete.
- Automatisches Wiederverbinden im tatsächlichen Dienstablauf nach fehlender Gerätebestätigung, ohne die unklare Nachricht erneut zu senden. Nur Testuhr und Wartezeiten werden beschleunigt.
- Verspätete Bestätigung: Die alte Sitzung wird nicht weiterverwendet, bevor neu verbunden wurde.
- Ein tatsächlich eingehendes Advert aktualisiert die Zeit des bekannten Repeaters; eine reine Kontaktabfrage tut das nicht.

Die Sendepausen verwenden eine beschleunigte Testuhr. Serielle Kommunikation und SDK-Verarbeitung laufen wirklich; die Simulation wartet nicht bei jedem Paket 30 reale Sekunden. DTR und RTS werden ausschließlich im Test deaktiviert, weil Pseudo-Terminals keine elektrischen Modem-Steuerleitungen besitzen. Es werden weder Firmware noch die interne MeshCore-SDK-Logik nachgebildet.

## Gefundener und behobener Fehler

Das SDK meldet ausbleibende Antworten als ERROR-Ereignis mit einem `reason` oder `error`, statt zwingend eine Ausnahme auszulösen. Das wurde zuvor wie eine explizite Geräteablehnung behandelt. Jetzt gilt nur ein Gerätefehler mit numerischem Fehlercode als Ablehnung. Eine fehlende oder abgebrochene Antwort wird als **uncertain** gespeichert. Die Sitzung wird anschließend neu verbunden, damit eine verspätete, nicht eindeutig zuordenbare OK-Antwort kein späteres Paket fälschlich bestätigt. Unklare Pakete werden nicht automatisch erneut gesendet.

## Grenzen

Diese Simulation ist kein erfolgreicher Test eines realen Raspberry Pi mit Funkgerät. Ungeprüft bleiben Stromversorgung, echte USB-Treiber und -Kabel, DTR-/RTS-Verhalten, Firmwarevarianten, Antenne, Frequenzeinstellungen, Funklast, Reichweite und tatsächliche Zustellung an andere Teilnehmer. Auch ein simuliertes OK bestätigt nur lokale Annahme, keinen Empfang im Funknetz.
