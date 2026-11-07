# Jarvis: das Motorola anrufen und mit der KI sprechen

Jarvis nimmt Anrufe auf dem Motorola an, aber **nur von freigegebenen Nummern**.
Der Pi hängt sich als Bluetooth-Freisprecheinrichtung (HFP) in das Gespräch,
erkennt die Sprache lokal mit Vosk, fragt die konfigurierte KI und liest die
Antwort mit Piper (Stimme „Thorsten“, Deutsch, CC0) vor.

Ablauf eines Anrufs:

1. Der Dienst fragt jede Sekunde per ADB den Anrufstatus ab
   (`dumpsys telephony.registry`, wie das Dashboard).
2. Klingelt eine freigegebene Nummer, holt Jarvis die gemeinsame Audio-Sperre,
   verbindet das Freisprechprofil und nimmt per `KEYCODE_CALL` ab.
   Unterdrückte oder andere Nummern klingeln einfach weiter.
3. Begrüßung, dann abwechselnd zuhören und antworten. Während Jarvis spricht,
   wird das Mikrofon ignoriert (kein Echo, kein Dazwischenreden).
4. Ende bei „Tschüss“, „leg auf“ oder „Ende“, nach 60 Sekunden Stille, nach
   15 Minuten oder wenn der Anrufer auflegt. Danach Freisprechprofil trennen.

## Datenschutz und Grenzen

- Keine Aufnahmen, keine Abschriften auf der Festplatte. Das Protokoll enthält
  weder Nummern noch Fragen oder Antworten, nur Längen und Ergebnisse.
- Die erkannte Frage geht an die konfigurierte KI (eigener Gateway), sonst nirgends.
- Telefonton ist schmalbandig; die kleine Vosk-Variante verhört sich häufiger
  als bei DAB. Jarvis darf nachfragen. Pro Antwort sind 2 bis 4 Sekunden
  Wartezeit normal.
- Browser-Gesprächston im Dashboard und Jarvis teilen sich eine Sperre
  (`/var/lib/meshcore-gateway/phone-remote/audio.lock`). Wer zuerst kommt,
  hat den Ton; der andere lehnt ab. Ohne Sperrdatei verhält sich der
  Browser-Ton wie bisher.
- Das Handy braucht keine Bildschirmsperre-PIN für das Annehmen; ADB-Freigabe und
  Bluetooth-Kopplung aus [Handy-Audio](android-phone.md) müssen bestehen.

## Einrichtung auf dem Pi

Voraussetzungen: veröffentlichter Stand mit grünen GitHub-Prüfungen unter
`/opt/meshcore-ai-bot/source`, Motorola per Bluetooth gekoppelt (Gesprächston im
Browser funktioniert), Vosk unter `/home/marc/dab-warnings-meshcore`.
Vorher wie üblich sichern.

```sh
sudo /opt/meshcore-ai-bot/source/scripts/setup-jarvis.sh
```

Das Skript lädt Piper und die Stimme mit festen Adressen und prüft Größe und
Prüfsumme ([jarvis-artifacts.json](../deploy/jarvis-artifacts.json)), legt die
Sperrdatei und `/etc/meshcore-ai-bot/jarvis.env` (0600) an und installiert die
Unit, ohne sie zu starten. In `jarvis.env` privat eintragen:

```dotenv
JARVIS_ALLOWED_CALLERS=PRIVATE_NUMBER_1,PRIVATE_NUMBER_2
JARVIS_AI_BASE_URL=https://PRIVATE_GATEWAY/v1
JARVIS_AI_MODEL=gemini-2.5-flash
JARVIS_AI_API_KEY=PRIVATE_KEY
# Leer, wenn der Gateway Anfragen direkt unter der base_url annimmt (Ideenwerft-Gateway).
JARVIS_AI_COMPLETION_PATH=/chat/completions
```

Nummern gehören nicht ins Git. Ist die Liste leer, startet Jarvis nicht.
Danach `sudo systemctl enable --now meshcore-jarvis.service` und mit
`journalctl -u meshcore-jarvis -f` einen Testanruf verfolgen.

## Abschalten

`sudo systemctl disable --now meshcore-jarvis.service`. Der Browser-Gesprächston
und die übrigen Dienste laufen unverändert weiter.
