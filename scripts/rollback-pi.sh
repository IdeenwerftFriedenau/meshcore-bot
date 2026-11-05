#!/usr/bin/env bash
set -euo pipefail
[[ ${EUID} -eq 0 ]] || { echo 'Mit sudo ausführen.' >&2; exit 1; }
for SERVICE in meshcore-ai-bot meshcore-dashboard; do
  if systemctl is-active --quiet "$SERVICE"; then
    echo "Vor der Wiederherstellung bitte $SERVICE stoppen." >&2
    exit 1
  fi
done
exec 9>/run/lock/meshcore-ai-install.lock
flock -n 9 || { echo 'Eine Installation oder Wiederherstellung läuft bereits.' >&2; exit 1; }
# Restores code selection, service units, configuration AND database to the checkpoint.
python3 /opt/meshcore-ai-bot/deployment.py restore
systemctl daemon-reload
printf '%s\n' 'Vorherige Version, Einstellungen und Datenstand wiederhergestellt.' 'Kein Dienst wurde gestartet.'
