# Pi mit vorhandenem Gateway

Ein bestehender Gateway mit DAB+, Kontaktverwaltung und App-Anbindung darf nicht
parallel zum eigenständigen Funkdienst denselben USB-Port öffnen. Die Pi-Integration
verwendet deshalb `deploy/pi_gateway_bridge.py` im vorhandenen Collector. Der
Collector behält die USB-Verbindung und seinen gemeinsamen Sendetakt; die Bridge
nutzt Engine, Memory, Warteschlange und Statusdatei aus diesem Paket.

Vor dem Umbau Collector, Dashboard, Dienstdefinitionen und SQLite-Datenbanken
sichern. SQLite über die Backup-API kopieren. Die Bridge ersetzt den alten
Cloud-Antwortworker, nicht Empfang, Kontaktverwaltung oder manuelle Sendungen.
Empfangene App-Nachrichten dürfen weiterhin gespiegelt werden, aber ohne einen
zweiten Antwortprozess. Der lokale Bot verwendet ausschließlich seinen lokalen
Memory-Bereich; der App-Nachrichtenverlauf ist davon getrennt.
Eine unklare Funkannahme beendet die verbleibenden Antwortteile und erfordert eine
neue Gateway-Verbindung.

`deploy/existing-gateway/` enthält die zusammengeführten Ergänzungen für den
bestehenden Ideenwerft-Collector und sein lokales Dashboard. Patches erst auf einer
Kopie mit `patch --dry-run -p1` prüfen: Sie beziehen sich auf den Collector mit
dynamischem Kanalempfang, nicht auf beliebige Fremdversionen. `channel_mirror.py`
und `receive_channels.py` gehören neben den Collector, `pi_gateway_bridge.py`
ebenfalls. Der App-Server muss bereits `receive_only: true` strikt als reinen
Empfang behandeln und darf dabei keine Antwort erzeugen. Ohne diese Voraussetzung
die App-Spiegelung nicht aktivieren.

Der Collector muss beim Verbinden die Bridge erzeugen und starten, empfangene
Kanalnachrichten an `receive()` übergeben, Kontakte mit `update_contacts()`
aktualisieren, regelmäßig `check()` aufrufen und im `finally` auf `close()` warten.
Nach einer Änderung der empfangenen Kanalplätze muss auch `bridge.indices`
aktualisiert werden. Zusätzliche Empfangskanäle erhalten dadurch keine automatische
Antwortberechtigung. Public bleibt für Bot-Antworten gesperrt.

In dieser Betriebsart den eigenständigen Dienst `meshcore-ai-bot` deaktivieren und
zusätzlich über eine systemd-Startbedingung gegen versehentliches Starten schützen.
Gateway und Konfigurationsdashboard benötigen passende Rechte auf dieselben
Konfigurations- und Statusdateien. Keine parallelen Installationen starten.

## Vorhandener KI-Gateway

Für einen mit Chat Completions kompatiblen HTTPS-Endpunkt, dessen Adresse bereits
vollständig ist, kann `ai.completion_path = ""` gesetzt werden. Standardmäßig wird
weiterhin `/chat/completions` an `ai.base_url` angehängt. Der konfigurierte Schlüssel
bleibt in der geschützten `.env`; Weiterleitungen werden nicht automatisch verfolgt.
Die bestehende Budgetkontrolle des Gateways und die lokalen Aufruflimits bleiben
wirksam. Ein Offline-Start benötigt keinen KI-Zugriff.

## Zugriff ohne Internet

Die lokale Oberfläche muss HTML, CSS, JavaScript und gegebenenfalls Aufnahmen
direkt vom Pi laden. Online-Prüfungen gehören in den Hintergrund und dürfen die
lokale Statusseite nicht blockieren. Zugriff über die lokale IP oder den mDNS-Namen
des Pi funktioniert bei ausgefallenem Internet, solange Pi, Funkgerät und das
lokale Netzwerk mit Strom versorgt sind. Das ist kein eigener WLAN-Hotspot.

Das Konfigurationsdashboard bleibt standardmäßig auf Loopback. Zugriff über
`ssh -L 8765:127.0.0.1:8765 BENUTZER@PI-ADRESSE`, anschließend
`http://127.0.0.1:8765`. Vorhandene lokale Funkoberflächen können unabhängig davon
im lokalen Netz erreichbar bleiben. Keine Router-Portfreigabe einrichten.

Ohne Internet funktionieren lokale Funkwarteschlange, Funkkürzel, Ping, Rechnen
und Memory-Befehle. Aktuelle Online-Wetterdaten und entfernte KI-Modelle brauchen
ihre jeweilige Verbindung. DAB+-Aufnahmen und der vorhandene DAB+/ASA-Empfänger
bleiben lokal; eine KI-Zusammenfassung von Audio kann weiter Internet benötigen.

