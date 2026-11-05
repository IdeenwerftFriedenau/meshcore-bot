# Fehlertest und Pi-Dauerlauf

## Wiederholbarer Test ohne Funkhardware

Im Repository eine Entwicklungsumgebung mit `.[dev,radio]` installieren und dann:

```sh
python scripts/soak-test.py --cycles 3 --output /tmp/meshcore-soak-report.json
```

Der Test verwendet temporäre Datenbanken und den virtuellen seriellen Companion. Er öffnet keinen echten USB-Port und erzeugt keine Funksendungen. Geprüft werden unter anderem fehlende Bestätigungen, Verbindungsabbrüche, Neustart mit unterbrochener Sendung, vollständige Paketfolgen, Memory-Freigabe nach Geräteannahme, Konfigurationswechsel während der Sendepause, simulierte volle Datenbank sowie Update-/Rollback-Dateioperationen. Jeder Durchlauf ist auf zwei Minuten begrenzt; der erste Fehler beendet die Serie. Der JSON-Bericht wird nach jedem Durchlauf atomar ersetzt.

Die Simulation eines vollen Datenträgers erzeugt gezielt einen SQLite-Fehler; sie füllt nicht den tatsächlichen Datenträger. Tests ersetzen keine Prüfung von Antenne, Netzteil, realer SD-Karte, USB-Treiber und systemd auf dem Pi.

## Offline-Prüfung auf dem Pi

Bei gestopptem Funkdienst:

```sh
sudo -u meshcore-ai /opt/meshcore-ai-bot/current/.venv/bin/meshcore-ai doctor --config /etc/meshcore-ai-bot/config.toml
```

Erwartet: Datenbankintegrität und Schema in Ordnung, mindestens 100 MiB frei, Datenverzeichnis schreibbar. USB-Pfad vorhanden ist eine separate Information und wird nicht als Funknachweis ausgegeben. Der Befehl öffnet weder Gerät noch Netzwerkverbindung. Die Diagnosedatei enthält keine Fragen, Antworten oder Schlüssel.

## Echte Hardware: kontrollierter 24-Stunden-Lauf

Dieser Teil wird bewusst manuell ausgeführt, weil das Starten des Funkdiensts echte Antworten und freigegebene Rundsendungen ermöglichen kann.

1. Daten/Einstellungen sichern; eigenen Testkanal und zweites Gerät verwenden. Public bleibt gesperrt. Warnungen/RSS für einen reinen Antworttest ausschalten.
2. Version und Konfiguration protokollieren; Statusanzeige und Journal beobachten. Kurze und mehrteilige Fragen prüfen.
3. USB-Verbindung kontrolliert trennen und wiederherstellen. Erwartet: Status verliert die Verbindung, Bereinigung läuft weiter, keine Wiederholung unklarer Sendungen und keine verwaisten Folgeteile.
4. Internetzugang vorübergehend trennen. Lokale Hilfe/Memory-Befehle bleiben verfügbar; Online-Quellen liefern keinen erfundenen Erfolg. Nach Wiederkehr erholen sie sich.
5. Kanalregel während einer wartenden Antwort auf aus setzen. Erwartet: keine neue Aussendung nach der Regelübernahme und nachvollziehbarer Abbruchstatus.
6. Bot regulär neu starten. Akzeptierte Pakete werden nicht wiederholt; unsichere Paketfolgen bleiben beendet. Memory enthält nur vollständig vom Gerät akzeptierte Antworten.
7. Memory im privaten und im gemeinsamen Bereich prüfen, einschließlich Vergessen und anschließendem Neustart.
8. Nach 24 Stunden prüfen: Reaktionszeiten, Fehler, Quellenalter, freier Speicher und Einhaltung der Aufbewahrung. Ein USB-OK ist weiterhin keine Empfängerbestätigung.

Einen echten Stromausfall oder vollen Datenträger ausschließlich auf einem separaten Testsystem mit wiederherstellbarer SD-Karte prüfen. Für das normale System genügen die simulierten Fehlertests. Vor einem Release-Update und einer manuellen Rückkehr Dienste stoppen; Rollback stellt auch den alten Datenstand wieder her.
