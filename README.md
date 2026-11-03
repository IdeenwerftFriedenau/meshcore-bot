# MeshCore AI Bot

Ein eigenständiger deutschsprachiger MeshCore-Bot für Raspberry Pi. Die Konfiguration läuft lokal auf dem Pi. Ein USB-Gerät mit MeshCore-Companion-Firmware übernimmt den Funk; der Pi übernimmt Fragen, Datenquellen und die dauerhafte Sendewarteschlange.

## Funktionen

- `#fragdenbot`: Fragen ohne Anrede. `#bot`: nur mit dem eigenständigen Wort **KI** im Nachrichtentext.
- Public und `#public` bleiben gesperrt, auch für automatische Warnungen und Nachrichten.
- Mindestens 30 Sekunden Abstand in der Beispielkonfiguration, Empfangsruhe und dauerhaft gespeicherte Warteschlange.
- Funkabkürzungen, WX, Repeater/Node/Companion, Rechenaufgaben und auf Wunsch spielerische Horoskope.
- Wetter über Open-Meteo; Repeaterwissen aus passiv empfangenen Kontakten und Adverts. Ein altes Advert gilt ausdrücklich nicht als Beweis für einen Ausfall.
- Konfigurierbare Scope-Bezeichnungen `de-be`, `de-bb`, `de-bebb`.
- Optionale DWD-/BBK-Warnungen für alle 16 Bundesländer mit Zeitangaben und gemeinsamer Kennung BER/BB. Handlungsempfehlungen standardmäßig aus. Optionale RSS-Schlagzeilen.
- KI über einen kompatiblen Chat-Completions-Anbieter oder einen lokalen Ollama-Dienst. API-Schlüssel bleiben außerhalb des Repositorys.

**Zustellung:** Ein OK vom USB-Gerät bestätigt die lokale Annahme, keinen Empfang durch alle Funkteilnehmer. Unklare Sendungen werden deshalb nicht blind wiederholt. Mehrteilige Texte bleiben vollständig erhalten; KI-Antworten über acht Pakete werden durch eine Bitte um eine engere Frage ersetzt.

## Auf dem Raspberry Pi installieren

Empfohlen: Raspberry Pi 4/5, Raspberry Pi OS **64 Bit**, Python 3.11 oder neuer, Internet für Wetter/Online-KI sowie ein USB-MeshCore-Companion. Ein lokales KI-Modell benötigt deutlich mehr Arbeitsspeicher und Rechenzeit; es ist optional.

```sh
git clone https://github.com/IdeenwerftFriedenau/meshcore-bot.git
cd meshcore-bot
sudo bash scripts/install-pi.sh
sudo nano /etc/meshcore-ai-bot/config.toml
sudo nano /etc/meshcore-ai-bot/.env
```

In `config.toml` den USB-Pfad (`ls -l /dev/serial/by-id/`), Stationsnamen, Standort, Kanäle und KI-Modell eintragen. In `.env` den Schlüssel hinterlegen. Die Kanäle müssen auf dem Funkgerät vorhanden sein und ihre Schlüssel müssen passen. `create_channels = true` erlaubt das Anlegen in freien Plätzen; bestehende Kanäle werden nicht überschrieben.

```sh
sudo -u meshcore-ai /opt/meshcore-ai-bot/.venv/bin/meshcore-ai check --config /etc/meshcore-ai-bot/config.toml
# Optional: Kanäle prüfen / ausdrücklich konfigurierte freie Plätze anlegen:
sudo -u meshcore-ai /opt/meshcore-ai-bot/.venv/bin/meshcore-ai provision --config /etc/meshcore-ai-bot/config.toml
sudo systemctl enable --now meshcore-ai-bot
journalctl -u meshcore-ai-bot -f
```

Der Installer startet den Funkbetrieb **nicht** automatisch. Erst der letzte Startbefehl aktiviert Antworten. Warnungen und RSS sind zusätzlich standardmäßig ausgeschaltet. Kein Start-Ping und keine Testsendung.

