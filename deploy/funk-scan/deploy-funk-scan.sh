#!/usr/bin/env bash
# Funk-Suchlauf in die Pi-Console (Port 8091) einbauen.
# Als marc auf dem Pi ausführen (nutzt sudo):
#   bash deploy-funk-scan.sh              -> Prüfen, Backup, Einbau, Neustart der Console, Kurztest
#   bash deploy-funk-scan.sh test         -> zusätzlich echte UKW-Messung (DAB-Monitor ca. 20 s aus)
#   bash deploy-funk-scan.sh rollback <backupdir>
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
GW=/home/marc/meshcore-gateway
LINK=$GW/local-console
UNIT=ideenwerft-local-dashboard.service
RULE=/etc/polkit-1/rules.d/49-ideenwerft-funk-scan.rules
RBB_ORIG=/opt/meshcore-ai-bot/source/deploy/dab/rbb_news.py
RBB_EXPECT=e0b605e9fd4a37a6ce582b43441f2af6e4b1e70aa9df0f5d20bf9afc4cf58f0b
RBB_DIR=/opt/ideenwerft-rbb-archive
RBB_DROPIN=/etc/systemd/system/ideenwerft-rbb-news.service.d/50-archiv.conf
EXPECT_DASH="0928b4d3b40d92f2bfc5160e6603f5757308318dd6d47939cb99120b290894e9 a434574c5e9f9a35a2358ab52a29bcb9814c1d511a61d7a852f9db0408b5ba70 f0b6bc89869553ada3d5f4eed27100fe542f65ba0948da7fb475746c5acfd2bf 3cd0e425c8e65d2edf199b98995f45bf6f781879c99ffbc072f379b0ec873168"

if [[ "${1:-}" == rollback ]]; then
  BK=${2:?Backup-Ordner angeben}
  sudo install -m 644 -o marc -g marc "$BK/local_dashboard.py" "$GW/local_dashboard.py"
  sudo ln -sfn "$(cat "$BK/console-link.txt")" "$LINK"
  sudo rm -f "$RULE" "$GW/radio_scan.py" "$RBB_DROPIN"
  sudo systemctl daemon-reload
  echo "rbb-Archiv-Aufnahmen bleiben unter /var/lib/meshcore-gateway/rbb-archiv erhalten."
  sudo systemctl restart "$UNIT"; sleep 3; systemctl is-active "$UNIT"
  echo "Rollback fertig."; exit 0
fi

# 1. Vorbedingungen
have=$(sha256sum "$GW/local_dashboard.py" | cut -d' ' -f1)
new=$(sha256sum "$HERE/local_dashboard.py" | cut -d' ' -f1)
if [[ " $EXPECT_DASH " != *" $have "* && "$have" != "$new" ]]; then
  echo "ABBRUCH: local_dashboard.py wurde seit 07.11. 16:17 geändert ($have). Nichts geändert." >&2; exit 1
fi
OLD=$(readlink -f "$LINK")
for f in index.html console.js console.css; do [[ -f "$OLD/$f" ]] || { echo "ABBRUCH: $OLD/$f fehlt" >&2; exit 1; }; done
[[ -x /usr/bin/rtl_power ]] || { echo "ABBRUCH: rtl_power fehlt" >&2; exit 1; }
if ! command -v rtl_433 >/dev/null; then
  echo "Installiere rtl_433 (Funksensoren) ..."
  sudo apt-get install -y rtl-433 >/dev/null 2>&1 || echo "HINWEIS: rtl_433 konnte nicht installiert werden (sudo apt install rtl-433). Sensor-Suche bleibt aus."
fi
command -v rtl_433 >/dev/null && echo "rtl_433: $(rtl_433 -V 2>&1 | head -1)"
if command -v ffmpeg >/dev/null; then echo "Ton-Encoder: ffmpeg"; elif command -v lame >/dev/null; then echo "Ton-Encoder: lame";
else echo "HINWEIS: weder ffmpeg noch lame installiert. Reinhören läuft dann als WAV (geht in Chrome/Firefox, Safari evtl. nicht). Besser: sudo apt install ffmpeg"; fi
systemctl show -p NoNewPrivileges -p PrivateDevices -p DevicePolicy -p SupplementaryGroups "$UNIT"
id marc | grep -q plugdev || echo "HINWEIS: marc ist nicht in der Gruppe plugdev; rtl_power könnte keinen Zugriff auf den Stick haben."

