#!/usr/bin/env bash
# DAB-Empfang absichern: welle-cli-Wächter (dab_monitor.py) und robuste rbb-Aufnahme einspielen.
# Als marc auf dem Pi ausführen (nutzt sudo selbst):
#   curl -fsSL https://raw.githubusercontent.com/IdeenwerftFriedenau/meshcore-bot/main/deploy/dab/update-empfang.sh -o /tmp/empfang.sh
#   bash /tmp/empfang.sh main            -> prüfen, Backup, einspielen, DAB-Monitor neu starten
#   bash /tmp/empfang.sh rollback <ordner>
# Hintergrund: welle-cli schließt beendete HTTP-Verbindungen nicht (CLOSE_WAIT). Bei 1024 offenen
# Dateien nimmt es nichts mehr an; rbb-Aufnahmen enden dann mit „kein Ton · ConnectTimeout“.
set -euo pipefail
REPO=IdeenwerftFriedenau/meshcore-bot
MON_UNIT=ideenwerft-dab-monitor.service
RBB=/opt/ideenwerft-rbb-archive/rbb_news.py
LINK=/home/marc/meshcore-gateway/local-console
# Alle bisher auf GitHub veröffentlichten Fassungen; alles andere gilt als Handänderung.
MON_KNOWN="ef4937b9da85ab4949bf1503672e6465f3f6296fb05c21b9599bbf6fffd2e9a5 e55e55360d0e564099fb0b0f8c19729ca3c154b55292a6f443816fb6df15a9df"
RBB_KNOWN="d9b145cace287d957fbc333a6059fbe4b1e7805c0dd8a9ddddf4460bd5ee3747"
JS_KNOWN="8309d77872f789f97e64176a94719dd27727efb9591caac6d5d5837c1e0ad14d"

monitor_path() {
  systemctl show -p ExecStart "$MON_UNIT" | grep -o '[^ ;=]*dab_monitor\.py' | head -1
}
in_news_window() {  # HH:59:30 bis HH:05:50: dann nicht am Empfänger rütteln
  local s=$(( $(date +%s) % 3600 )); (( s >= 3570 || s < 350 ))
}
restart_monitor() {
  while in_news_window; do echo "Nachrichtenaufnahme läuft, warte bis Minute 06 …"; sleep 20; done
  sudo systemctl restart "$MON_UNIT"; sleep 5
  systemctl is-active "$MON_UNIT"
}

if [[ "${1:-}" == rollback ]]; then
  BK=${2:?Backup-Ordner angeben}
  MON=$(monitor_path)
  sudo test -f "$BK/dab_monitor.py" && sudo install -m 644 -o marc -g marc "$BK/dab_monitor.py" "$MON"
  sudo test -f "$BK/rbb_news.py" && sudo install -m 644 -o root -g root "$BK/rbb_news.py" "$RBB"
  sudo test -f "$BK/rbb-archiv.js" && sudo install -m 644 -o root -g root "$BK/rbb-archiv.js" "$(readlink -f "$LINK")/rbb-archiv.js"
  restart_monitor; echo "Rollback fertig."; exit 0
fi

C=${1:?Commit oder main angeben}
[[ "$C" == main || "$C" =~ ^[0-9a-f]{7,40}$ ]] || { echo "Ungültiger Stand: $C" >&2; exit 1; }
D=$(mktemp -d)
for f in deploy/dab/dab_monitor.py deploy/funk-scan/rbb_news.py deploy/funk-scan/console/rbb-archiv.js; do
  curl -fsSL "https://raw.githubusercontent.com/$REPO/$C/$f" -o "$D/$(basename "$f")"
done
python3 -m py_compile "$D/dab_monitor.py" "$D/rbb_news.py"

MON=$(monitor_path)
[[ -n "$MON" && -f "$MON" ]] || { echo "ABBRUCH: dab_monitor.py nicht gefunden (ExecStart von $MON_UNIT)." >&2; exit 1; }
grep -q "def news_window" "$(dirname "$MON")/dab_control.py" \
  || { echo "ABBRUCH: $(dirname "$MON")/dab_control.py ist zu alt (news_window fehlt)." >&2; exit 1; }
JS="$(readlink -f "$LINK")/rbb-archiv.js"

check() {  # Datei, bekannte Hashes, neue Datei
  local have; have=$(sudo sha256sum "$1" | cut -d' ' -f1)
  if [[ "$have" == $(sha256sum "$3" | cut -d' ' -f1) ]]; then echo "schon aktuell: $1"; return 0; fi
  if [[ " $2 " != *" $have "* && "${FORCE:-0}" != 1 ]]; then
    echo "ABBRUCH: $1 wurde von Hand geändert ($have). Mit FORCE=1 trotzdem ersetzen (liegt dann im Backup)." >&2
    exit 1
  fi
}
check "$MON" "$MON_KNOWN" "$D/dab_monitor.py"
[[ -f "$RBB" ]] && check "$RBB" "$RBB_KNOWN" "$D/rbb_news.py"
[[ -f "$JS" ]] && check "$JS" "$JS_KNOWN" "$D/rbb-archiv.js"

BK=/var/backups/meshcore-pi-v04/before-empfang-$(date -u +%Y%m%dT%H%M%SZ)
sudo mkdir -p "$BK"
sudo cp -a "$MON" "$BK/"
[[ -f "$RBB" ]] && sudo cp -a "$RBB" "$BK/"
[[ -f "$JS" ]] && sudo cp -a "$JS" "$BK/"
echo "BACKUP=$BK"

sudo install -m 644 -o "$(stat -c %U "$MON")" -g "$(stat -c %G "$MON")" "$D/dab_monitor.py" "$MON"
if [[ -f "$RBB" ]]; then sudo install -m 644 -o root -g root "$D/rbb_news.py" "$RBB"; else
  echo "HINWEIS: $RBB fehlt (rbb-Archiv nicht eingerichtet); nur der Empfangswächter wird eingespielt."; fi
[[ -f "$JS" ]] && sudo install -m 644 -o root -g root "$D/rbb-archiv.js" "$JS"
restart_monitor
sleep 10
journalctl -u "$MON_UNIT" --since -1min --no-pager -o cat | tail -5
FDS=$(sudo ls "/proc/$(pgrep -x welle-cli | head -1)/fd" 2>/dev/null | wc -l || true)
echo "welle-cli offene Dateien: ${FDS:-?} (Neustart erfolgt vorsorglich ab 800)"
echo "Fertig. Nächste rbb-Aufnahme zur nächsten ungeraden vollen Stunde."