## Ohne Funk ausprobieren

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
cp config.example.toml config.toml
cp .env.example .env
meshcore-ai demo --offline 'wx friedenau'
meshcore-ai demo --offline 'Was bedeutet QTH?'
meshcore-ai replay --offline examples/radio-cases.json
pytest
```

`--offline` vermeidet auch Wetter- und KI-Abfragen. `demo`, `replay` und `check` öffnen niemals das Funkgerät. `run` verbindet erst mit zusätzlichem `--live`. Der Systemdienst verwendet dieses Flag.

## KI auswählen

Die Vorlage verwendet OpenRouter; Modell und Anbieter lassen sich frei ändern. Für Ollama auf demselben Pi:

```toml
[ai]
base_url = "http://127.0.0.1:11434/v1"
model = "DEIN_INSTALLIERTES_MODELL"
allow_local_http = true
```

Ollama und das Modell separat installieren. Ohne verfügbaren KI-Dienst funktionieren feste Funkantworten, Rechnen und die lokalen Repeaterinformationen weiter; freie Fragen und Horoskope benötigen das Modell. HTTP ist ausschließlich für ausdrücklich aktivierte Loopback-Dienste erlaubt.

## Betrieb und Grenzen

Einstellungen: [Konfigurationsvorlage](config.example.toml). Details: [Betrieb und Datenschutz](docs/operation.md), [Architektur](docs/architecture.md).

Die SQLite-Datenbank liegt beim Dienst unter `/var/lib/meshcore-ai-bot`. Nachrichten werden standardmäßig nach 24 Stunden bereinigt. Dieses Projekt erzeugt und speichert keine Audiodateien. Nachrichten kommen optional aus ausdrücklich eingetragenen RSS-Feeds.

Das Repository enthält keine privaten Stationsschlüssel oder vorgetäuschten Repeater-Livedaten. Der öffentliche Analyzer wird nicht automatisiert ausgelesen. Funkbedingungen, Firmware und externe Dienste können Zustellungen verhindern; es gibt keine Garantie vollständiger Funkzustellung. Vor dem Dauerbetrieb auf der eigenen Hardware prüfen. Lizenz: MIT.

## Geräteanbindung ohne Hardware testen

```sh
pip install -e '.[dev,radio]'
pytest -q tests/test_hardware_simulation.py
```

20 zusätzliche Szenarien prüfen den echten MeshCore-SDK-Transport an einem virtuellen seriellen Companion, einschließlich verzögerter Bestätigungen, Verbindungsabbrüchen und Neustarts. Dabei wird kein Funkgerät geöffnet. Ablauf und Grenzen stehen in der [Anleitung zur Hardware-Simulation](docs/hardware-simulation.md).

## Pi Console: lokales Konfigurationsdashboard

Eine responsive Oberfläche für Station, Funk, KI, Kanalregeln, alle 16 Bundesländer und Datenaufbewahrung. Keine externen Assets. Public bleibt geschützt. Einstellungen werden geprüft und atomar gespeichert; der laufende Bot übernimmt gültige Änderungen mit einem Verbindungsneustart. API-Schlüssel sind nur schreibbar, nicht auslesbar.

```sh
sudo systemctl enable --now meshcore-dashboard
# Auf dem eigenen Rechner:
ssh -L 8765:127.0.0.1:8765 pi@raspberrypi.local
```

Browser: `http://127.0.0.1:8765`. Das Dashboard startet den Funkdienst nicht. [Einrichtung und Zugriffsschutz](docs/dashboard.md).

## DAB+ ASA ohne Internet

Profil für **Nooelec RTL-SDR V5**, optionale RTL-SDR-Treiber und eine lokale Offline-Warnschnittstelle mit Zeitprüfung, Zielregionen, Aktualisierungen und Duplikatschutz. **Für echten ASA-Empfang ist zusätzlich ein separat eingerichteter ASA-fähiger Decoderadapter erforderlich.** Ein Stick oder gewöhnlicher DAB-Decoder allein bestätigt keinen ASA-Empfang. [Empfang, Voraussetzungen und Adapterformat](docs/dab-asa.md).


Text-Testbereich: Unter **Text testen** eine Nachricht, einen vorhandenen Kanal und einen Testabsender wählen. Die Vorschau verwendet die gespeicherten Kanalregeln, denselben Antwortprozess und dieselbe Paketaufteilung wie der Bot. Public bleibt auch im Test stumm, #bot benötigt KI. Standardmäßig läuft der Test offline. Für freie KI-Fragen oder Wetterdaten Offline ausschalten; dabei werden die konfigurierten Dienste abgefragt und es können Anbietergebühren entstehen. Jeder Test startet ohne Gesprächsverlauf; vorhandene Repeaterkontakte werden ausschließlich gelesen. Die Testdatenbank liegt nur im Arbeitsspeicher, die echte Sendewarteschlange bleibt unverändert. Es wird keine Funkhardware geöffnet und nichts ausgesendet. Die Vorschau prüft keine Funkzustellung. Ein Test dauert höchstens 60 Sekunden, nur ein Test läuft gleichzeitig. Verbundene Tests haben eigene Limits gemäß KI-Konfiguration; diese gelten pro Dashboard-Prozess und getrennt vom Funkdienst.