# DAB-Zustand vor dem Einspielen merken (wird am Ende wiederhergestellt)
DAB_UNITS="ideenwerft-dab-monitor.service ideenwerft-dab-news.service"
DAB_BEFORE=""
for u in $DAB_UNITS; do [[ $(systemctl is-active "$u" || true) == active ]] && DAB_BEFORE="$DAB_BEFORE $u"; done
echo "DAB vorher aktiv:${DAB_BEFORE:- keiner}"
# Nicht mitten in einen Suchlauf oder ins Reinhören hinein neu starten
if curl -fsS -m 3 http://127.0.0.1:8091/api/funk/status 2>/dev/null | python3 -c 'import json,sys;d=json.load(sys.stdin);sys.exit(0 if d["status"]["running"] or d.get("listen") else 1)' 2>/dev/null; then
  echo "ABBRUCH: Gerade läuft ein Funk-Suchlauf oder jemand hört rein. Bitte kurz warten." >&2; exit 1
fi

# 2. Backup
TS=$(date -u +%Y%m%dT%H%M%SZ)
BK=/var/backups/meshcore-pi-v04/before-funk-scan-$TS
sudo mkdir -p "$BK"
sudo cp -a "$GW/local_dashboard.py" "$BK/"
echo "$OLD" | sudo tee "$BK/console-link.txt" >/dev/null
sudo cp -a "$OLD" "$BK/console"
echo "BACKUP=$BK"

# 3. Einbau
NEWC=/opt/ideenwerft-local-console/funk-$TS
sudo mkdir -p "$NEWC"
sudo cp -a "$OLD/." "$NEWC/"
for f in index.html console.js console.css funk-scan.js rbb-archiv.js; do sudo install -m 644 -o root -g root "$HERE/console/$f" "$NEWC/$f"; done
sudo install -m 644 -o root -g root "$HERE/local_dashboard.py" "$HERE/radio_scan.py" "$NEWC/"
sudo install -m 644 -o marc -g marc "$HERE/radio_scan.py" "$GW/radio_scan.py"
sudo install -m 644 -o marc -g marc "$HERE/local_dashboard.py" "$GW/local_dashboard.py"
python3 -m py_compile "$GW/radio_scan.py" "$GW/local_dashboard.py"
sudo ln -sfn "$NEWC" "$LINK"

# Nur marc darf genau diese zwei DAB-Dienste anhalten/starten (für den Band-Suchlauf).
sudo tee "$RULE" >/dev/null <<'RULES'
polkit.addRule(function(action, subject) {
  if (action.id == "org.freedesktop.systemd1.manage-units" && subject.user == "marc") {
    var unit = action.lookup("unit"), verb = action.lookup("verb");
    if ((unit == "ideenwerft-dab-monitor.service" || unit == "ideenwerft-dab-news.service")
        && (verb == "start" || verb == "stop")) {
      return polkit.Result.YES;
    }
  }
});
RULES
sudo chmod 644 "$RULE"

# rbb-88.8-Nachrichten (ungerade Stunden) dauerhaft archivieren
RBB_HAVE=$(sha256sum "$RBB_ORIG" | cut -d' ' -f1)
if [[ "$RBB_HAVE" != "$RBB_EXPECT" ]]; then
  echo "HINWEIS: $RBB_ORIG hat sich geändert ($RBB_HAVE). rbb-Archiv wird NICHT eingerichtet."
else
  [[ -f "$RBB_DROPIN" ]] && sudo cp -a "$RBB_DROPIN" "$BK/" || true
  sudo mkdir -p "$RBB_DIR"
  sudo install -m 644 -o root -g root "$HERE/rbb_news.py" "$RBB_DIR/rbb_news.py"
  python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$RBB_DIR/rbb_news.py"
  sudo mkdir -p "$(dirname "$RBB_DROPIN")"
  printf '[Service]\nExecStart=\nExecStart=/opt/meshcore-ai-bot/current/.venv/bin/python %s/rbb_news.py\n' "$RBB_DIR" | sudo tee "$RBB_DROPIN" >/dev/null
  sudo systemctl daemon-reload
  systemctl show -p ExecStart ideenwerft-rbb-news.service | grep -q "$RBB_DIR/rbb_news.py" && echo "rbb-Archiv: aktiv ab der nächsten ungeraden Stunde"
  # Vorhandene Aufnahmen der letzten 24 Stunden übernehmen
  python3 - <<'PY'