Den Ausfalltest in einem eigenen Linux-Netzwerk-Namespace mit ausschließlich
Loopback ausführen. Dabei Testdatenbanken und einen simulierten Funktransport
verwenden, um den echten Router und Funkbetrieb nicht zu unterbrechen. Zusätzlich
im erlaubten Testkanal eine kurze, gekennzeichnete Nachricht senden und die lokale
Funkannahme prüfen. Annahme durch das eigene Gerät beweist keinen Empfang bei
anderen Teilnehmern. Ein physischer Neustart und ein 24-Stunden-Dauerlauf sind
separate Prüfungen.

## Lokale Pi Console im GitHub-Stil

Die vollständige Oberfläche liegt unter `deploy/local-console/`. Sie verwendet
dieselben Farben und dieselbe Navigation wie die Konfigurationsoberfläche und
ergänzt Live-DAB+/ASA, das Audioarchiv, lokale Sendungen und Notfallhilfe.
`local_dashboard.py` ersetzt das bestehende Dashboard-Skript; `index.html`,
`console.css` und `console.js` liegen im Verzeichnis `local-console` daneben.
Alternativ legt `DASHBOARD_ASSETS` den absoluten Asset-Pfad fest. Keine zusätzlichen
Python-Pakete oder externen Web-Ressourcen sind erforderlich.

Die Bridge veröffentlicht nur die Namen der eingerichteten Empfangskanäle in der
Statusdatei, niemals deren Schlüssel. Unter Empfang & Versand kann der lokale
Funkempfang nach Kanal gefiltert werden. `/api/inbox?channel=%23bot` liest gezielt
bis zu 100 passende Nachrichten der letzten 24 Stunden; ein leerer Filter zeigt
alle Kanäle. Die Auswahl bleibt in der aktuellen Browsersitzung erhalten.

ASA-Anzeige: Empfang, vorhandene Signalisierung, aktives Warnsignal und
Funkweiterleitung werden getrennt dargestellt. Empfangsdaten ohne gültigen oder
frischen Decoder-Zeitpunkt gelten als unbestätigt. Der vorhandene passive
ASA-Monitor erfasst die Signalisierung; ab Version 0.4.3 kann die Gateway-Bridge
reale Signale automatisch nach `#warnings` weiterleiten. Dafür müssen die lokale
DAB-Einstellung `forward_asa`, `asa.enabled`, die Warnweiterleitung und die
Kanalberechtigung für `#warnings` eingeschaltet sein. Testsignale, unbestätigte
Kennzeichnungen und veraltete Daten werden verworfen. Episoden und Revisionen
werden dauerhaft dedupliziert. Die Meldung kennzeichnet ausdrücklich ein
empfangenes Warnsignal ohne verfügbaren amtlichen Warntext; daraus werden keine
Gefahrenbeschreibung und keine Entwarnung abgeleitet. Es werden keine simulierten
Warnungen in einen echten Funkkanal eingespeist.

Vor einem Oberflächen-Update Backend, Bridge und Asset-Pfad sichern. Nur Bridge
und Dashboard austauschen; den Collector mit seinem dynamischen Kanalempfang
beibehalten. Anschließend Gateway und lokales Dashboard neu starten und die
Status- und Asset-Endpunkte auf erfolgreiche Antworten prüfen. Das geschützte
Konfigurationsdashboard bleibt unabhängig davon auf Loopback erreichbar.

## DAB+-Ensemble umschalten

Die Auswahl unter DAB+ ASA unterstützt 5C (Bundesmux), 7D (RBB Berlin,
194,064 MHz) und 10B (RBB Brandenburg/Berlin, 211,648 MHz). Grundlage für die
RBB-Voreinstellungen: https://www.rbb-online.de/radio/frequenzen/
Der vorhandene Empfänger überwacht immer nur ein Ensemble. Bei RBB fehlen
währenddessen der Empfang von 5C und neue DLF-Aufnahmen; das vorhandene Archiv
bleibt verfügbar. Die Auswahl wird lokal in
`/var/lib/meshcore-gateway/dab-control.json` gespeichert. Die Oberfläche zeigt
gewünschten und tatsächlich bestätigten Kanal getrennt an.

