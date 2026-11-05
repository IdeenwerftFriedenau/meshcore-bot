#!/usr/bin/env bash
set -euo pipefail
[[ ${EUID} -eq 0 ]] || { echo 'Bitte mit sudo bash scripts/install-pi.sh starten.' >&2; exit 1; }
SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
APP_ROOT=/opt/meshcore-ai-bot
for SERVICE in meshcore-ai-bot meshcore-dashboard; do
  if systemctl is-active --quiet "$SERVICE"; then
    echo "Vor dem Update bitte $SERVICE stoppen: sudo systemctl stop $SERVICE" >&2
    exit 1
  fi
done
# Serialize installers and rollback operations without holding the radio instance lock.
exec 9>/run/lock/meshcore-ai-install.lock
flock -n 9 || { echo 'Eine Installation oder Wiederherstellung läuft bereits.' >&2; exit 1; }
apt-get update
apt-get install -y python3 python3-venv python3-pip ca-certificates
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ erforderlich"'
id meshcore-ai >/dev/null 2>&1 || useradd --system --user-group --home-dir /var/lib/meshcore-ai-bot --shell /usr/sbin/nologin meshcore-ai
usermod -a -G dialout meshcore-ai
install -d -m 755 "$APP_ROOT" "$APP_ROOT/releases"
RELEASE=$(mktemp -d "$APP_ROOT/releases/release-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
chmod 755 "$RELEASE"
cp -r "$SOURCE_DIR/src" "$RELEASE/"
install -m 644 "$SOURCE_DIR/pyproject.toml" "$SOURCE_DIR/README.md" "$RELEASE/"
python3 -m venv "$RELEASE/.venv"
"$RELEASE/.venv/bin/python" -m pip install "$RELEASE[radio]"
install -m 644 "$SOURCE_DIR/src/meshcore_ai_bot/deployment.py" "$APP_ROOT/deployment.py"
install -m 755 "$SOURCE_DIR/scripts/rollback-pi.sh" "$APP_ROOT/rollback-pi.sh"
install -d -m 700 /var/backups/meshcore-ai-bot
CHECKPOINT=/var/backups/meshcore-ai-bot/$(basename "$RELEASE")
python3 "$APP_ROOT/deployment.py" backup --backup "$CHECKPOINT"
restore_on_error() {
  local RESULT=$?
  trap - ERR
  echo "Update fehlgeschlagen. Stelle Einstellungen, Daten und vorherige Version aus $CHECKPOINT wieder her." >&2
  python3 "$APP_ROOT/deployment.py" restore --backup "$CHECKPOINT" || {
    echo "Automatische Wiederherstellung fehlgeschlagen; Sicherung: $CHECKPOINT" >&2
    exit 1
  }
  systemctl daemon-reload
  exit "$RESULT"
}
trap restore_on_error ERR
install -d -o meshcore-ai -g meshcore-ai -m 700 /etc/meshcore-ai-bot /var/lib/meshcore-ai-bot
if [[ ! -e /etc/meshcore-ai-bot/config.toml ]]; then
  install -o meshcore-ai -g meshcore-ai -m 600 "$SOURCE_DIR/config.example.toml" /etc/meshcore-ai-bot/config.toml
fi
if [[ ! -e /etc/meshcore-ai-bot/.env ]]; then
  install -o meshcore-ai -g meshcore-ai -m 600 "$SOURCE_DIR/.env.example" /etc/meshcore-ai-bot/.env
fi
# Set absolute runtime paths for new installations; retain existing custom paths.
python3 - <<'PYCONFIG'
import tomllib
from pathlib import Path
path=Path('/etc/meshcore-ai-bot/config.toml')
raw=path.read_text()
raw=raw.replace('path = "data/bot.sqlite3"', 'path = "/var/lib/meshcore-ai-bot/bot.sqlite3"')
raw=raw.replace('inbox = "data/asa"', 'inbox = "/var/lib/meshcore-ai-bot/asa"')
if 'asa' not in tomllib.loads(raw):
    raw+='\n[asa]\nenabled = false\ninbox = "/var/lib/meshcore-ai-bot/asa"\n'
path.write_text(raw)
PYCONFIG
chown meshcore-ai:meshcore-ai /etc/meshcore-ai-bot/config.toml /etc/meshcore-ai-bot/.env
chmod 600 /etc/meshcore-ai-bot/config.toml /etc/meshcore-ai-bot/.env
install -d -o meshcore-ai -g meshcore-ai -m 700 /var/lib/meshcore-ai-bot/asa
# Migration, offline snapshot and health checks must pass before switching the release.
runuser -u meshcore-ai -- env MESHCORE_AI_DB_PATH=/var/lib/meshcore-ai-bot/bot.sqlite3 MESHCORE_AI_ASA_INBOX=/var/lib/meshcore-ai-bot/asa "$RELEASE/.venv/bin/meshcore-ai" snapshot --config /etc/meshcore-ai-bot/config.toml
runuser -u meshcore-ai -- env MESHCORE_AI_DB_PATH=/var/lib/meshcore-ai-bot/bot.sqlite3 MESHCORE_AI_ASA_INBOX=/var/lib/meshcore-ai-bot/asa "$RELEASE/.venv/bin/meshcore-ai" doctor --config /etc/meshcore-ai-bot/config.toml
install -m 644 "$SOURCE_DIR/deploy/meshcore-ai-bot.service" /etc/systemd/system/meshcore-ai-bot.service
install -m 644 "$SOURCE_DIR/deploy/meshcore-dashboard.service" /etc/systemd/system/meshcore-dashboard.service
python3 "$APP_ROOT/deployment.py" activate --release "$RELEASE"
systemctl daemon-reload
trap - ERR
printf '%s\n' "Installiert: $RELEASE" "Sicherung: $CHECKPOINT" 'Bitte config.toml und .env anpassen.' 'Funk optional starten: sudo systemctl enable --now meshcore-ai-bot' 'Dashboard optional starten: sudo systemctl enable --now meshcore-dashboard' 'Rollback bei gestoppten Diensten: sudo bash /opt/meshcore-ai-bot/rollback-pi.sh' 'Kein Dienst wurde gestartet.'
