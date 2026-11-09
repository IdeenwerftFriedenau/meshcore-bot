#!/usr/bin/env bash
# Install Piper and the German voice for Jarvis, verify them, prepare the shared
# audio lock and the unit. Does not enable or start anything.
set -euo pipefail

SOURCE=${SOURCE:-/opt/meshcore-ai-bot/source}
TARGET=${TARGET:-/opt/meshcore-ai-bot/jarvis}
ARTIFACTS="$SOURCE/deploy/jarvis-artifacts.json"
LOCK=/var/lib/meshcore-gateway/phone-remote/audio.lock
ENVFILE=/etc/meshcore-ai-bot/jarvis.env

[ "$(id -u)" -eq 0 ] || { echo "Bitte mit sudo ausführen" >&2; exit 1; }
field() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]][sys.argv[3]])' "$ARTIFACTS" "$1" "$2"; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

fetch() { # name file checksum-command
  local url bytes
  url=$(field "$1" url); bytes=$(field "$1" bytes)
  curl -fsSL --proto '=https' --max-filesize $((bytes + 1)) -o "$work/$2" "$url"
  [ "$(stat -c %s "$work/$2")" = "$bytes" ] || { echo "$1: falsche Größe" >&2; exit 1; }
}

fetch piper piper.tgz
echo "$(field piper sha256)  $work/piper.tgz" | sha256sum -c --quiet
fetch voice voice.onnx
echo "$(field voice md5)  $work/voice.onnx" | md5sum -c --quiet
fetch voice_config voice.onnx.json
echo "$(field voice_config md5)  $work/voice.onnx.json" | md5sum -c --quiet

install -d -m 0755 "$TARGET"
rm -rf "$TARGET/piper.new"
mkdir "$TARGET/piper.new"
tar -xzf "$work/piper.tgz" -C "$TARGET/piper.new" --strip-components=1 --no-same-owner
rm -rf "$TARGET/piper.old"
[ -d "$TARGET/piper" ] && mv "$TARGET/piper" "$TARGET/piper.old"
mv "$TARGET/piper.new" "$TARGET/piper"
install -m 0644 "$work/voice.onnx" "$TARGET/de_DE-thorsten-medium.onnx"
install -m 0644 "$work/voice.onnx.json" "$TARGET/de_DE-thorsten-medium.onnx.json"
echo "Sprachausgabe-Prüfsummen (zur Kontrolle):"
sha256sum "$TARGET/de_DE-thorsten-medium.onnx"

# Shared with the browser call bridge; both run as marc.
[ -e "$LOCK" ] || install -m 0600 -o marc -g marc /dev/null "$LOCK"

if [ ! -e "$ENVFILE" ]; then
  install -m 0600 -o root -g root /dev/null "$ENVFILE"
  cat > "$ENVFILE" <<'CONF'
# Kommagetrennt; nur diese Nummern nimmt Jarvis an.
JARVIS_ALLOWED_CALLERS=
JARVIS_AI_BASE_URL=
JARVIS_AI_MODEL=gemini-2.5-flash
JARVIS_AI_API_KEY=
CONF
  echo "$ENVFILE angelegt: Nummern und KI-Zugang privat eintragen."
fi

install -m 0644 "$SOURCE/deploy/meshcore-jarvis.service" /etc/systemd/system/meshcore-jarvis.service
systemctl daemon-reload
echo "Fertig. Testen: echo 'Hallo, hier ist Jarvis.' | $TARGET/piper/piper --model $TARGET/de_DE-thorsten-medium.onnx --output_file /tmp/jarvis.wav"
echo "Starten: systemctl enable --now meshcore-jarvis.service"
