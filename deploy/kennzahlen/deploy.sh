#!/usr/bin/env bash
# Rollt die Firmenkennzahlen im Börsenbot aus (PR #41). Als marc ausführen, nutzt sudo selbst.
# Holt den Code vom festen Commit 4f8a55548faa0580fc3c3e06662118c6d28cebae auf GitHub und prüft jede Datei per SHA-256.
#   bash deploy.sh            -> Backup, Dateien, Live-Test (ohne Neustart)
#   bash deploy.sh restart    -> zusätzlich meshcore-gateway neu starten (nötig, damit es wirkt)
#   bash deploy.sh rollback <backupdir>
set -euo pipefail
COMMIT=4f8a55548faa0580fc3c3e06662118c6d28cebae
RAW=https://raw.githubusercontent.com/IdeenwerftFriedenau/meshcore-bot
BASE=/opt/meshcore-ai-bot
PY=$BASE/current/.venv/bin/python
CONF=/etc/meshcore-ai-bot/config.toml
SP=$($PY -c 'import meshcore_ai_bot,os;print(os.path.dirname(meshcore_ai_bot.__file__))')
FILES=(assistant.py tools.py markets.py fundamentals.py)

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
  [assistant.py]="4f7a08035c18e4c3f0208ab72887975c59d7ad066dc9e5aabbee2cb281be8fb6"
  [tools.py]="42efd85c0e7aabe966e01ae4ced5e8341c73b9247f6895d8b1aee50f589e5c8b"
  [markets.py]="7a1fece2877ea239189e818bcd3c49f1d2cb5113ac4650e32e0b5f58ca5fbbec"
  [fundamentals.py]="1f5d8ee04e16ff51239392239184ef59948c2cae7f559ad7697e99c5ab7ece98"
  [test_fundamentals.py]="f904420cd862ce799ea5253bfa5c6a311d828bd3720c660c01e316050cd680e6"
)
# Erwarteter Vorstand auf dem Pi: main 0ffa030 (Gedächtnis #40) oder eine frühere Kennzahlen-Auslieferung (a5f7765, d7c3b98).
declare -A EXPECT=(
  [assistant.py]="c843b7747da94bec563b9cc7431bb3e90fce3a34726ba1e946dbed56b0ec6d8b 4f7a08035c18e4c3f0208ab72887975c59d7ad066dc9e5aabbee2cb281be8fb6"
  [tools.py]="fa58e377124917992a98b2cc95a7872425a412f474ab690fc90162c490cd397f 42efd85c0e7aabe966e01ae4ced5e8341c73b9247f6895d8b1aee50f589e5c8b"
  [markets.py]="1e7911016b3026aa79083233c29263040f0e1fa643b3941e6c77dca4f6f6f990 7a1fece2877ea239189e818bcd3c49f1d2cb5113ac4650e32e0b5f58ca5fbbec"
  [fundamentals.py]="neu 06b5a69ae53d3d8c6d4bdba594556ee7372df95d8da19e70d126a29dc571f70a 7b252114a064c35c3590da3f66c1f87eb160bfa7fbc535c7c0371af529849bb3"
)

PKG=$(mktemp -d)
trap 'rm -rf "$PKG"' EXIT
for f in "${FILES[@]}" test_fundamentals.py; do
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
  BK=/var/backups/meshcore-pi-v04/before-kennzahlen-$TS
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
  NEW="$BASE/sources/${COMMIT:0:7}-kennzahlen"
  [[ -d "$NEW" ]] || sudo cp -a "$OLD" "$NEW"
  for f in "${FILES[@]}"; do sudo cp "$PKG/$f" "$NEW/src/meshcore_ai_bot/$f"; done
  sudo cp "$PKG/test_fundamentals.py" "$NEW/tests/"
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
