#!/usr/bin/env bash
# Rollt einen GitHub-Stand vollständig auf den Pi im Gateway-Betrieb aus. Als marc ausführen, nutzt sudo selbst.
#
#   bash deploy.sh pruefen [commit]   -> nur prüfen und berichten, ändert nichts
#   bash deploy.sh <commit|main>      -> Backup, Bot-Paket, Gateway-Dateien, Quellenprüfung (ohne Neustart)
#   bash deploy.sh <commit|main> restart -> zusätzlich meshcore-gateway neu starten (nötig, damit es wirkt)
#   bash deploy.sh rollback [backupordner] -> letzten (oder genannten) Stand wiederherstellen und neu starten
#
# Ablauf: Der Stand kommt per git von GitHub (jede Datei ist damit durch die Commit-ID geprüft). Vor dem
# Austausch wird jede Datei auf dem Pi mit der GitHub-Historie verglichen. Eine Datei, die es so nie auf
# GitHub gab, ist eine Änderung von Hand: Dann bricht das Skript ab, statt sie zu überschreiben.
# Schlägt der Start nach dem Austausch fehl, stellt das Skript den vorherigen Stand selbst wieder her.
#
# Schalter (als Umgebungsvariable davor setzen):
#   FORCE=1   von Hand geänderte Dateien trotzdem ersetzen (sie liegen dann im Backup) und fehlende
#             GitHub-Prüfungen übergehen
#   SKIP_CI=1 nur die GitHub-Prüfungen übergehen (Notfallkorrektur)
#   DEPS=1    geänderte Python-Abhängigkeiten mit pip nachinstallieren
#   DIFF=1    bei pruefen die Unterschiede der von Hand geänderten Dateien ausgeben
#   KEEP=10   so viele Deploy-Backups behalten (0 = nie aufräumen)
set -euo pipefail

