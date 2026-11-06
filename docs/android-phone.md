# Motorola als Offline-Ersatz für kurze Bot-Antworten

Der Pi bleibt für MeshCore, DAB+, Aufnahmen und lokale Spracherkennung zuständig.
Ein per USB freigegebenes Android-Handy kann bei einer Störung des primären
KI-Dienstes kurze Textantworten übernehmen. Die Antwort beginnt mit `Lokale KI:`.
Die lokale Verarbeitung verwendet keine Werkzeugaufrufe und keine aktuellen
Internetinformationen. Nachrichten aus RBB/DLF werden **nicht** auf das Handy
umgeleitet: Die getesteten kleinen Modelle waren dafür inhaltlich zu ungenau.

## Voraussetzungen und geprüfte Artefakte

- Moto g35 5G, ARM64, Android 16, 4 GB physischer RAM; der USB-Zugriff muss
  am entsperrten Handy freigegeben sein.
- Ubuntu-Paket `adb` auf dem Pi; der Gateway bleibt alleiniger Besitzer des
  MeshCore-USB-Geräts. Das Handy wird ausschließlich über seine private,
  konfigurierte Android-Gerätekennung angesprochen.
- Laufzeit und quantisiertes Modell stehen mit festen Download-Adressen und
  SHA256-Werten in [android-artifacts.json](../deploy/android-artifacts.json).
  Quelle der Laufzeit ist das offizielle llama.cpp-Projekt; das Modell ist die
  Q4_K_M-Konvertierung von Qwen3.5 2B. Beide Prüfsummen vor Verwendung prüfen.
  Gewichte, Binärdateien, USB-Schlüssel und Gerätekennungen gehören nicht ins Git.

Die Dateien werden auf dem Handy unter
`/data/local/tmp/meshcore-ai-b12167-qwen35-2b/` bereitgestellt: `model.gguf`
und die ausgepackte Android-Laufzeit im Unterordner `runtime/`.
Der Startdienst kontrolliert die Modell-Prüfsumme. Der Betrieb braucht kein
Root auf dem Handy. Vor dem erstmaligen Schreiben ein vorhandenes Verzeichnis
dieses Namens prüfen; keine fremden Handydateien überschreiben.

## Einbindung auf dem Pi

Vor Änderungen Konfiguration, Quellstand, Units und die SQLite-Datenbank
konsistent privat sichern. Erst einen veröffentlichten Stand mit erfolgreichen
GitHub-Prüfungen ausliefern. Den Core als neue inaktive Veröffentlichung
installieren und nach Prüfung aktivieren.

`deploy/meshcore-phone-ai.service` wird nach `/etc/systemd/system/` installiert.
Der Dienst verwendet `deploy/android_phone_ai.py` aus dem veröffentlichten
Quellstand. `/etc/meshcore-ai-bot/phone.env` enthält privat:

```dotenv
MESHCORE_ANDROID_SERIAL=PRIVATE_DEVICE_IDENTIFIER
MESHCORE_PHONE_API_KEY=PRIVATE_RANDOM_HEX_KEY
```

Der Schlüssel muss zufällig sein und mindestens 48 Hex-Zeichen umfassen.
Dieselbe Variable `MESHCORE_PHONE_API_KEY` in der privaten Bot-`.env` speichern;
Dateien restriktiv schützen und nicht ausgeben. Der Dienst schreibt den Schlüssel
über den freigegebenen USB-Kanal in eine private Datei auf dem Handy, nicht in
Prozessargumente oder das Statusprotokoll.

In der privaten Bot-Konfiguration:

```toml
[ai]
# Primären Anbieter und dessen Zugangsdaten beibehalten.
offline_fallback_url = "http://127.0.0.1:18180/v1"
offline_fallback_model = "motorola-local"
```

`systemctl daemon-reload` und
`systemctl enable --now meshcore-phone-ai.service` starten die Überwachung.
Der primäre Anbieter wird weiter verwendet. Bei Verbindungsfehlern oder
Serverfehlern ab HTTP 500 übernimmt das Handy. Fehlerhafte Anmeldung und
Ratenbegrenzungen werden nicht umgangen. Der bestehende Anfragezähler und die
Funkkanalregeln gelten auch für die lokale Antwort.

Die Ersatzantwort verwendet einen kurzen Offline-Prompt und höchstens vier
begrenzte Gesprächsbeiträge. Primäre Zugangsdaten und die Werkzeugliste werden
nicht ans Handy weitergegeben. Der lokale Client ignoriert HTTP-Proxys und
folgt keinen Weiterleitungen. Die Ersatzadresse muss eine numerische
Loopback-Adresse sein.

## Betrieb und Grenzen

ADB verwendet auf dem Pi ausschließlich `127.0.0.1:18137`; die Modell-API
`127.0.0.1:18180` wird über USB weitergereicht. Es wird kein KI-Port im WLAN
geöffnet. Andere Dienste auf den Standard-ADB-Ports werden nicht beendet.
Das Modell läuft auf dem Handy mit vier CPU-Threads, 4096 Kontext-Tokens und einem Verarbeitungsslot.

Der Dienst kontrolliert die Verbindung, startet nur seinen eigenen Modellprozess
und veröffentlicht einen privaten Status unter
`/var/lib/meshcore-ai-bot/phone/status.json`. Nach Abziehen des Handys sind lokale
KI-Antworten nicht verfügbar; der Pi-Funkbetrieb läuft weiter. Beim Stoppen des
Dienstes werden nur der eigene Handyprozess und seine Weiterleitung beendet.

