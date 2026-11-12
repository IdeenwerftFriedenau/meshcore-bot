# Gateway auf dem Pi (`~/meshcore-gateway`)

Dieser Ordner enthält den vollständigen Gateway-Code so, wie er auf dem Pi läuft. Der Dienst
`meshcore-gateway` besitzt das MeshCore-Funkgerät am USB-Port und lädt den Bot aus diesem Repository
über `pi_gateway_bridge.py`.

| Datei | Aufgabe |
| --- | --- |
| `collector.py` | Hauptprogramm: USB-Verbindung, Empfang, Kontakte, Sendetakt, Start der Bridge |
| `radio_bot.py` | Erkennung von Anreden, Aufteilen langer Antworten, Eingangsprotokoll |
| `radio_pacing.py` | Gemeinsamer Sendetakt mit Empfangsfenster zwischen den Paketen |
| `direct_contacts.py` | Kontakte für Direktnachrichten vorbereiten |
| `admin_sender.py` | Nachrichten aus der App-Warteschlange senden, Kontaktarchiv |
| `channel_mirror.py`, `receive_channels.py` | Empfangskanäle und Spiegelung an die App |
| `../pi_gateway_bridge.py` | Verbindung zwischen Collector und Bot-Paket |
| `check.py`, `contacts-check.py` | Handprüfungen des Funkgeräts (nur bei gestopptem Gateway) |
| `meshcore-gateway.service` und `meshcore-gateway.service.d/` | systemd-Dienst samt Ergänzung für den lokalen Bot |
| `gateway.env.example` | Vorlage für die Zugangsdaten; die echte `gateway.env` bleibt nur auf dem Pi |

## Wiederherstellung auf einem neuen Pi

1. Bot-Paket nach `/opt/meshcore-ai-bot` installieren (`scripts/install-pi.sh`), den eigenständigen
   Dienst `meshcore-ai-bot` aber nicht starten.
2. Die Python-Dateien dieses Ordners und `deploy/pi_gateway_bridge.py` nach `/home/marc/meshcore-gateway/`
   kopieren.
3. `gateway.env.example` als `gateway.env` daneben legen, Werte eintragen, Rechte auf 600 setzen.
4. `meshcore-gateway.service` und den Ordner `meshcore-gateway.service.d` nach `/etc/systemd/system/`
   kopieren, dann `sudo systemctl daemon-reload && sudo systemctl enable --now meshcore-gateway`.

Datenbanken unter `/var/lib/meshcore-gateway` und `/var/lib/meshcore-ai-bot` kommen aus der Sicherung,
nicht aus GitHub.

## Änderungen

Änderungen am Gateway hier im Repository vornehmen und mit `deploy/deploy.sh` ausrollen. Das Skript
aktualisiert diese Dateien auf dem Pi nur, wenn sie dort nicht von Hand verändert wurden.
Die Dateien sind bewusst unverändert vom Pi übernommen und deshalb von der Stilprüfung ausgenommen.