REPO=${REPO:-IdeenwerftFriedenau/meshcore-bot}
REMOTE=${REMOTE:-https://github.com/$REPO.git}
BASE=${BASE:-/opt/meshcore-ai-bot}
GATEWAY=${GATEWAY:-/home/marc/meshcore-gateway}
BACKUPS=${BACKUPS:-/var/backups/meshcore-pi-v04}
CONF=${CONF:-/etc/meshcore-ai-bot/config.toml}
SERVICE=${SERVICE:-meshcore-gateway}
CACHE=${CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/meshcore-bot.git}
KEEP=${KEEP:-10}
PY=$BASE/current/.venv/bin/python
SUDO=${SUDO-sudo}

say() { printf '%s\n' "$*"; }
die() { printf 'ABBRUCH: %s\n' "$*" >&2; exit 1; }

package_dir() { "$PY" -I -c 'import meshcore_ai_bot,os;print(os.path.dirname(meshcore_ai_bot.__file__))'; }

# Dateien neben dem Collector, die aus dem Repository stammen: Zielname -> Pfad im Repository.
gateway_map() {
  local src=$1 f
  for f in "$src"/deploy/existing-gateway/*.py; do printf '%s\t%s\n' "$(basename "$f")" "$f"; done
  printf '%s\t%s\n' pi_gateway_bridge.py "$src/deploy/pi_gateway_bridge.py"
  for f in "$src"/deploy/local-console/*.py "$src"/src/meshcore_ai_bot/dab_control.py \
           "$src"/src/meshcore_ai_bot/dab_scan.py; do
    # Diese Kopien gibt es nur, wenn sie früher einmal neben den Collector gelegt wurden.
    if [[ -f "$GATEWAY/$(basename "$f")" ]]; then printf '%s\t%s\n' "$(basename "$f")" "$f"; fi
  done
  return 0
}

known_blob() {  # Gab es genau diesen Dateiinhalt irgendwann auf GitHub?
  local blob
  # Manche Dateien auf dem Pi darf nur root lesen.
  blob=$($SUDO cat -- "$1" | git --git-dir="$CACHE" hash-object --stdin) || return 1
  git --git-dir="$CACHE" cat-file -e "$blob" 2>/dev/null
}

restart_and_verify() {
  $SUDO systemctl restart "$SERVICE"
  sleep 10
  systemctl is-active --quiet "$SERVICE"
}

restore() {
  local bk=$1 sp
  [[ -d "$bk/meshcore_ai_bot" && -f "$bk/source-link.txt" ]] || die "$bk ist kein Deploy-Backup."
  sp=$(cat "$bk/package-dir.txt" 2>/dev/null || package_dir)
  $SUDO rm -rf "$sp.rollback"
  $SUDO cp -a "$bk/meshcore_ai_bot" "$sp.rollback"
  $SUDO rm -rf "$sp"
  $SUDO mv "$sp.rollback" "$sp"
  $SUDO ln -sfn "$(cat "$bk/source-link.txt")" "$BASE/source"
  if [[ -d "$bk/gateway" ]]; then
    local f
    for f in "$bk"/gateway/*.py; do
      if [[ -e "$f" ]]; then $SUDO cp -a "$f" "$GATEWAY/$(basename "$f")"; fi
    done
  fi
  return 0
}

prune_backups() {
  [[ "$KEEP" =~ ^[0-9]+$ && "$KEEP" -gt 0 ]] || return 0
  local old
  # Nur Ordner, die dieses und die früheren Deploy-Skripte angelegt haben; die neuesten bleiben.
  while IFS= read -r old; do
    [[ -n "$old" && "$old" == "$BACKUPS"/before-* ]] || continue
    $SUDO rm -rf -- "$old"
    say "Altes Backup entfernt: $old"
  done < <(find "$BACKUPS" -mindepth 1 -maxdepth 1 -type d -name 'before-*' -printf '%T@ %p\n' 2>/dev/null \
           | sort -rn | tail -n +"$((KEEP + 1))" | cut -d' ' -f2-)
}

MODE=${1:-}
[[ -n "$MODE" ]] || { sed -n '2,8p' "$0"; exit 1; }
[[ -x "$PY" ]] || die "$PY fehlt. Das Bot-Paket ist hier nicht installiert."

if [[ "$MODE" == rollback ]]; then
  BK=${2:-$(find "$BACKUPS" -mindepth 1 -maxdepth 1 -type d -name 'before-*' -printf '%T@ %p\n' | sort -rn | head -1 | cut -d' ' -f2-)}
  [[ -n "$BK" ]] || die "Kein Backup unter $BACKUPS gefunden."
  say "Stelle wieder her: $BK"
  restore "$BK"
  restart_and_verify && say "Rollback fertig, $SERVICE läuft." || die "$SERVICE läuft nach dem Rollback nicht. journalctl -u $SERVICE"
  exit 0
fi

CHECK_ONLY=0
if [[ "$MODE" == pruefen ]]; then CHECK_ONLY=1; WANT=${2:-main}; else WANT=$MODE; fi
RESTART=0
[[ "${2:-}" == restart ]] && RESTART=1
command -v git >/dev/null || die "git fehlt. Einmalig: sudo apt-get install -y git"

# 1. Stand von GitHub holen. git prüft dabei jeden Inhalt gegen die Commit-ID.
[[ -d "$CACHE" ]] || git init -q --bare "$CACHE"
git --git-dir="$CACHE" fetch -q --prune "$REMOTE" '+refs/heads/*:refs/remotes/origin/*' \
  || die "GitHub ist nicht erreichbar."
if [[ "$WANT" =~ ^[0-9a-f]{7,40}$ ]]; then REF=$WANT; else REF=refs/remotes/origin/$WANT; fi
COMMIT=$(git --git-dir="$CACHE" rev-parse --verify -q "$REF^{commit}") || die "Stand '$WANT' gibt es auf GitHub nicht."
say "Stand: $COMMIT ($(git --git-dir="$CACHE" log -1 --format=%s "$COMMIT"))"

# 2. GitHub-Prüfungen müssen bestanden sein (Regel aus AGENTS.md).
CI=$(curl -fsS -H 'Accept: application/vnd.github+json' "https://api.github.com/repos/$REPO/commits/$COMMIT/check-runs" 2>/dev/null \
  | "$PY" -I -c '
import json, sys
try:
    runs = json.load(sys.stdin).get("check_runs", [])
except ValueError:
    runs = None
if runs is None: print("unbekannt")
elif not runs: print("keine")
elif any(r.get("status") != "completed" for r in runs): print("laufen noch")
elif all(r.get("conclusion") in ("success", "skipped", "neutral") for r in runs): print("bestanden")
else: print("fehlgeschlagen")' || true)
CI=${CI:-unbekannt}
say "GitHub-Prüfungen: $CI"
if [[ "$CI" != bestanden && "${FORCE:-}" != 1 && "${SKIP_CI:-}" != 1 && $CHECK_ONLY == 0 ]]; then
  die "Die GitHub-Prüfungen für diesen Stand sind nicht bestanden ($CI). Mit FORCE=1 übergehen."
fi

WORK=$(mktemp -d)
trap '$SUDO rm -rf "$WORK"' EXIT
NEW=$WORK/src
mkdir "$NEW"
git --git-dir="$CACHE" archive "$COMMIT" | tar -x -C "$NEW"

SP=$(package_dir)
OLD=$(readlink -f "$BASE/source" 2>/dev/null || true)
say "Bot-Paket: $SP"
say "Bisheriger Stand: ${OLD:-unbekannt} ($(cat "$OLD/source-commit.txt" 2>/dev/null || echo 'ohne Commit-Angabe'))"

# 3. Von Hand geänderte Dateien finden: Inhalte, die es auf GitHub nie gab.
DRIFT=()
while IFS= read -r -d '' f; do
  known_blob "$f" || DRIFT+=("$f")
done < <(find "$SP" -type f ! -path '*/__pycache__/*' ! -name '*.pyc' -print0)
# Im bisherigen Quellstand zählt nur, was der neue Stand ersetzt. Fremde Dateien dort (etwa alte
# Patch-Dateien) bleiben im alten Ordner liegen und stören nicht.
EXTRA=0
if [[ -n "$OLD" && -d "$OLD" ]]; then
  while IFS= read -r -d '' f; do
    rel=${f#"$OLD"/}
    if [[ ! -e "$NEW/$rel" ]]; then
      known_blob "$f" || EXTRA=$((EXTRA + 1))
      continue
    fi
    $SUDO cmp -s "$f" "$NEW/$rel" && continue
    known_blob "$f" || DRIFT+=("$f")
  done < <($SUDO find "$OLD" -type f ! -path '*/__pycache__/*' ! -name '*.pyc' ! -name source-commit.txt \
             ! -path '*/.venv/*' ! -path '*/.git/*' ! -path '*.egg-info/*' ! -path '*/.pytest_cache/*' \
             ! -path '*/.ruff_cache/*' -print0)
fi
KEPT=()
while IFS=$'\t' read -r name from; do
  [[ -f "$GATEWAY/$name" ]] || continue
  cmp -s "$GATEWAY/$name" "$from" && continue
  known_blob "$GATEWAY/$name" || KEPT+=("$name")
done < <(gateway_map "$NEW")

if ((${#DRIFT[@]})); then
  say "Von Hand geändert (so nie auf GitHub gewesen), Zeilen anders als im neuen Stand:"
  for f in "${DRIFT[@]}"; do
    rel=${f#"$OLD"/}; [[ "$f" == "$SP"/* ]] && rel=src/meshcore_ai_bot/${f#"$SP"/}
    lines=$($SUDO diff -- "$f" "$NEW/$rel" 2>/dev/null | grep -c '^[<>]' || true)
    printf '  %s (%s)\n' "$f" "${lines:-?}"
  done
  if [[ "${DIFF:-}" == 1 ]]; then
    for f in "${DRIFT[@]}"; do
      rel=${f#"$OLD"/}; [[ "$f" == "$SP"/* ]] && rel=src/meshcore_ai_bot/${f#"$SP"/}
      $SUDO diff -u --label "Pi: $f" --label "GitHub: $rel" -- "$f" "$NEW/$rel" || true
    done
  fi
fi
if ((EXTRA)); then say "Im bisherigen Quellstand liegen $EXTRA fremde Dateien (etwa alte Patches). Sie bleiben dort."; fi
if ((${#KEPT[@]})); then
  say "Im Gateway-Ordner von Hand geändert, bleiben unverändert liegen:"
  printf '  %s\n' "${KEPT[@]/#/$GATEWAY/}"
fi

# 4. Abhängigkeiten vergleichen.
deps_of() {
  [[ -f "$1" ]] || return 0
  "$PY" -I -c '
import sys, tomllib
project = tomllib.load(open(sys.argv[1], "rb"))["project"]
print("\n".join(sorted(project.get("dependencies", []) + project.get("optional-dependencies", {}).get("radio", []))))
' "$1" 2>/dev/null || true
}
DEPS_NEW=$(deps_of "$NEW/pyproject.toml")
DEPS_OLD=$(deps_of "$OLD/pyproject.toml")
DEPS_CHANGED=0
if [[ -n "$DEPS_OLD" && "$DEPS_OLD" != "$DEPS_NEW" ]]; then
  DEPS_CHANGED=1
  say "Python-Abhängigkeiten haben sich geändert:"
  diff <(printf '%s\n' "$DEPS_OLD") <(printf '%s\n' "$DEPS_NEW") | sed 's/^/  /' || true
fi

CHANGED=$(diff -rq -x __pycache__ "$SP" "$NEW/src/meshcore_ai_bot" 2>/dev/null | wc -l || true)
say "Unterschiede im Bot-Paket zum neuen Stand: $CHANGED Dateien."

if [[ $CHECK_ONLY == 1 ]]; then
  say "Nur geprüft, nichts geändert."
  exit 0
fi
if ((${#DRIFT[@]})) && [[ "${FORCE:-}" != 1 ]]; then
  die "Erst die oben genannten Änderungen nach GitHub übernehmen. Mit FORCE=1 werden sie ersetzt (Kopie im Backup)."
fi
if [[ $DEPS_CHANGED == 1 && "${DEPS:-}" != 1 ]]; then
  die "Mit DEPS=1 erneut starten, dann installiert pip die geänderten Abhängigkeiten."
fi

# 5. Backup.
TS=$(date -u +%Y%m%dT%H%M%SZ)
BK=$BACKUPS/before-deploy-$TS
$SUDO mkdir -p "$BK/gateway"
$SUDO cp -a "$SP" "$BK/meshcore_ai_bot"
[[ -f "$CONF" ]] && $SUDO cp -a "$CONF" "$BK/config.toml"
printf '%s\n' "$OLD" | $SUDO tee "$BK/source-link.txt" >/dev/null
printf '%s\n' "$SP" | $SUDO tee "$BK/package-dir.txt" >/dev/null
while IFS=$'\t' read -r name from; do
  if [[ -f "$GATEWAY/$name" ]]; then $SUDO cp -a "$GATEWAY/$name" "$BK/gateway/$name"; fi
done < <(gateway_map "$NEW")
say "BACKUP=$BK"
say "Rückweg falls nötig: bash $0 rollback $BK"

undo() {
  say "Stelle den vorherigen Stand wieder her …" >&2
  restore "$BK"
  if [[ $RESTART == 1 ]]; then
    restart_and_verify || say "WARNUNG: $SERVICE läuft auch nach der Wiederherstellung nicht." >&2
  fi
  die "$1 Der vorherige Stand ist wiederhergestellt."
}

# 6. Abhängigkeiten, dann das Bot-Paket als Ganzes austauschen.
if [[ $DEPS_CHANGED == 1 ]]; then
  mapfile -t LIST <<<"$DEPS_NEW"
  $SUDO "$PY" -m pip install --quiet "${LIST[@]}" || undo "pip konnte die Abhängigkeiten nicht installieren."
fi
OWNER=$(stat -c %U:%G "$SP")
$SUDO rm -rf "$SP.new"
$SUDO cp -r "$NEW/src/meshcore_ai_bot" "$SP.new"
$SUDO chown -R "$OWNER" "$SP.new"
$SUDO find "$SP.new" -type d -exec chmod 755 {} +
$SUDO find "$SP.new" -type f -exec chmod 644 {} +
$SUDO "$PY" -I -m compileall -q "$SP.new" >/dev/null || { $SUDO rm -rf "$SP.new"; die "Der neue Stand lässt sich nicht übersetzen. Nichts geändert."; }
$SUDO rm -rf "$SP.old"
$SUDO mv "$SP" "$SP.old"
$SUDO mv "$SP.new" "$SP"
$SUDO rm -rf "$SP.old"

# 7. Quellstand daneben ablegen (Dienste starten Skripte aus $BASE/source/deploy).
SNAP=$BASE/sources/${COMMIT:0:7}
if [[ ! -d "$SNAP" ]]; then
  $SUDO mkdir -p "$BASE/sources"
  $SUDO cp -r "$NEW" "$SNAP"
  printf '%s\n' "$COMMIT" | $SUDO tee "$SNAP/source-commit.txt" >/dev/null
fi
$SUDO ln -sfn "$SNAP" "$BASE/source"
say "SOURCE=$SNAP"

# 8. Gateway-Dateien neben dem Collector: nur unveränderte GitHub-Fassungen werden ersetzt.
while IFS=$'\t' read -r name from; do
  target=$GATEWAY/$name
  if [[ -f "$target" ]]; then
    cmp -s "$target" "$from" && continue
    [[ " ${KEPT[*]:-} " == *" $name "* ]] && continue
    $SUDO install -m "$(stat -c %a "$target")" -o "$(stat -c %U "$target")" -g "$(stat -c %G "$target")" "$from" "$target"
  else
    $SUDO install -m 644 -o "$(stat -c %U "$GATEWAY")" -g "$(stat -c %G "$GATEWAY")" "$from" "$target"
  fi
  say "Gateway-Datei aktualisiert: $name"
done < <(gateway_map "$NEW")

# 9. Tägliche Quellenprüfung einrichten oder aktualisieren.
UNITS_CHANGED=0
for unit in meshcore-quellen.service meshcore-quellen.timer; do
  if ! cmp -s "$NEW/deploy/quellen/$unit" "/etc/systemd/system/$unit" 2>/dev/null; then
    $SUDO install -m 644 "$NEW/deploy/quellen/$unit" "/etc/systemd/system/$unit"
    UNITS_CHANGED=1
  fi
done
if [[ $UNITS_CHANGED == 1 ]]; then
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now meshcore-quellen.timer >/dev/null 2>&1 || say "WARNUNG: Timer meshcore-quellen ließ sich nicht einschalten."
  say "Tägliche Quellenprüfung eingerichtet (meshcore-quellen.timer)."
fi

# 10. Lädt der neue Stand? Antworten die Quellen?
$SUDO "$PY" -I -c 'import meshcore_ai_bot.assistant, meshcore_ai_bot.engine, meshcore_ai_bot.tools' \
  || undo "Der neue Stand lässt sich nicht laden."
set +e
$SUDO env STATE_DIRECTORY="$WORK/quellen" "$PY" -I "$SNAP/deploy/quellen/pruefen.py" "$CONF" 2>&1 | grep -v 'HTTP Request'
LIVE=${PIPESTATUS[0]}
set -e
if [[ $LIVE == 2 ]]; then undo "Die Quellenprüfung ist mit dem neuen Stand abgestürzt."; fi
if [[ $LIVE == 1 ]]; then
  say "Hinweis: Einzelne Quellen sind gestört (siehe oben). Das liegt meist an der Quelle, nicht am neuen Stand."
fi

# 11. Neustart und Kontrolle.
if [[ $RESTART == 1 ]]; then
  restart_and_verify || undo "$SERVICE ist nach dem Neustart nicht aktiv."
  journalctl -u "$SERVICE" --since "-1min" --no-pager 2>/dev/null | tail -8 || true
  say "$SERVICE läuft mit $COMMIT."
else
  say "Noch nicht aktiv: Der laufende Dienst nutzt den alten Stand bis zum Neustart (bash $0 $WANT restart)."
fi

prune_backups
say "Fertig."
