#!/usr/bin/env bash
# Rundumtest auf dem Pi, vom Handy startbar. Läuft im Hintergrund weiter, auch wenn die SSH-Verbindung abreißt.
# Sendet nichts über Funk, ändert nichts an Bot, Konfig oder Datenbank (Testdatenbanken im Speicher).
#   bash start.sh               -> Rundumtest starten (ca. 40-50 Minuten)
#   bash start.sh faehigkeiten  -> nur den Fähigkeitentest beider Kanäle (ca. 8-10 Minuten)
#   bash start.sh status     -> läuft er noch?
# Ergebnis: /tmp/rundumtest/kurz.txt (Kurzfassung), /tmp/rundumtest/bericht.txt (alles)
set -euo pipefail
COMMIT=${COMMIT:-}
RAW=https://raw.githubusercontent.com/IdeenwerftFriedenau/meshcore-bot/$COMMIT
DIR=/tmp/rundumtest
PY=/opt/meshcore-ai-bot/current/.venv/bin/python
CONF=/etc/meshcore-ai-bot/config.toml

if [[ "${1:-}" == status ]]; then
  if pgrep -f "$DIR/lauf.sh" >/dev/null; then echo "Läuft noch. Letzte Zeile:"; tail -n 1 "$DIR/bericht.txt"
  else echo "Fertig."; [[ -f $DIR/kurz.txt ]] && echo "Ergebnis: cat $DIR/kurz.txt"; fi
  exit 0
fi
if pgrep -f "$DIR/lauf.sh" >/dev/null; then echo "Der Test läuft schon. Status: bash $0 status"; exit 0; fi
[[ -n "$COMMIT" ]] || { echo "COMMIT fehlt." >&2; exit 1; }

mkdir -p "$DIR"
for f in rundum.py massive_test.py kurz.py faehigkeiten.py; do curl -fsSL "$RAW/deploy/rundumtest/$f" -o "$DIR/$f"; done
curl -fsSL "$RAW/scripts/boerse-test.py" -o "$DIR/boerse-test.py"
curl -fsSL "$RAW/deploy/kennzahlen/live_test.py" -o "$DIR/kennzahlen_test.py"
rm -f "$DIR/kurz.txt"
sudo -v  # Passwort jetzt abfragen, nicht mitten im Hintergrundlauf

if [[ "${1:-}" == faehigkeiten ]]; then
cat > "$DIR/lauf.sh" <<RUN
#!/usr/bin/env bash
cd "$DIR"
{
  echo "Fähigkeitentest \$(date -Is) auf \$(hostname), Quelle \$(readlink -f /opt/meshcore-ai-bot/source)"
  echo "################ FÄHIGKEITEN #fragdenbot und #börsenbot ################"
  sudo "$PY" -I faehigkeiten.py "$CONF" 3 || echo "FEHLER faehigkeiten"
  echo; echo "FERTIG \$(date -Is)"
} > "$DIR/bericht.txt" 2>&1
python3 "$DIR/kurz.py" "$DIR/bericht.txt" 40 > "$DIR/kurz.txt"
RUN
else
cat > "$DIR/lauf.sh" <<RUN
#!/usr/bin/env bash
cd "$DIR"
{
  echo "Rundumtest \$(date -Is) auf \$(hostname), Quelle \$(readlink -f /opt/meshcore-ai-bot/source)"
  systemctl is-active meshcore-gateway || true
  echo; echo "################ A. RUNDUM: echte Fragen neu, Börsenbefehle, Alarm, Bericht ################"
  sudo "$PY" -I rundum.py "$CONF" 120 || echo "FEHLER rundum"
  echo; echo "################ B. BÖRSEN-HÄRTETEST (echte KI) ################"
  sudo PAUSE=2.5 "$PY" -I boerse-test.py "$CONF" || echo "FEHLER boerse-test"
  echo; echo "################ C. ALLGEMEINER HÄRTETEST (echte KI) ################"
  sudo "$PY" -I massive_test.py "$CONF" 4 || echo "FEHLER massive_test"
  echo; echo "################ D. KENNZAHLEN (SEC, Wikidata) ################"
  sudo "$PY" -I kennzahlen_test.py || echo "FEHLER kennzahlen"
  echo; echo "FERTIG \$(date -Is)"
} > "$DIR/bericht.txt" 2>&1
python3 "$DIR/kurz.py" "$DIR/bericht.txt" 40 > "$DIR/kurz.txt"
RUN
fi
chmod +x "$DIR/lauf.sh"
nohup setsid "$DIR/lauf.sh" >/dev/null 2>&1 &
echo "Test gestartet (läuft auch ohne offene Verbindung weiter)."
echo "Stand prüfen:  bash $0 status"
echo "Ergebnis:      cat $DIR/kurz.txt"
