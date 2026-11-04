# Pi Console

Das Dashboard läuft lokal auf dem Raspberry Pi, ohne externe Webseiten, Schriften oder CDNs. Es öffnet keine Funkhardware und startet den Funkdienst nicht.

Nach der Installation:

```sh
sudo systemctl enable --now meshcore-dashboard
# Auf deinem Computer (pi durch den eigenen SSH-Benutzer ersetzen):
ssh -L 8765:127.0.0.1:8765 pi@raspberrypi.local
```

Danach `http://127.0.0.1:8765` im Browser öffnen. Der Dienst bindet standardmäßig nur Loopback. Für Entwicklung: `meshcore-ai dashboard --config config.toml`.

Station, Wetterort, USB-Pfad, KI-Modell, Verbrauchslimits, Sendeabstände, bestehende Kanalregeln und Warnquellen lassen sich einstellen. Alle 16 Bundesländer sind für DWD und BBK auswählbar, gemeinsam oder pro Quelle. ASA hat eine eigene Auswahl. Public ist gesperrt, #bot antwortet nur auf Erwähnungen, Fragekanäle erhalten keine automatischen Meldungen.

Speichern validiert die gesamte Konfiguration, legt `config.toml.bak` an und ersetzt die Datei atomar. API-Schlüssel werden nur geschrieben, nie zurückgegeben. Eine leere Schlüsseleingabe erhält den bestehenden Wert. Private Kanalschlüssel bleiben erhalten. Im Kanal #bot werden nur Aus und Erwähnung angeboten, um Kollisionen mit anderen Bots zu vermeiden. Gleichzeitige Änderungen werden erkannt; in diesem Fall neu laden. Die Dateien bleiben nur für den Dienstbenutzer lesbar. Umgebungsvariablen können einzelne Einstellungen übersteuern und werden im Dashboard benannt.

Der laufende Bot erkennt gültige Änderungen alle zwei Sekunden, unterbricht die alte Sitzung und verbindet sich mit den neuen Regeln nach der regulären Wartezeit erneut. Bei geänderter Quelle/Region werden deren alte Warteschlangen verworfen. Ungültige Dateien lassen die letzte funktionierende Einstellung aktiv. Ein geänderter Datenbankpfad erfordert einen Neustart. Der Dashboarddienst liest eine vom Bot atomar veröffentlichte Statusdatei (höchstens etwa 30 Sekunden alt), einschließlich Repeaterkontakten. Der Installer legt die erste Datei ohne Funkzugriff an. Ein aktueller Lebensnachweis ist für „verbunden“ erforderlich.

## Zugriffsschutz

Loopback und SSH-Tunnel sind die empfohlene Variante. JSON-Speichervorgänge sind durch Ursprungskontrolle und ein Sitzungstoken geschützt; lokale Hostprüfung verhindert DNS-Rebinding. Keine beliebigen Dateipfade, Befehle oder neuen Kanalschlüssel werden über die Oberfläche angenommen.

Optional kann `dashboard.host = "0.0.0.0"` gesetzt werden. Dafür ist ein mindestens 24 Zeichen langer Zugangscode in der Umgebungsvariablen `MESHCORE_DASHBOARD_TOKEN` erforderlich (in `.env` neben `config.toml` oder ausdrücklich in der Dienstumgebung setzen; anschließend Dashboard neu starten). Der Zugangscode wird im Browser nur für die aktuelle Sitzung gespeichert. Direktes HTTP im LAN ist unverschlüsselt: für Zugriff außerhalb des lokalen Rechners SSH oder einen separat abgesicherten HTTPS-Proxy verwenden. Port nicht öffentlich weiterleiten.


Text-Testbereich: Unter **Text testen** eine Nachricht, einen vorhandenen Kanal und einen Testabsender wählen. Die Vorschau verwendet die gespeicherten Kanalregeln, denselben Antwortprozess und dieselbe Paketaufteilung wie der Bot. Public bleibt auch im Test stumm, #bot benötigt eine Erwähnung. Standardmäßig läuft der Test offline. Für freie KI-Fragen oder Wetterdaten Offline ausschalten; dabei werden die konfigurierten Dienste abgefragt und es können Anbietergebühren entstehen. Jeder Test startet ohne Gesprächsverlauf; vorhandene Repeaterkontakte werden ausschließlich gelesen. Die Testdatenbank liegt nur im Arbeitsspeicher, die echte Sendewarteschlange bleibt unverändert. Es wird keine Funkhardware geöffnet und nichts ausgesendet. Die Vorschau prüft keine Funkzustellung. Ein Test dauert höchstens 60 Sekunden, nur ein Test läuft gleichzeitig. Verbundene Tests haben eigene Limits gemäß KI-Konfiguration; diese gelten pro Dashboard-Prozess und getrennt vom Funkdienst. Auch zusätzliche Modellrunden und Wiederholungen zählen als tatsächliche Modellaufrufe.