Einfache Antworten können mehrere Sekunden dauern. Lange Prompts sind deutlich
langsamer. Tests mit der echten, fehlerhaften Radio-Transkription zeigten
inhaltliche Verwechslungen; deshalb bleiben automatische Nachrichten beim
bisherigen Anbieter. Die lokale KI kann ebenfalls irren und ersetzt keine
aktuellen Quellen. Wetter- und Ortssuchen benötigen ihre vorhandenen Online-APIs.

## Abschalten oder zurückkehren

`offline_fallback_url = ""` deaktiviert die Ersatzantwort. Den Handy-Dienst
mit `systemctl disable --now meshcore-phone-ai.service` abschalten, wenn er
nicht mehr benötigt wird. Der primäre Anbieter bleibt erhalten. Bei einem
Auslieferungsfehler auf die gesicherte Veröffentlichung und Konfiguration
zurückwechseln; die laufende Gesprächsdatenbank nicht durch eine alte Sicherung
ersetzen. Kein zusätzlicher MeshCore-Funkdienst wird gestartet.

## Motorola im Dashboard fernbedienen

Unter **Motorola** stehen ein manuell einschaltbares Livebild, Tippen/Wischen,
Home, Zurück, offene Apps, Display und Lautstärke sowie Texteingabe bereit.
**Vollbild öffnen** startet das Livebild und vergrößert die Ansicht. Tippen,
Wischen und die Bedientasten bleiben verfügbar. Mit **Vollbild schließen** oder
Esc zurückkehren; beim Seitenwechsel und beim Stoppen des Livebilds wird das
Vollbild ebenfalls beendet. Unterstützt der Browser keinen nativen Vollbildmodus,
wird die gesamte verfügbare Browserfläche genutzt.

Das ist eine Bildschirm-Fernbedienung, kein Audio- oder Kamerastream. Geschützte
Android-Inhalte können schwarz bleiben. Entsperren weiterhin am Handy; keine
Geräte-PIN im Dashboard speichern. Texte werden nicht automatisch abgesendet.
ADB-Texteingabe unterstützt hier druckbares ASCII; Umlaute über die sichtbare
Android-Tastatur eingeben.

Die Fernbedienung akzeptiert nur Tailscale- oder Loopback-Verbindungen. Im
normalen LAN bleibt die übrige Pi Console verfügbar. Bei Internet-Ausfall kann
man im erreichbaren lokalen Netz einen SSH-Tunnel verwenden:

```sh
ssh -N -L 127.0.0.1:18091:127.0.0.1:8091 ideenwerft-pi
```

Danach `http://127.0.0.1:18091/#phone` öffnen. Tailscale benötigt eine bestehende
Verbindung zwischen den Geräten; getrennte Netze werden ohne Übertragungsweg
nicht verbunden.

Ein optionaler **fester Zugangscode** erlaubt die Freigabe auch ohne erneute
Bestätigung am Handy. Er wird ausschließlich lokal eingerichtet; der Code selbst
kommt weder ins Repository noch in Statusantworten oder Protokolle. Gespeichert
wird ein gesalzener PBKDF2-SHA256-Prüfwert (600.000 Durchläufe, Datei 0600).
Nach fünf Fehlversuchen ist die Anmeldung 30 Minuten gesperrt, auch nach einem
Dashboard-Neustart. Eine Änderung des Codes widerruft alle Browserfreigaben.
Einrichten als Dashboard-Dienstbenutzer auf dem Pi (verdeckte Eingabe):

```sh
python3 /home/marc/meshcore-gateway/phone_remote.py
```

**Code am Handy anzeigen** erzeugt eine Android-Benachrichtigung mit achtstelligem
Code. Diesen innerhalb von zwei Minuten im eigenen Dashboard eingeben. Maximal
fünf Fehlversuche, höchstens fünf Codeanforderungen pro Stunde, eine pro Minute.
Die Browserfreigabe gilt sieben Tage und lässt sich mit **Browser abmelden**
widerrufen. Ein Gerätewechsel macht alte Freigaben ungültig. Bei verlorenem
Browserzugang können alle Freigaben durch Entfernen der privaten
`/var/lib/meshcore-gateway/phone-remote/sessions.json` widerrufen werden.

Cookies sind HttpOnly/SameSite=Strict; Bildschirm und Aktionen benötigen außerdem
einen Sitzungsschlüssel gegen fremde Webseiten. HTTP bleibt ausschließlich
innerhalb des verschlüsselten Tailscale-/SSH-Transportwegs. Keine Portfreigabe
ins Internet einrichten. Livebilder bleiben im Arbeitsspeicher, werden beim
Seitenwechsel/Verbergen gestoppt und serverseitig nur bis zu einer Sekunde als aktuelles Bild wiederverwendet.
Es gibt keine Bildschirmaufzeichnung oder Protokollierung der eingegebenen Texte.

Der bestehende USB-Wächter schreibt ein privates `device.json` neben seinen
Status (Verzeichnis 0700, Datei 0600). Der Dashboard-Benutzer muss derselbe
Dienstbenutzer sein. Der Dashboard-Helfer verwendet ausschließlich den bestehenden
ADB-Server auf Loopback-Port 18137 und startet keinen eigenen Daemon. Freigaben
liegen mit Modus 0600 im privaten Dashboard-Zustandsverzeichnis; nur Hashes werden
gespeichert. Geräteprofil, Sitzung, Freigabecode und Bildschirmdaten gehören
niemals ins Repository oder öffentliche Status-API.

Bei Auslieferung `phone_remote.py` neben `local_dashboard.py` installieren,
`android_phone_ai.py` im veröffentlichten Quellbaum aktualisieren und nur
`meshcore-phone-ai` sowie `ideenwerft-local-dashboard` neu starten. DAB-Empfang,
Nachrichtenplanung und MeshCore-USB-Besitz bleiben davon unabhängig.
