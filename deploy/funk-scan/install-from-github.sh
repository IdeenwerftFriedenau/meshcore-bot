#!/usr/bin/env bash
# Funk-Suchlauf direkt auf dem Pi von GitHub holen und einspielen (ohne Mac/scp).
#   curl -fsSL https://raw.githubusercontent.com/IdeenwerftFriedenau/meshcore-bot/<commit>/deploy/funk-scan/install-from-github.sh -o /tmp/funk.sh && bash /tmp/funk.sh <commit>
set -euo pipefail
C=${1:?Commit angeben}
[[ "$C" =~ ^[0-9a-f]{7,40}$ ]] || { echo "Ungültiger Commit: $C" >&2; exit 1; }
D=/tmp/funk-gh-$C
rm -rf "$D"; mkdir -p "$D"
curl -fsSL "https://codeload.github.com/IdeenwerftFriedenau/meshcore-bot/tar.gz/$C" \
  | tar xz -C "$D" --strip-components=2 --wildcards '*/deploy/funk-scan'
echo "Paket von GitHub ($C) geholt: $(ls "$D/funk-scan" | tr '\n' ' ')"
# Läuft gerade jemand im Reinhören? Dann erst beenden (sonst bricht das Einspielen ab).
curl -s -m 3 -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8091/api/funk/listen/stop >/dev/null 2>&1 && sleep 5 || true
bash "$D/funk-scan/deploy-funk-scan.sh" "${2:-}"