import sys, re, pathlib
sys.path.insert(0, "/opt/ideenwerft-rbb-archive")
import rbb_news as r
n = 0
for p in sorted(pathlib.Path("/var/lib/meshcore-gateway/rbb-news").glob("rbb-*.mp3")):
    m = re.fullmatch(r"rbb-(\d+)\.mp3", p.name)
    if m:
        r.archive_audio(p, int(m.group(1))); n += 1
print(f"rbb-Archiv: {n} vorhandene Aufnahme(n) übernommen")
PY
fi

# 4. Neustart und Kurztest
sudo systemctl restart "$UNIT"; sleep 4
systemctl is-active "$UNIT"
PORT=$(systemctl show -p Environment "$UNIT" | grep -o 'DASHBOARD_PORT=[0-9]*' | cut -d= -f2); PORT=${PORT:-8091}
curl -fsS -o /tmp/funk-status.json "http://127.0.0.1:$PORT/api/funk/status" && echo "Status-API: ok"
curl -fsS -o /dev/null -w "funk-scan.js: %{http_code}\n" "http://127.0.0.1:$PORT/funk-scan.js"
curl -fsS "http://127.0.0.1:$PORT/api/rbb-archiv" | python3 -c 'import json,sys;d=json.load(sys.stdin);print("rbb-Archiv-API:", d["total"], "Sendungen")' || true
curl -fsS "http://127.0.0.1:$PORT/" | grep -q "Funk-Suchlauf" && echo "Menüpunkt Funk-Suchlauf: ok"

if [[ "${1:-}" == test ]]; then
  echo "Echte Messung UKW (DAB-Monitor ca. 20 s aus) ..."
  curl -sS -H 'Content-Type: application/json' -d '{"bands":["ukw"]}' "http://127.0.0.1:$PORT/api/funk/scan"; echo
  for i in $(seq 1 30); do
    sleep 3
    S=$(curl -fsS "http://127.0.0.1:$PORT/api/funk/status")
    echo "$S" | python3 -c 'import json,sys;d=json.load(sys.stdin);sys.exit(0 if d["status"]["running"] else 1)' || break
  done
  echo "$S" | python3 -c '
import json,sys;d=json.load(sys.stdin);s=d["status"];print("Fehler:",s["error"]);print("\n".join(s["log"]))
b=(d["results"].get("bands") or {}).get("bands",{}).get("ukw")
if b: print("UKW:", ", ".join("%.1f (%s dB)" % (x["mhz"], x["db"]) for x in b["signals"][:15]))'
  F=$(echo "$S" | python3 -c 'import json,sys;d=json.load(sys.stdin);b=((d["results"].get("bands") or {}).get("bands",{}).get("ukw") or {}).get("signals") or [];print(b[0]["mhz"] if b else "")')
  if [[ -n "$F" ]]; then
    echo "Reinhör-Test: 5 s auf $F MHz ..."
    sleep 3
    U=$(curl -sS -H 'Content-Type: application/json' -d "{\"mhz\":$F}" "http://127.0.0.1:$PORT/api/funk/listen" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("url",""))') || U=""
    rm -f /tmp/funk-hoertest.mp3
    [[ -n "$U" ]] && curl -s -m 5 -o /tmp/funk-hoertest.mp3 "http://127.0.0.1:$PORT$U" || true
    if [[ -s /tmp/funk-hoertest.mp3 ]]; then ls -l /tmp/funk-hoertest.mp3; file /tmp/funk-hoertest.mp3 || true; else echo "kein Ton empfangen"; fi
    sleep 4
  fi
  systemctl is-active ideenwerft-dab-monitor ideenwerft-dab-news
fi
sleep 2
for u in $DAB_BEFORE; do
  if [[ $(systemctl is-active "$u" || true) != active ]]; then echo "Starte $u wieder"; sudo systemctl start "$u"; fi
done
echo "DAB jetzt: $(systemctl is-active $DAB_UNITS | tr '\n' ' ')"
echo "Fertig. Seite: http://192.168.8.200:$PORT/#funk"
