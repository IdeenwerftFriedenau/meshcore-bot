# Pi Console

Das Dashboard läuft lokal auf dem Raspberry Pi, ohne externe Webseiten, Schriften oder CDNs. Es öffnet keine Funkhardware und startet den Funkdienst nicht.

Nach der Installation:

```sh
sudo systemctl enable --now meshcore-dashboard
# Auf deinem Computer (pi durch den eigenen SSH-Benutzer ersetzen):
ssh -L 8765:127.0.0.1:8765 pi@raspberrypi.local
```

Danach `http://127.0.0.1:8765` im Browser öffnen. Der Dienst bindet standardmäßig nur Loopback. Für Entwicklung: `meshcore-ai dashboard --config config.toml`.

Station, Wetterort, USB-Pfad, KI-Modell, Verbrauchslimits, Sendeabstände, bestehende Kanalregeln und Warnquellen lassen sich einstellen. Alle 16 Bundesländer sind für DWD und BBK auswählbar, gemeinsam oder pro Quelle. ASA hat eine eigene Auswahl. Public ist gesperrt, #bot antwortet nur mit KI, Fragekanäle erhalten keine automatischen Meldungen.

Speichern validiert die gesamte Konfiguration, legt `config.toml.bak` an und ersetzt die Datei atomar. API-Schlüssel werden nur geschrieben, nie zurückgegeben. Eine leere Schlüsseleingabe erhält den bestehenden Wert. Private Kanalschlüssel bleiben erhalten. Gleichzeitige Änderungen werden erkannt; in diesem Fall neu laden. Die Dateien bleiben nur für den Dienstbenutzer lesbar. Umgebungsvariablen können einzelne Einstellungen übersteuern und werden im Dashboard benannt.

Der laufende Bot erkennt gültige Änderungen alle zwei Sekunden, unterbricht die alte Sitzung und verbindet sich mit den neuen Regeln nach der regulären Wartezeit erneut. Bei geänderter Quelle/Region werden deren alte Warteschlangen verworfen. Ungültige Dateien lassen die letzte funktionierende Einstellung aktiv. Ein geänderter Datenbankpfad erfordert einen Neustart. Der Dashboarddienst liest Statusdaten nur aus einer bereits vorhandenen Datenbank. Ein aktueller Lebensnachweis ist für „verbunden“ erforderlich.

## Zugriffsschutz

Loopback und SSH-Tunnel sind die empfohlene Variante. JSON-Speichervorgänge sind durch Ursprungskontrolle und ein Sitzungstoken geschützt; lokale Hostprüfung verhindert DNS-Rebinding. Keine beliebigen Dateipfade, Befehle oder neuen Kanalschlüssel werden über die Oberfläche angenommen.

Optional kann `dashboard.host = "0.0.0.0"` gesetzt werden. Dafür ist ein mindestens 24 Zeichen langer Zugangscode in der Umgebungsvariablen `MESHCORE_DASHBOARD_TOKEN` erforderlich (bei Verwendung des Diensts auch in einer systemd-Umgebungsdatei oder einem Drop-in setzen). Der Zugangscode wird im Browser nur für die aktuelle Sitzung gespeichert. Direktes HTTP im LAN ist unverschlüsselt: für Zugriff außerhalb des lokalen Rechners SSH oder einen separat abgesicherten HTTPS-Proxy verwenden. Port nicht öffentlich weiterleiten.
