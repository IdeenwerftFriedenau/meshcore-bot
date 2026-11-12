#!/usr/bin/env bash
# Zeigt das Ergebnis der letzten Quellenprüfung.   bash stand.sh
set -euo pipefail
F=/var/lib/meshcore-quellen/status.json
[[ -f $F ]] || { echo "Noch keine Prüfung gelaufen. Start: sudo systemctl start meshcore-quellen"; exit 0; }
python3 - "$F" <<'PY'
import json, sys, time
s = json.load(open(sys.argv[1], encoding="utf-8"))
print(time.strftime("Letzte Prüfung: %d.%m.%Y %H:%M", time.localtime(s["checked"])))
print(f"{s['total'] - len(s['failed'])}/{s['total']} Quellen in Ordnung.")
for label, reason in s["failed"].items():
    since = time.strftime("%d.%m. %H:%M", time.localtime(s["since"].get(label, s["checked"])))
    print(f"GESTÖRT seit {since}: {label}: {reason}")
PY
