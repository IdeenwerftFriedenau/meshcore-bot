# Funk-Suchlauf (Pi-Console, Port 8091)

Band-Suchlauf mit rtl_power, Reinhören (rtl_fm → MP3), Funksensoren mit rtl_433 und das rbb-Nachrichtenarchiv.
Hält dafür kurz `ideenwerft-dab-monitor` und `ideenwerft-dab-news` an und startet sie danach wieder.

- `radio_scan.py` – Suchlauf, Reinhören, Sensor-Suche (wird vom Dashboard importiert)
- `local_dashboard.py` – Stand des Pi-Dashboards (`~/meshcore-gateway/local_dashboard.py`) mit Funk-API
- `console/` – Oberfläche (index.html, console.js, funk-scan.js, rbb-archiv.js, console.css)
- `rbb_news.py` – rbb-Aufnahme mit dauerhaftem Archiv (Kopie unter `/opt/ideenwerft-rbb-archive`)
- `deploy-funk-scan.sh` – prüft, sichert nach `/var/backups/meshcore-pi-v04/before-funk-scan-<TS>`, spielt ein, testet; `rollback <backup>` stellt zurück
- `install-from-github.sh <commit>` – holt dieses Verzeichnis von GitHub und startet das Einspielen

## Empfangswächter (welle-cli)

welle-cli schließt beendete HTTP-Verbindungen nicht (CLOSE_WAIT). Bei 1024 offenen Dateien nimmt es
keine Verbindung mehr an, und rbb-Aufnahmen enden mit „kein Ton · ConnectTimeout“. `deploy/dab/dab_monitor.py`
startet welle-cli deshalb selbst neu: sofort bei „Too many open files“ oder sechs fehlgeschlagenen
lokalen Abfragen, vorsorglich ab 800 offenen Dateien (nie zwischen HH:59:30 und HH:05:50). Die rbb-Aufnahme
wartet bis 30 s auf den Empfänger, setzt bei abgebrochenem Stream neu an und behält fertige Aufnahmen.
Einspielen: `deploy/dab/update-empfang.sh main` (Backup unter `/var/backups/meshcore-pi-v04/before-empfang-*`).
