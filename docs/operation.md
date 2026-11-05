# Betrieb

Der Dienst liest `/etc/meshcore-ai-bot/config.toml` und daneben `.env`. Nach Änderungen neu starten: `sudo systemctl restart meshcore-ai-bot`. Status: `systemctl status meshcore-ai-bot`; Logs: `journalctl -u meshcore-ai-bot`. Zwei Instanzen mit derselben Datenbank werden durch eine Dateisperre verhindert. Kein anderes Programm darf denselben USB-Port gleichzeitig verwenden.

## Fehlersuche

- Kein USB-Gerät: Companion-Firmware und Datenkabel prüfen, `/dev/serial/by-id/` verwenden. Der Dienstbenutzer benötigt `dialout`.
- Kanal fehlt: auf dem Gerät konfigurieren oder in der TOML `create_channels=true` setzen und den Dienst stoppen, bevor `provision` aufgerufen wird. Die USB-Konfiguration ist keine Funk-Testsendung.
- Keine Antwort: Public ist absichtlich gesperrt. In `#bot` muss eine konfigurierte Erwähnung stehen, etwa `@Ideenwerft` oder `#ideenwerft`; KI/AI lösen nichts aus. Alte oder eigene Nachrichten werden verworfen. Anschließend Warteschlange, KI-Konfiguration und Empfangsruhe prüfen.
- Keine freie KI-Antwort: Schlüssel, Modellname, Anbieterlimits und Internet prüfen. Es gibt persistente Minuten-/Tageslimits.
- Fehlende Teilnachricht: `accepted` bedeutet USB-Annahme, keine Funk-Zustellung. `uncertain` wird nicht automatisch wiederholt; dadurch werden Doppel- oder Endlossendungen vermieden. Eine neue Frage kann erneut beantwortet werden.
- Repeater gilt als unbekannt: nur lokal bekannte Repeater werden geführt. Kein Advert bedeutet nicht automatisch Ausfall. Es werden keine versteckten Funkabfragen ausgelöst.

## Warnungen und RSS

Beide Funktionen benötigen ausdrücklich gesetzte `*_enabled`-Schalter UND freigegebene `broadcasts` am Zielkanal. Beim ersten gesunden Abruf wird der aktuelle Bestand als Ausgangspunkt gespeichert, nicht komplett ausgesendet. Danach werden neue oder geänderte Meldungen berücksichtigt. Warnungen tragen Quellen und Zeitangaben; inhaltlich identische Meldungen für beide Regionen werden zusammengefasst. Veraltete oder zurückgezogene wartende Pakete werden verworfen. Die einzelnen Anbieter können dasselbe Ereignis in unterschiedlich formulierten Meldungen führen; solche Meldungen werden nicht unsicher zusammengeführt.

RSS versendet Überschrift und Link, keine vollständigen Artikel. Ein konfigurierter Stundenabstand ist ein Abfrageintervall ab Dienststart, keine Garantie exakt zur vollen Uhrstunde. Warnungen werden nicht durch KI gekürzt. Überlange Meldungen erhalten eine ausdrücklich gekennzeichnete Kurzfassung mit Quelle, Gültigkeit und Region. Volltext und Handlungsempfehlungen stehen an der genannten Quelle; sie werden nicht als vollständig übertragener Inhalt ausgegeben. Passt selbst die Kurzfassung nicht in das konfigurierte Paketbudget, wird die ganze Meldung zurückgehalten und jeder zurückgehaltene Einplanungsversuch im Dashboard gezählt.

## Datenschutz und Sicherung

Der Bot speichert Fragen und Antworten für kontextbezogene Rückfragen pro sichtbarem Absender innerhalb des gewählten Memory-Bereichs, einschließlich nicht direkt an den Bot gerichteter Nachrichten. Hashtag-Kanäle teilen in der Einstellung auto einen Bereich; private Kanäle bleiben getrennt. Public und eigene Nachrichten bleiben ausgeschlossen. Standard: 24 Stunden. Bereinigung läuft im Dienst alle 30 Sekunden, auch ohne USB-Verbindung. Kanalnamen, Absendernamen und Texte sind keine verlässlich authentifizierten Identitäten. Keine Passwörter über Funk senden.

Bei Online-KI werden Frage, begrenzter Verlauf, Stationskontext und gegebenenfalls Wetter-/Repeaterinformationen an den eingestellten Anbieter übertragen. Wetteranfragen übermitteln den gesuchten Ort an Open-Meteo. Lokale Ollama-Modelle halten die KI-Verarbeitung auf dem Pi; Wetter und Warnungen benötigen weiterhin Internet.

Für Backups den Dienst stoppen und `/var/lib/meshcore-ai-bot` sowie die geschützten Einstellungen sichern. Datenbank nicht gleichzeitig durch eine ältere Programmversion öffnen. Schlüssel niemals in Git einchecken. Journald folgt der Aufbewahrung des Betriebssystems; diese ist getrennt von der Bot-Datenbank zu konfigurieren. Es werden keine Audioarchive erzeugt.

Updates: Bot und Dashboard stoppen, Repository aktualisieren, Installer erneut ausführen, Dienste neu starten. Vorher Datenbank sichern. Bestehende Konfigurationsdateien werden vom Installer nicht überschrieben.


Dashboard: [Pi Console](dashboard.md). Lokaler Warnpfad: [DAB+ ASA](dab-asa.md). DWD und BBK können getrennte Bundesländer verwenden. Die ASA-Inbox wird unabhängig von Online-Abfragen verarbeitet.