Für diese Erweiterung `deploy/dab/dab_monitor.py` am bisherigen Monitor-Pfad
installieren und `src/meshcore_ai_bot/dab_control.py` als `dab_control.py` daneben
ablegen. Der vorhandene Monitor behält seinen SDR-Prozess und übernimmt die
Auswahl über dessen ausschließlich lokale Kanal-Schnittstelle. Der Gateway
verwendet das installierte Python-Paket ab Version 0.4.3. Das lokale Dashboard
benötigt ebenfalls `dab_control.py` neben seinem Skript, wenn sein System-Python
das Paket nicht installiert hat. Einstellungen und Empfänger bleiben ohne
Internet bedienbar. Vor der Auslieferung nach GitHub synchronisieren und den
veröffentlichten Commit auf dem Pi dokumentieren.

## Verifizierte Rohsignalisierung ab 0.4.3

Die frühere reine Prüfung des Decoder-JSON reichte nicht aus: Ein real empfangenes
regional begrenztes Signal war nicht für Friedenau bestimmt. Deshalb werden rohe
FIBs aus dem ausschließlich lokalen `/fic`-Strom ausgewertet. Jede FIB benötigt
eine gültige CRC; FIG 0/15 wird einschließlich P/D, OE, Phase, Stage, vollständiger
mehrteiliger Location-Sets und Subcodes nach ETSI TS 104 089 Annex E/F gelesen.
Ein warmer Neuanschluss verwirft zunächst den begrenzten Decoder-Puffer.

Nur vollständig bestätigte Trigger für den konfigurierten Empfängerstandort
werden weitergeleitet. `asa.location_latitude` und `asa.location_longitude` sind
dafür ausdrücklich zu setzen (Voreinstellung Friedenau). Andere Ensembles ohne
Lokalisierung werden nicht als lokal relevant behauptet. Testsignale, Discard-
Signalisierung, beschädigte FIBs und unvollständige Location-Sets bleiben gesperrt.
Heartbeat und End beenden nur den technischen Signalzustand; es wird keine
amtliche Entwarnung daraus erzeugt. Ohne frische Rohbestätigung gilt die ASA-
Anzeige als unbestätigt. Die alte Decoder-Zusammenfassung autorisiert keine Sendung.

Primärquelle: https://www.etsi.org/deliver/etsi_ts/104000_104099/104089/01.01.01_60/ts_104089v010101p.pdf

## Alle empfangbaren Ensembles und Programme

Der lokale Suchlauf prüft alle 38 DAB+-Band-III-Blöcke (5A bis 13F), wartet auf
einen frischen Ensemble-Lock und sammelt auf belegten Blöcken zusätzlich den
langsameren Programmnamen-Zyklus. Ergebnisse unter
`/var/lib/meshcore-gateway/dab-ensembles.json` bleiben ausschließlich auf dem Pi;
sie sind keine Repository-Dateien. Der Suchlauf zeigt seinen Fortschritt und
stellt danach Kanal sowie den vorherigen ASA-Schalter wieder her. Bei einer
fehlgeschlagenen Kanal-Wiederherstellung bleibt ASA ausgeschaltet. Während des
Suchlaufs besteht kein kontinuierlicher Warnempfang und keine neue DLF-Aufzeichnung.

Pro Ensemble werden Block, Frequenz, Signalwert, Radioprogramme und Datum
angezeigt. Eine empfangene FIG-0/15-/EWS-Signalisierung wird als ASA nachgewiesen
ausgewiesen. Fehlende Signalisierung während des begrenzten Suchlaufs heißt
„unbestätigt“, nicht „unterstützt ASA nicht“. Empfangbarkeit ist der Stand des
letzten Suchlaufs und kann sich mit Signalbedingungen ändern. Dieselbe
Ensemble-Signalisierung gilt für dessen Programme.

Das System-Python des lokalen Dashboards benötigt neben `dab_control.py` auch
`dab_scan.py`, wenn das Paket nur in der privaten Programmumgebung installiert
ist. Der Suchlauf startet ausschließlich das fest vorgegebene installierte Modul
`meshcore_ai_bot.dab_scan`, ohne Shell und ohne frei übergebbare Befehle.


### Live-Radio und stündlicher Deutschlandfunk

Unter „Radio & Aufnahmen“ kann der Browser Radioprogramme des empfangenen Ensembles
lokal abspielen. Die Wiedergabe startet nur auf Klick; der Stream bleibt auf dem Pi
und nutzt keinen Internet-Radiodienst. Bis zu 16 lokale Wiedergaben teilen sich denselben
DAB-Empfänger. Ein Ensemblewechsel unterbricht andere Programme.

