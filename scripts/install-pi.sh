#!/usr/bin/env bash
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then
  echo 'Bitte mit sudo bash scripts/install-pi.sh starten.' >&2
  exit 1
fi
SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
for SERVICE in meshcore-ai-bot meshcore-dashboard; do
  if systemctl is-active --quiet "$SERVICE"; then
    echo "Vor dem Update bitte $SERVICE stoppen: sudo systemctl stop $SERVICE" >&2
    exit 1
  fi
done
apt-get update
apt-get install -y python3 python3-venv python3-pip ca-certificates
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ erforderlich"'
id meshcore-ai >/dev/null 2>&1 || useradd --system --user-group --home-dir /var/lib/meshcore-ai-bot --shell /usr/sbin/nologin meshcore-ai
usermod -a -G dialout meshcore-ai
install -d -m 755 /opt/meshcore-ai-bot
install -d -o meshcore-ai -g meshcore-ai -m 700 /etc/meshcore-ai-bot
install -d -o meshcore-ai -g meshcore-ai -m 700 /var/lib/meshcore-ai-bot
# Only package files are copied. Never copy a developer's .env, database or virtual environment.
if [[ "$SOURCE_DIR" != /opt/meshcore-ai-bot ]]; then
  # Stage a clean tree first, so deleted modules cannot survive an upgrade.
  STAGED_SRC=$(mktemp -d /opt/meshcore-ai-bot/.src-XXXXXX)
  cp -r "$SOURCE_DIR/src/." "$STAGED_SRC/"
  rm -rf /opt/meshcore-ai-bot/src
  mv "$STAGED_SRC" /opt/meshcore-ai-bot/src
  install -m 644 "$SOURCE_DIR/pyproject.toml" "$SOURCE_DIR/README.md" /opt/meshcore-ai-bot/
fi
python3 -m venv /opt/meshcore-ai-bot/.venv
/opt/meshcore-ai-bot/.venv/bin/python -m pip install --upgrade pip
/opt/meshcore-ai-bot/.venv/bin/python -m pip install '/opt/meshcore-ai-bot[radio]'
if [[ ! -e /etc/meshcore-ai-bot/config.toml ]]; then
  install -o meshcore-ai -g meshcore-ai -m 600 "$SOURCE_DIR/config.example.toml" /etc/meshcore-ai-bot/config.toml
  sed -i 's|path = "data/bot.sqlite3"|path = "/var/lib/meshcore-ai-bot/bot.sqlite3"|' /etc/meshcore-ai-bot/config.toml
  sed -i 's|inbox = "data/asa"|inbox = "/var/lib/meshcore-ai-bot/asa"|' /etc/meshcore-ai-bot/config.toml
fi
if [[ ! -e /etc/meshcore-ai-bot/.env ]]; then
  install -o meshcore-ai -g meshcore-ai -m 600 "$SOURCE_DIR/.env.example" /etc/meshcore-ai-bot/.env
fi
install -m 644 "$SOURCE_DIR/deploy/meshcore-ai-bot.service" /etc/systemd/system/meshcore-ai-bot.service
# Add an inbox to older configurations without changing any existing ASA profile.
python3 - <<'PYCONFIG'
import tomllib
from pathlib import Path
path = Path('/etc/meshcore-ai-bot/config.toml')
with path.open('rb') as stream:
    config = tomllib.load(stream)
if 'asa' not in config:
    with path.open('a') as stream:
        stream.write('\n[asa]\nenabled = false\ninbox = "/var/lib/meshcore-ai-bot/asa"\n')
PYCONFIG
chown meshcore-ai:meshcore-ai /etc/meshcore-ai-bot/config.toml /etc/meshcore-ai-bot/.env
chmod 600 /etc/meshcore-ai-bot/config.toml /etc/meshcore-ai-bot/.env
install -d -o meshcore-ai -g meshcore-ai -m 700 /var/lib/meshcore-ai-bot/asa
install -m 644 "$SOURCE_DIR/deploy/meshcore-dashboard.service" /etc/systemd/system/meshcore-dashboard.service
# Initialize the read-only dashboard snapshot without touching radio hardware.
runuser -u meshcore-ai -- /opt/meshcore-ai-bot/.venv/bin/meshcore-ai snapshot --config /etc/meshcore-ai-bot/config.toml
systemctl daemon-reload
printf '%s\n' 'Installiert. Bitte /etc/meshcore-ai-bot/config.toml und .env anpassen.' 'Danach: sudo systemctl enable --now meshcore-ai-bot' 'Der Funkdienst wurde nicht gestartet.'

printf '%s\n' 'Dashboard optional starten: sudo systemctl enable --now meshcore-dashboard' 'Zugriff per SSH-Tunnel, siehe docs/dashboard.md. Funk wird dadurch nicht gestartet.'
