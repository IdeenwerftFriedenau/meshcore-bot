# Funk-Suchlauf (Pi-Console, Port 8091)

Band-Suchlauf mit rtl_power, Reinhören (rtl_fm → MP3), Funksensoren mit rtl_433 und das rbb-Nachrichtenarchiv.
Hält dafür kurz `ideenwerft-dab-monitor` und `ideenwerft-dab-news` an und startet sie danach wieder.

- `radio_scan.py` – Suchlauf, Reinhören, Sensor-Suche (wird vom Dashboard importiert)
- `local_dashboard.py` – Stand des Pi-Dashboards (`~/meshcore-gateway/local_dashboard.py`) mit Funk-API
- `console/` – Oberfläche (index.html, console.js, funk-scan.js, rbb-archiv.js, console.css)
- `rbb_news.py` – rbb-Aufnahme mit dauerhaftem Archiv (Kopie unter `/opt/ideenwerft-rbb-archive`)
- `deploy-funk-scan.sh` – prüft, sichert nach `/var/backups/meshcore-pi-v04/before-funk-scan-<TS>`, spielt ein, testet; `rollback <backup>` stellt zurück
- `install-from-github.sh <commit>` – holt dieses Verzeichnis von GitHub und startet das Einspielen