Der Empfänger wechselt unabhängig von Internet-Uploads jede Stunde ab Minute 59:30
auf 5C (Deutschlandfunk). Bis Minute 05:50 hat die bestehende fünfminütige
Nachrichtenaufzeichnung Vorrang. Danach wird die gespeicherte Auswahl wieder
empfangen. Unterbrochene Live-Wiedergabe anschließend erneut starten. Die Auswahl
wird durch die Reservierung nicht überschrieben. Suchläufe lassen sich nur zwischen
Minute 06 und 49 starten und werden bei einer laufenden Nachrichtenreservierung
abgebrochen; Kanal und ASA-Schalter werden wiederhergestellt.


### Direkte Senderauswahl

„Radio & Aufnahmen“ zeigt nur alphabetisch sortierte Sendernamen. Mehrfach
empfangene Programmkennungen werden zusammengeführt. Ein Klick auf „Radio hören“
stellt automatisch den passenden DAB-Kanal ein und wartet auf aktuelle
Empfangsdaten des gewählten Senders. Die Senderauswahl bleibt beim Aktualisieren
erhalten. Deutschlandfunk-Aufnahme und Suchlauf haben weiterhin Vorrang.
Manuelle Ensemble-Einstellungen bleiben auf der ASA-Seite unter der optionalen
Empfangstechnik erreichbar.


### Gleichzeitige Audioausgabe

Der lokale Audioproxy verteilt pro empfangenem Programm einen gemeinsamen
Decoder-Stream an mehrere Geräte. Jede Wiedergabe besitzt einen begrenzten Puffer;
ein langsames oder getrenntes Gerät blockiert die anderen nicht. Maximal 16
Wiedergaben können gleichzeitig verbunden sein. Die Oberfläche zeigt deren Anzahl.

Gleiche Sender lassen sich parallel hören; verschiedene Programme des aktuell
empfangenen Ensembles ebenfalls. Ein Wechsel zu einer anderen Empfangsfrequenz
unterbricht die dort nicht verfügbaren Programme. Stündliche DLF-Umschaltung und
Suchlauf behalten Vorrang. Das ist keine zwischen Geräten zeitsynchrone Wiedergabe;
die Browser können unterschiedliche Pufferverzögerungen haben.

### Radio-Studio: Aufnahme, Titelinfos und Online-Erkennung

Die Radioseite besitzt eine helle Radiooberfläche und mehrere getrennte
Player. Jeder eingeschaltete Sender erhält Lautstärke-/Pause-Regler, Aufnahme und
Songerkennung. Parallele Sender benötigen dieselbe empfangene Frequenz; ein einzelner
SDR kann keine unterschiedlichen Ensembles gleichzeitig empfangen.

Der gemeinsame Audiostream puffert bis zu 16 MP3-Blöcke (64 KiB) vor. Bei geringerer
Bitrate beginnt die Wiedergabe nach etwa fünf Sekunden mit dem vorhandenen Puffer.
Später hinzukommende Geräte erhalten den jüngsten Puffer. Geräte laufen dadurch
nicht zwingend zeitsynchron.

Eigene Aufnahmen dauern wahlweise 1, 5 oder 15 Minuten und lassen sich vorzeitig
stoppen. Bis zu vier Aufnahme-/Erkennungsaufträge sind gleichzeitig möglich.
Pro Aufnahme gelten 32 MiB, insgesamt 512 MiB und 24 Stunden Aufbewahrung. Die
Dateien und Auftragsdaten liegen nur unter
`/var/lib/meshcore-gateway/radio-recordings`; dieser Pfad ist für den bestehenden
Dashboard-Dienst schreibbar. Ein Empfangsabbruch kann die Aufnahme verkürzen.

DLS-Sendertexte erscheinen lokal beim jeweiligen Player. Nur frische, nach dem
letzten Empfangswechsel eingegangene Texte werden gezeigt. Ein Sendertext ist
keine garantierte Songerkennung. „Song online erkennen“ erfasst auf ausdrücklichen
Klick etwa zwölf Sekunden Radioaudio, erzeugt mit ShazamIO einen Fingerabdruck und
fragt den Shazam-Dienst ab. Kein Mikrofon wird verwendet. Internet ist erforderlich;
Fehler oder kein Treffer werden angezeigt. Der inoffizielle Client ist optional:

```
python3 -m venv /opt/meshcore-ai-bot/radio-recognition/.venv
/opt/meshcore-ai-bot/radio-recognition/.venv/bin/pip install -r deploy/local-console/recognition-requirements.txt
```

Neben `local_dashboard.py` müssen `radio_features.py` und `recognize_song.py` im
lokalen Dashboard-Verzeichnis liegen. Zugangsdaten sind dafür nicht erforderlich.
Referenz: https://github.com/shazamio/ShazamIO
