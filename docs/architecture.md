# Architektur

`radio.py` kapselt das optionale MeshCore-SDK und USB. Eingehende Kanaltexte werden durch `protocol.py` geprüft und vor der Modellabfrage dauerhaft in SQLite gespeichert. `assistant.py` beantwortet feste Funksprüche direkt oder ruft ein konfiguriertes Modell mit begrenzten Lesewerkzeugen auf. Werkzeuge dürfen Wetter und lokale Repeaterinformationen lesen; sie dürfen weder Funk senden noch Shellbefehle ausführen.

`engine.py` erzeugt nummerierte UTF-8-Pakete. `store.py` verwaltet Zustände und Duplikaterkennung. Ein einziger Sender wartet auf Empfangsruhe und den konfigurierten Mindestabstand nach der Geräteantwort. Die Kanalregeln werden unmittelbar vor dem Senden erneut geprüft. Nach einem Absturz wird eine unterbrochene Sendung als unklar markiert; sie wird nicht blind wiederholt. Bereits angenommene Teile werden nicht erneut eingeplant.

`broadcasts.py` verarbeitet optionale DWD-/BBK-Warnungen und RSS. Ursprüngliche Gültigkeitszeiten begrenzen die Lebensdauer der Pakete. Empfang, KI und Sender laufen unabhängig; die serielle Schnittstelle wird durch eine gemeinsame Sperre geschützt.

Die Datenbank enthält keine Modelldateien. Ein Pi kann daher als Funkgateway und Anwendung ohne Serverbetrieb außerhalb des eigenen Netzes arbeiten. Ob die KI selbst lokal oder bei einem API-Anbieter läuft, entscheidet die Konfiguration.

Datenquellen: Open-Meteo Wetter/Geocoding, DWD WarnWetter-Daten, BBK NINA-Meldungen und individuell konfigurierte RSS-Feeds. Quellenbedingungen und Nutzungsgrenzen sind beim eigenen Betrieb zu beachten. Der Analyzer meshcorenetz.de wird nicht automatisiert abgefragt. Verlässliche Repeater-Aussagen beziehen sich auf Zeit und Quelle der tatsächlich lokal erfassten Daten.
