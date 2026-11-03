# Betrieb

Der Dienst liest `/etc/meshcore-ai-bot/config.toml` und daneben `.env`. Nach Änderungen neu starten: `sudo systemctl restart meshcore-ai-bot`. Status: `systemctl status meshcore-ai-bot`; Logs: `journalctl -u meshcore-ai-bot`. Zwei Instanzen mit derselben Datenbank werden durch eine Dateisperre verhindert. Kein anderes Programm darf denselben USB-Port gleichzeitig verwenden.

## Fehlersuche

- Kein USB-Gerät: Companion-Firmware und Datenkabel prüfen, `/dev/serial/by-id/` verwenden. Der Dienstbenutzer benötigt `dialout`.
- Kanal fehlt: auf dem Gerät konfigurieren oder in der TOML `create_channels=true` setzen und den Dienst stoppen, bevor `provision` aufgerufen wird. Die USB-Konfiguration ist keine Funk-Testsendung.
- Keine Antwort: Public ist absichtlich gesperrt. In `#bot` muss KI als eigenes Wort im Text stehen. Alte oder eigene Nachrichten werden verworfen. Anschließend Warteschlange, KI-Konfiguration und Empfangsruhe prüfen.
- Keine freie KI-Antwort: Schlüssel, Modellname, Anbieterlimits und Internet prüfen. Es gibt persistente Minuten-/Tageslimits.
- Fehlende Teilnachricht: `accepted` bedeutet USB-Annahme, keine Funk-Zustellung. `uncertain` wird nicht automatisch wiederholt; dadurch werden Doppel- oder Endlossendungen vermieden. Eine neue Frage kann erneut beantwortet werden.
- Repeater gilt als unbekannt: nur lokal bekannte Repeater werden geführt. Kein Advert bedeutet nicht automatisch Ausfall. Es werden keine versteckten Funkabfragen ausgelöst.

## Warnungen und RSS

Beide Funktionen benötigen ausdrücklich gesetzte `*_enabled`-Schalter UND freigegebene `broadcasts` am Zielkanal. Beim ersten gesunden Abruf wird der aktuelle Bestand als Ausgangspunkt gespeichert, nicht komplett ausgesendet. Danach werden neue oder geänderte Meldungen berücksichtigt. Warnungen tragen Quellen und Zeitangaben; inhaltlich identische Meldungen für beide Regionen werden zusammengefasst. Veraltete oder zurückgezogene wartende Pakete werden verworfen. Die einzelnen Anbieter können dasselbe Ereignis in unterschiedlich formulierten Meldungen führen; solche Meldungen werden nicht unsicher zusammengeführt.

RSS versendet Überschrift und Link, keine vollständigen Artikel. Ein konfigurierter Stundenabstand ist ein Abfrageintervall ab Dienststart, keine Garantie exakt zur vollen Uhrstunde. Warnungen werden nicht durch KI gekürzt. Sehr lange Originalmeldungen können entsprechend viele Funkpakete benötigen.

## Datenschutz und Sicherung

Der Bot speichert Fragen und Antworten für kontextbezogene Rückfragen pro Absender und Kanal. Standard: 24 Stunden. Bereinigung läuft im Dienst regelmäßig. Kanalnamen, Absendernamen und Texte sind keine verlässlich authentifizierten Identitäten. Keine Passwörter über Funk senden.

Bei Online-KI werden Frage, begrenzter Verlauf, Stationskontext und gegebenenfalls Wetter-/Repeaterinformationen an den eingestellten Anbieter übertragen. Wetteranfragen übermitteln den gesuchten Ort an Open-Meteo. Lokale Ollama-Modelle halten die KI-Verarbeitung auf dem Pi; Wetter und Warnungen benötigen weiterhin Internet.

Für Backups den Dienst stoppen und `/var/lib/meshcore-ai-bot` sowie die geschützten Einstellungen sichern. Datenbank nicht gleichzeitig durch eine ältere Programmversion öffnen. Schlüssel niemals in Git einchecken. Journald folgt der Aufbewahrung des Betriebssystems; diese ist getrennt von der Bot-Datenbank zu konfigurieren. Es werden keine Audioarchive erzeugt.

Updates: Repository aktualisieren, Installer erneut ausführen, Dienst neu starten. Vorher Datenbank sichern. Bestehende Konfigurationsdateien werden vom Installer nicht überschrieben.


Dashboard: [Pi Console](dashboard.md). Lokaler Warnpfad: [DAB+ ASA](dab-asa.md). DWD und BBK können getrennte Bundesländer verwenden. Die ASA-Inbox wird unabhängig von Online-Abfragen verarbeitet.
