#!/usr/bin/env bash
# Rollt die Alltagsdaten für #fragdenbot aus (Warnungen, Störungen, Weltraumwetter, Fußball, Luft, Pollen, Feiertage, Sprit). Als marc ausführen, nutzt sudo selbst.
# Holt den Code vom festen Commit 34273e11f2700729085af44c60ec532b5d95dff9 auf GitHub und prüft jede Datei per SHA-256.
#   bash deploy.sh            -> Backup, Dateien, Live-Test (ohne Neustart)
#   bash deploy.sh restart    -> zusätzlich meshcore-gateway neu starten (nötig, damit es wirkt)
#   bash deploy.sh rollback <backupdir>
set -euo pipefail
COMMIT=34273e11f2700729085af44c60ec532b5d95dff9
RAW=https://raw.githubusercontent.com/IdeenwerftFriedenau/meshcore-bot
BASE=/opt/meshcore-ai-bot
PY=$BASE/current/.venv/bin/python
CONF=/etc/meshcore-ai-bot/config.toml
SP=$($PY -c 'import meshcore_ai_bot,os;print(os.path.dirname(meshcore_ai_bot.__file__))')
FILES=(assistant.py tools.py alltag.py research.py)
TESTS=(test_alltag.py)

if [[ "${1:-}" == rollback ]]; then
  BK=${2:?Backup-Ordner angeben}
  sudo rm -rf "$SP" && sudo cp -a "$BK/meshcore_ai_bot" "$SP"
  sudo ln -sfn "$(cat "$BK/source-link.txt")" "$BASE/source"
  sudo systemctl restart meshcore-gateway
  sleep 5; systemctl is-active meshcore-gateway
  echo "Rollback fertig."
  exit 0
fi

declare -A WANT=(
  [assistant.py]="6821a46b537d83f84b458e776defaf5139ba72988e5da99e3da42b9823825de4"
  [tools.py]="5dc25bef9f1d4c0da8aca2f797d4429424a8da31ad465d5164ecbadc8e4062ed"
  [alltag.py]="058bd624d0cb8a960a6f79097060dd7c56a68bf63e7fb1812c2fa43d0dbb6640"
  [research.py]="ab10683ebcb770b9d230474da1083d3378b93b98cd2a5c96f8a792f66259dbcb"
  [test_alltag.py]="72ca4a965980271e016d6d11d1e531b68d66a5558981a86657a7a67e7bc63e2c"
)
# Erwarteter Vorstand auf dem Pi: Insider-Auslieferung 5a54619 (= main 6a6d725) oder Alltagsdaten 7977e35.
declare -A EXPECT=(
  [assistant.py]="89d07cfb69a7d6b5914d71c0dc4ad9d3d2e089b9fa31a5c93e7cf11dff979ffe 6821a46b537d83f84b458e776defaf5139ba72988e5da99e3da42b9823825de4"
  [tools.py]="5bec21a0a94357bb2d8ffe1ccb6857dc18afe9469e7e942fa01d3bd1d8f0a7c4 5dc25bef9f1d4c0da8aca2f797d4429424a8da31ad465d5164ecbadc8e4062ed"
  [alltag.py]="neu 681bf386a5f9580a9b969ea778be3c35de44fe623a37bc7643d862444284c0d1"
  [research.py]="5026601bb1a0b84049c9d2ea224c2a4429190a11289609778005ee25880b2a7e"
)

PKG=$(mktemp -d)
trap 'rm -rf "$PKG"' EXIT
for f in "${FILES[@]}" "${TESTS[@]}"; do
  dir=src/meshcore_ai_bot; [[ $f == test_* ]] && dir=tests
  curl -fsSL "$RAW/$COMMIT/$dir/$f" -o "$PKG/$f"
  got=$(sha256sum "$PKG/$f" | cut -d' ' -f1)
  [[ "$got" == "${WANT[$f]}" ]] || { echo "ABBRUCH: $f vom Download stimmt nicht ($got)." >&2; exit 1; }
done
echo "Download geprüft ($COMMIT)."

TODO=0
for f in "${FILES[@]}"; do
  new=$(sha256sum "$PKG/$f" | cut -d' ' -f1)
  if [[ -f "$SP/$f" ]]; then have=$(sha256sum "$SP/$f" | cut -d' ' -f1); else have=neu; fi
  [[ "$have" == "$new" ]] && continue
  if [[ " ${EXPECT[$f]} " != *" $have "* ]]; then
    echo "ABBRUCH: $SP/$f hat unerwarteten Stand ($have). Nichts geändert." >&2
    exit 1
  fi
  TODO=1
done

if [[ $TODO == 1 ]]; then
  TS=$(date -u +%Y%m%dT%H%M%SZ)
  BK=/var/backups/meshcore-pi-v04/before-alltag-$TS
  sudo mkdir -p "$BK"
  sudo cp -a "$SP" "$BK/meshcore_ai_bot"
  sudo cp -a "$CONF" "$BK/config.toml"
  readlink -f "$BASE/source" | sudo tee "$BK/source-link.txt" >/dev/null
  echo "BACKUP=$BK"
  echo "Rückweg falls nötig: bash $0 rollback $BK"

  OWNER=$(stat -c %U:%G "$SP/tools.py")
  for f in "${FILES[@]}"; do
    sudo install -m 644 -o "${OWNER%%:*}" -g "${OWNER##*:}" "$PKG/$f" "$SP/$f"
  done
  for f in "${FILES[@]}"; do sudo "$PY" -m py_compile "$SP/$f"; done

  OLD=$(readlink -f "$BASE/source")
  NEW="$BASE/sources/${COMMIT:0:7}-alltag"
  [[ -d "$NEW" ]] || sudo cp -a "$OLD" "$NEW"
  for f in "${FILES[@]}"; do sudo cp "$PKG/$f" "$NEW/src/meshcore_ai_bot/$f"; done
  for f in "${TESTS[@]}"; do sudo cp "$PKG/$f" "$NEW/tests/"; done
  echo "$COMMIT" | sudo tee "$NEW/source-commit.txt" >/dev/null
  sudo ln -sfn "$NEW" "$BASE/source"
  echo "SOURCE=$NEW"
else
  echo "Dateien sind schon aktuell."
fi

curl -fsSL "$RAW/$COMMIT/deploy/alltag/live_test.py" -o "$PKG/live_test.py" 2>/dev/null \
  || curl -fsSL "$RAW/claude/project-thread-5vd676/deploy/alltag/live_test.py" -o "$PKG/live_test.py"
sudo "$PY" "$PKG/live_test.py" "$CONF"

if [[ "${1:-}" == restart ]]; then
  sudo systemctl restart meshcore-gateway
  sleep 10
  systemctl is-active meshcore-gateway
  journalctl -u meshcore-gateway --since "-1min" --no-pager | tail -15
fi