## Warteschlange und Lastgrenzen

Voreinstellung: höchstens fünf Pakete pro Antwort, zwölf Pakete pro Warnung/RSS-Meldung und 128 gleichzeitig wartende oder sendende Pakete insgesamt. Eine Meldung wird nur als ganze Paketfolge aufgenommen; bei Platzmangel gibt es keine halben neuen Meldungen. Beim Start und nach Konfigurationswechseln werden auch alte Warteschlangen auf die neuen Grenzen begrenzt; überzählige Paketfolgen werden vollständig abgebrochen. Warnquellen versuchen zurückgehaltene Online-Meldungen beim nächsten Abruf erneut, solange sie gültig sind.

Warnungen, Antworten und Nachrichten wechseln sich bei gefüllten Warteschlangen ab. Innerhalb der Warnungen gehen höhere Warnstufen vor. Eingehende adressierte Fragen sind auf sechs pro Minute insgesamt und zwei pro Minute und sichtbarem Funknamen begrenzt; höchstens 32 Fragen warten auf Verarbeitung. Auch lokale Kurzantworten unterliegen diesen Grenzen. Begrenzte Anfragen erzeugen keine zusätzliche Funk-Fehlermeldung. Die Zähler für Eingangs- und Modelllimits sowie die Versandreihenfolge überstehen Neustarts.

Die Statusdatei `<Datenbankpfad>.dashboard.json` enthält nur Status und Repeaterkontakte, keine Gesprächsinhalte. Sie wird atomar ersetzt, ist nur für den Dienstbenutzer lesbar und kann vom schreibgeschützten Dashboarddienst gelesen werden. Die bisherige direkte SQLite-Lesemethode dient nur als Übergang, wenn noch keine Statusdatei vorhanden ist.

Dateibasierte API- und Kanalschlüssel werden bei jedem gültigen Neuladen vollständig ersetzt. Das Entfernen eines Dateiwerts entfernt dessen Wirkung; ein ausdrücklich in der Dienstumgebung gesetzter Ersatzwert bleibt gültig. `.env` hat für Schlüssel Vorrang vor der Dienstumgebung. Ein leerer gespeicherter Wert deaktiviert den Schlüssel auch bei einem Ersatzwert. Dashboard-Zugangscodes werden beim Start gebunden; zum Ändern oder Entfernen den Dashboarddienst neu starten. Kompromittierte API-Schlüssel zusätzlich beim Anbieter widerrufen.

## Versandabbruch und Memory

`prepared` bedeutet Antwort erzeugt und eingereiht, `accepted` bedeutet alle Teile vom eigenen Gerät akzeptiert. Bei `failed`, `uncertain`, `cancelled` oder `expired` endet die restliche Paketfolge im betreffenden Kanal; andere Nachrichten und Zielkanäle laufen weiter. Auch nach einem Neustart werden unsichere Teile und deren Restfolge nicht erneut versendet. Nur vollständig akzeptierte Antworten werden als Assistentenbeiträge ins Memory übernommen. Frühere fälschlich erinnerte Antworten werden bei der Schema-3-Migration korrigiert.

Memory-Regeln gelten sowohl für das Lesen als auch für das Erfassen von Kontext. `auto`: Hashtag-Kanäle teilen Kontext, private Kanäle bleiben getrennt. `off`: keine Erinnerung dieses Kanals. `channel`: nur dieser Kanal. `shared`: ausdrückliche Freigabe für den gemeinsamen Bereich. `Vergiss mich` gilt für den sichtbaren Funknamen in allen Bereichen und schaltet neues Erinnern bis `memory an` ab. Die Befehle werden lokal ausgeführt; die Inspektion zeigt höchstens drei gefilterte letzte Nutzernachrichten aus dem aktuellen Bereich, keine vermeintlich gesicherte Persönlichkeitsbeschreibung.

Das Empfangsprotokoll hält höchstens 500 Metadateneinträge vor und zeigt die letzten 100. Es enthält Kanal, Funkname, Entscheidung, Zeit und Paketstatus, keine Nachrichtentexte. Die Statusdatei wird alle 30 Sekunden erneuert; ihre Altersanzeige verhindert, dass alte Werte für aktuell gehalten werden. Wartezeiten sind Schätzungen ohne Empfangsruhe, Quell- und Gerätezeiten.

## Updates und Rückkehr

Vor Update und Rollback beide Dienste stoppen. Der Installer erstellt ein separates Release und eine geschützte Sicherung, prüft Datenbankmigration, freien Speicher und Integrität, aktiviert danach `current` und aktualisiert Dienstdefinitionen. Ein Fehler nach dem Sicherungspunkt stellt den vorherigen Stand wieder her. Ein Fehler beim Erstellen des neuen Releases lässt den bisherigen Stand unverändert.

Die vorherige Programmumgebung bleibt erhalten. Die erste Aktualisierung einer älteren Installation sichert auch deren ursprüngliche Dienstdefinitionen und kann auf das frühere Layout zurückkehren. Die manuelle Rückkehr stellt den gesamten Sicherungsstand einschließlich Datenbank und Einstellungen wieder her; nachträgliche Daten gehen dabei verloren. Automatische Wiederherstellung nach einem Prozessfehler ist getestet; Schutz gegen jeden beliebigen Stromausfall mitten in mehreren Dateisystemoperationen wird nicht behauptet.
