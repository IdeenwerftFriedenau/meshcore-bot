# MeshCore AI Bot

Ein eigenständiger deutschsprachiger MeshCore-Bot für Raspberry Pi. Die Konfiguration läuft lokal auf dem Pi. Ein USB-Gerät mit MeshCore-Companion-Firmware übernimmt den Funk; der Pi übernimmt Fragen, Datenquellen und die dauerhafte Sendewarteschlange.

## Funktionen

- `#fragdenbot`: Fragen ohne Anrede. `#bot`: nur bei **@Ideenwerft**, **#ideenwerft** oder **#ideenwerft-friedenau**. KI/AI lösen dort keine Antwort aus.
- Public und `#public` bleiben gesperrt, auch für automatische Warnungen und Nachrichten.
- 10 Sekunden Mindestabstand in der Beispielkonfiguration und zwei Sekunden Empfangsfenster, Empfangsruhe und dauerhaft gespeicherte Warteschlange.
- Funkabkürzungen, WX, Repeater/Node/Companion, Rechenaufgaben und auf Wunsch spielerische Horoskope.
- Wetter über Open-Meteo; Repeaterwissen aus passiv empfangenen Kontakten und Adverts. Ein altes Advert gilt ausdrücklich nicht als Beweis für einen Ausfall.
- Konfigurierbare Scope-Bezeichnungen `de-be`, `de-bb`, `de-bebb`.
- Optionale DWD-/BBK-Warnungen mit Zeitangaben für frei wählbare Bundesländer. Voreinstellung: Berlin und Brandenburg (BER/BB). Handlungsempfehlungen standardmäßig aus. Optionale RSS-Schlagzeilen.
- KI über einen kompatiblen Chat-Completions-Anbieter oder einen lokalen Ollama-Dienst. API-Schlüssel bleiben außerhalb des Repositorys.

**Zustellung:** Ein OK vom USB-Gerät bestätigt die lokale Annahme, keinen Empfang durch alle Funkteilnehmer. Unklare Sendungen werden deshalb nicht blind wiederholt. Antworten umfassen höchstens fünf Pakete und rund 420 Zeichen; gekürzte Antworten laden zur Nachfrage ein. ASCII-Art behält ihre Zeilenumbrüche. Überlange Warnungen werden ausdrücklich als Kurzfassung mit Verweis auf den Volltext gekennzeichnet.

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

### Text testen

Unter **Text testen** eine Nachricht, einen vorhandenen Kanal und einen Testabsender wählen. Die Vorschau verwendet die gespeicherten Kanalregeln, denselben Antwortprozess und dieselbe Paketaufteilung wie der Bot. Public bleibt auch im Test stumm, #bot benötigt eine Erwähnung. Standardmäßig läuft der Test offline. Für freie KI-Fragen oder Wetterdaten Offline ausschalten; dabei werden die konfigurierten Dienste abgefragt und es können Anbietergebühren entstehen. Jeder Test startet ohne Gesprächsverlauf; vorhandene Repeaterkontakte werden ausschließlich gelesen. Die Testdatenbank liegt nur im Arbeitsspeicher, die echte Sendewarteschlange bleibt unverändert. Es wird keine Funkhardware geöffnet und nichts ausgesendet. Die Vorschau prüft keine Funkzustellung. Ein Test dauert höchstens 60 Sekunden, nur ein Test läuft gleichzeitig. Verbundene Tests haben eigene Limits pro Dashboard-Prozess, getrennt vom Funkdienst. Jeder tatsächliche Modellaufruf einschließlich Wiederholungen zählt gegen diese Limits.

## DAB+ ASA ohne Internet

Profil für **Nooelec RTL-SDR V5**, optionale RTL-SDR-Treiber und eine lokale Offline-Warnschnittstelle mit Zeitprüfung, Zielregionen, Aktualisierungen und Duplikatschutz. **Für echten ASA-Empfang ist zusätzlich ein separat eingerichteter ASA-fähiger Decoderadapter erforderlich.** Ein Stick oder gewöhnlicher DAB-Decoder allein bestätigt keinen ASA-Empfang. [Empfang, Voraussetzungen und Adapterformat](docs/dab-asa.md).

## Private Kanäle und Erinnerung

Hashtag-Kanäle erhalten ihren Schlüssel aus dem Namen. Für einen privaten Kanal einen Namen ohne `#` und `secret_env = "MESHCORE_CHANNEL_IDEENWERFT"` eintragen. Die entsprechende Variable in `.env` enthält genau 32 Hex-Zeichen. Bestehende Geräteschlüssel werden niemals überschrieben.

Das persönliche Memory berücksichtigt bis zu zwölf vorherige Nachrichten desselben sichtbaren Funknamens über konfigurierte Kanäle hinweg, auch ohne direkte Anrede. Public und eigene Nachrichten bleiben ausgeschlossen. Die Aufbewahrung beträgt standardmäßig 24 Stunden; `history_turns = 0` deaktiviert die Kontextnutzung. Anzeigenamen sind nicht authentifiziert: gleiche Namen teilen Erinnerung. Keine Geheimnisse über Funk mitteilen. Dauerhafte Rollen- und Regelanweisungen werden aus dem Kontext gefiltert; der Filter ist kein vollständiger Schutz gegen beliebige Prompt-Injections.

## Aktualisierung auf 0.3

Vor einem Update laufenden Bot und Dashboard stoppen und Daten/Einstellungen sichern. Danach den Installer ausführen und die gewünschten Dienste wieder starten. Die Datenbank wird auf Schema 2 erweitert; ein Downgrade benötigt die Sicherung. Alte `#bot`-Konfigurationen mit Stichwort KI werden beim Laden auf Erwähnungen umgestellt. Bereits konfigurierte Sendeabstände bleiben bestehen; für das Echtbetrieb-Profil `send_interval_seconds = 10` und `receive_quiet_seconds = 2` einstellen.

Der Installer erzeugt ohne Funkzugriff eine schreibgeschützt lesbare Dashboard-Statusdatei. Im Betrieb wird sie alle 30 Sekunden erneuert. Manuell: `meshcore-ai snapshot --config /etc/meshcore-ai-bot/config.toml` als Dienstbenutzer ausführen.
