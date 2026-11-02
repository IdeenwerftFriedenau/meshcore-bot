#!/usr/bin/env bash
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then
  echo 'Bitte mit sudo bash scripts/install-pi.sh starten.' >&2
  exit 1
fi
SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
apt-get update
apt-get install -y python3 python3-venv python3-pip ca-certificates
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ erforderlich"'
id meshcore-ai >/dev/null 2>&1 || useradd --system --user-group --home-dir /var/lib/meshcore-ai-bot --shell /usr/sbin/nologin meshcore-ai
usermod -a -G dialout meshcore-ai
install -d -m 755 /opt/meshcore-ai-bot /etc/meshcore-ai-bot
install -d -o meshcore-ai -g meshcore-ai -m 700 /var/lib/meshcore-ai-bot
# Only package files are copied. Never copy a developer's .env, database or virtual environment.
if [[ "$SOURCE_DIR" != /opt/meshcore-ai-bot ]]; then
  cp -r "$SOURCE_DIR/src" /opt/meshcore-ai-bot/
  install -m 644 "$SOURCE_DIR/pyproject.toml" "$SOURCE_DIR/README.md" /opt/meshcore-ai-bot/
fi
python3 -m venv /opt/meshcore-ai-bot/.venv
/opt/meshcore-ai-bot/.venv/bin/python -m pip install --upgrade pip
/opt/meshcore-ai-bot/.venv/bin/python -m pip install '/opt/meshcore-ai-bot[radio]'
if [[ ! -e /etc/meshcore-ai-bot/config.toml ]]; then
  install -o root -g meshcore-ai -m 640 "$SOURCE_DIR/config.example.toml" /etc/meshcore-ai-bot/config.toml
  sed -i 's|path = "data/bot.sqlite3"|path = "/var/lib/meshcore-ai-bot/bot.sqlite3"|' /etc/meshcore-ai-bot/config.toml
fi
if [[ ! -e /etc/meshcore-ai-bot/.env ]]; then
  install -o root -g meshcore-ai -m 640 "$SOURCE_DIR/.env.example" /etc/meshcore-ai-bot/.env
fi
install -m 644 "$SOURCE_DIR/deploy/meshcore-ai-bot.service" /etc/systemd/system/meshcore-ai-bot.service
systemctl daemon-reload
printf '%s\n' 'Installiert. Bitte /etc/meshcore-ai-bot/config.toml und .env anpassen.' 'Danach: sudo systemctl enable --now meshcore-ai-bot' 'Der Funkdienst wurde nicht gestartet.'
