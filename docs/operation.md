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

Der Bot speichert Fragen und Antworten für kontextbezogene Rückfragen pro sichtbarem Absender kanalübergreifend, einschließlich nicht direkt an den Bot gerichteter Nachrichten in konfigurierten Kanälen. Public und eigene Nachrichten bleiben ausgeschlossen. Standard: 24 Stunden. Bereinigung läuft im Dienst alle 30 Sekunden, auch ohne USB-Verbindung. Kanalnamen, Absendernamen und Texte sind keine verlässlich authentifizierten Identitäten. Keine Passwörter über Funk senden.

Bei Online-KI werden Frage, begrenzter Verlauf, Stationskontext und gegebenenfalls Wetter-/Repeaterinformationen an den eingestellten Anbieter übertragen. Wetteranfragen übermitteln den gesuchten Ort an Open-Meteo. Lokale Ollama-Modelle halten die KI-Verarbeitung auf dem Pi; Wetter und Warnungen benötigen weiterhin Internet.

Für Backups den Dienst stoppen und `/var/lib/meshcore-ai-bot` sowie die geschützten Einstellungen sichern. Datenbank nicht gleichzeitig durch eine ältere Programmversion öffnen. Schlüssel niemals in Git einchecken. Journald folgt der Aufbewahrung des Betriebssystems; diese ist getrennt von der Bot-Datenbank zu konfigurieren. Es werden keine Audioarchive erzeugt.

Updates: Bot und Dashboard stoppen, Repository aktualisieren, Installer erneut ausführen, Dienste neu starten. Vorher Datenbank sichern. Bestehende Konfigurationsdateien werden vom Installer nicht überschrieben.


Dashboard: [Pi Console](dashboard.md). Lokaler Warnpfad: [DAB+ ASA](dab-asa.md). DWD und BBK können getrennte Bundesländer verwenden. Die ASA-Inbox wird unabhängig von Online-Abfragen verarbeitet.

## Warteschlange und Lastgrenzen

Voreinstellung: höchstens fünf Pakete pro Antwort, zwölf Pakete pro Warnung/RSS-Meldung und 128 gleichzeitig wartende oder sendende Pakete insgesamt. Eine Meldung wird nur als ganze Paketfolge aufgenommen; bei Platzmangel gibt es keine halben neuen Meldungen. Beim Start und nach Konfigurationswechseln werden auch alte Warteschlangen auf die neuen Grenzen begrenzt; überzählige Paketfolgen werden vollständig abgebrochen. Warnquellen versuchen zurückgehaltene Online-Meldungen beim nächsten Abruf erneut, solange sie gültig sind.

Warnungen, Antworten und Nachrichten wechseln sich bei gefüllten Warteschlangen ab. Innerhalb der Warnungen gehen höhere Warnstufen vor. Eingehende adressierte Fragen sind auf sechs pro Minute insgesamt und zwei pro Minute und sichtbarem Funknamen begrenzt; höchstens 32 Fragen warten auf Verarbeitung. Auch lokale Kurzantworten unterliegen diesen Grenzen. Begrenzte Anfragen erzeugen keine zusätzliche Funk-Fehlermeldung. Die Zähler für Eingangs- und Modelllimits sowie die Versandreihenfolge überstehen Neustarts.

Die Statusdatei `<Datenbankpfad>.dashboard.json` enthält nur Status und Repeaterkontakte, keine Gesprächsinhalte. Sie wird atomar ersetzt, ist nur für den Dienstbenutzer lesbar und kann vom schreibgeschützten Dashboarddienst gelesen werden. Die bisherige direkte SQLite-Lesemethode dient nur als Übergang, wenn noch keine Statusdatei vorhanden ist.

Dateibasierte API- und Kanalschlüssel werden bei jedem gültigen Neuladen vollständig ersetzt. Das Entfernen eines Dateiwerts entfernt dessen Wirkung; ein ausdrücklich in der Dienstumgebung gesetzter Ersatzwert bleibt gültig. `.env` hat für Schlüssel Vorrang vor der Dienstumgebung. Ein leerer gespeicherter Wert deaktiviert den Schlüssel auch bei einem Ersatzwert. Dashboard-Zugangscodes werden beim Start gebunden; zum Ändern oder Entfernen den Dashboarddienst neu starten. Kompromittierte API-Schlüssel zusätzlich beim Anbieter widerrufen.
