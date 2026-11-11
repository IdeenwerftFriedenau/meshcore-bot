#!/usr/bin/env bash
# Rollt Firmenkennzahlen und Insider-Geschäfte im Börsenbot aus (PR #41). Als marc ausführen, nutzt sudo selbst.
# Holt den Code vom festen Commit 5a54619c5aeafe7e8533dd387f6b3a02801ef48c auf GitHub und prüft jede Datei per SHA-256.
#   bash deploy.sh            -> Backup, Dateien, Live-Test (ohne Neustart)
#   bash deploy.sh restart    -> zusätzlich meshcore-gateway neu starten (nötig, damit es wirkt)
#   bash deploy.sh rollback <backupdir>
set -euo pipefail
COMMIT=5a54619c5aeafe7e8533dd387f6b3a02801ef48c
RAW=https://raw.githubusercontent.com/IdeenwerftFriedenau/meshcore-bot
BASE=/opt/meshcore-ai-bot
PY=$BASE/current/.venv/bin/python
CONF=/etc/meshcore-ai-bot/config.toml
SP=$($PY -c 'import meshcore_ai_bot,os;print(os.path.dirname(meshcore_ai_bot.__file__))')
FILES=(assistant.py tools.py markets.py fundamentals.py research.py insider.py)
TESTS=(test_fundamentals.py test_insider.py)

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
  [assistant.py]="89d07cfb69a7d6b5914d71c0dc4ad9d3d2e089b9fa31a5c93e7cf11dff979ffe"
  [tools.py]="5bec21a0a94357bb2d8ffe1ccb6857dc18afe9469e7e942fa01d3bd1d8f0a7c4"
  [markets.py]="fd4f35a880f4506eb078ffdbae9a4751c6aa7273c2c9cf00c40b4c86816e986a"
  [fundamentals.py]="ce8e8e5f0832a6abfb947b3637e93bf63b19fa2b93b3c586c65ea7c0adae1445"
  [research.py]="5026601bb1a0b84049c9d2ea224c2a4429190a11289609778005ee25880b2a7e"
  [insider.py]="194703b4d01a4083cb42a296d9d84dd09222ca7a4bbf9e7aaa074ba525f46ab1"
  [test_fundamentals.py]="79517f516dbb63bfff5c7e75a04fbcc8976b8594cecf667af03e4e3828383baf"
  [test_insider.py]="1022904b540936c6341c96c674a951e6ae6082ef0d3b56d7e6177590b959b2e7"
)
# Erwarteter Vorstand auf dem Pi: die Kennzahlen-Auslieferung 265329b (live seit 2026-11-11 10:33).
declare -A EXPECT=(
  [assistant.py]="4f7a08035c18e4c3f0208ab72887975c59d7ad066dc9e5aabbee2cb281be8fb6"
  [tools.py]="42efd85c0e7aabe966e01ae4ced5e8341c73b9247f6895d8b1aee50f589e5c8b"
  [markets.py]="7a1fece2877ea239189e818bcd3c49f1d2cb5113ac4650e32e0b5f58ca5fbbec"
  [fundamentals.py]="a1c618e67b14d4a9639fe8a28fbfb226d28dd2899bc79ed624c0a46f8f3a2d7f"
  [research.py]="5026601bb1a0b84049c9d2ea224c2a4429190a11289609778005ee25880b2a7e"
  [insider.py]="neu"
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
  BK=/var/backups/meshcore-pi-v04/before-insider-$TS
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
  NEW="$BASE/sources/${COMMIT:0:7}-insider"
  [[ -d "$NEW" ]] || sudo cp -a "$OLD" "$NEW"
  for f in "${FILES[@]}"; do sudo cp "$PKG/$f" "$NEW/src/meshcore_ai_bot/$f"; done
  for f in "${TESTS[@]}"; do sudo cp "$PKG/$f" "$NEW/tests/"; done
  echo "$COMMIT" | sudo tee "$NEW/source-commit.txt" >/dev/null
  sudo ln -sfn "$NEW" "$BASE/source"
  echo "SOURCE=$NEW"
else
  echo "Dateien sind schon aktuell."
fi

curl -fsSL "$RAW/$COMMIT/deploy/kennzahlen/live_test.py" -o "$PKG/live_test.py" 2>/dev/null \
  || curl -fsSL "$RAW/claude/project-thread-5vd676/deploy/kennzahlen/live_test.py" -o "$PKG/live_test.py"
sudo "$PY" "$PKG/live_test.py"

if [[ "${1:-}" == restart ]]; then
  sudo systemctl restart meshcore-gateway
  sleep 10
  systemctl is-active meshcore-gateway
  journalctl -u meshcore-gateway --since "-1min" --no-pager | tail -15
fi
