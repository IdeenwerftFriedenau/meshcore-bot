# DAB+ ASA: Offline-Warnschnittstelle

## Empfänger: Nooelec RTL-SDR V5

Der Nooelec NESDR SMArt V5 verwendet RTL2832 und R860 und kann mit passender Antenne DAB empfangen. Das macht ihn nicht automatisch zu einem ASA-Empfänger. Im Repository enthalten sind ein Empfängerprofil, eine optionale Treiberinstallation, passive Geräteerkennung und eine lokale Ereignisschnittstelle. **Ein vollständiger ASA-Funkdecoder ist nicht enthalten. Ein ASA-fähiger Decoderadapter muss separat bereitgestellt und am Standort geprüft werden.**

DAB+ ASA verbreitet Warnungen terrestrisch ohne Internet. Eine Internetstörung verhindert das Einlesen und Einreihen lokaler ASA-Ereignisse nicht. Voraussetzung sind ein laufender Pi, Strom, Antenne, Empfang, ein funktionierender externer Decoderadapter und der MeshCore-Companion für die Weiterleitung. Regionale Verfügbarkeit und die Eignung des Decoders prüfen. Es gibt keine garantierte Funkzustellung.

`welle.io`/`welle-cli` kann DAB+ mit RTL-SDR empfangen. Die bloße Installation wird ausdrücklich nicht als bestätigte ASA-Decodierung gewertet. Normale Radiotexte, Testmeldungen, unvollständige Warnungen oder ein vorhandener Stick werden nicht als empfangene ASA-Warnung ausgegeben.

## Einrichtung

```sh
sudo bash scripts/setup-rtlsdr.sh
# Stick erneut einstecken; optionaler Empfangstest, mit Strg+C beenden:
sudo -u meshcore-ai rtl_test -d 0 -s 2048000
```

Wenn ein DVB-T-Treiber das Gerät belegt, zunächst die lokale Treiberbelegung untersuchen. Das Installationsskript deaktiviert keine Systemtreiber automatisch. DAB-Empfang mit passender VHF-Band-III-Antenne und separatem Decoder prüfen. Der DAB-Kanal 5C ist nur eine Voreinstellung im Profil. Kanal/Geräteindex im Dashboard werden gespeichert, aber nicht automatisch an einen unbekannten externen Decoder übermittelt.

## Lokaler Adaptervertrag

Ein separat eingerichteter Decoderadapter muss echte ASA-Ereignisse in dieses Format normalisieren. IDs, Zielregionen, Zeiten und Inhalt müssen aus der decodierten Meldung stammen; niemals Text erfinden oder aus Radiotexten Warnungen ableiten. Beispiel nur zur Formatbeschreibung (kein Live-Ereignis):

```json
{
  "version": 1,
  "protocol": "DAB+ ASA",
  "status": "Actual",
  "kind": "alert",
  "id": "stable-source-event-id",
  "received_at": "2026-11-03T10:00:00+01:00",
  "onset": "2026-11-03T10:00:00+01:00",
  "expires": "2026-11-03T12:00:00+01:00",
  "regions": ["BER", "BB"],
  "title": "Titel aus der Quelle",
  "severity": "Schweregrad aus der Quelle",
  "area": "Zielgebiet aus der Quelle",
  "description": "Vollständiger Warninhalt aus der Quelle",
  "instruction": "Optionale Empfehlung aus der Quelle"
}
```

`kind` ist `alert`, `update` oder `cancel`. Aktualisierung und Aufhebung verwenden dieselbe stabile ID. Aufhebungen benötigen ebenfalls einen vollständigen, noch gültigen Quellzeitraum; überholte Fragmente werden verworfen, es wird keine erfundene Entwarnung gesendet.

```sh
sudo -u meshcore-ai env MESHCORE_AI_ASA_INBOX=/var/lib/meshcore-ai-bot/asa \
 /opt/meshcore-ai-bot/.venv/bin/meshcore-ai asa-ingest \
 --config /etc/meshcore-ai-bot/config.toml /pfad/echte-asa-meldung.json
```

Der Dienst verwendet denselben Inbox-Pfad. Außerhalb der systemd-Dienste gilt `asa.inbox` relativ zur Konfigurationsdatei, oder die genannte Umgebungsvariable. `asa-ingest` validiert ohne Netzwerk und schreibt atomar, öffnet keine Funkhardware. Alternativ darf ein vertrauenswürdiger lokaler Adapter vollständige JSON-Dateien atomar in die Inbox legen. Dateien maximal 64 KB, Empfangszeit standardmäßig höchstens 300 Sekunden alt, Zeitangaben mit Zeitzone und Ablaufzeit in der Zukunft. Ungültige Dateien werden isoliert und blockieren nachfolgende Meldungen nicht. Verarbeitete/abgewiesene Ereignisse werden standardmäßig nach 24 Stunden bereinigt.

Im Dashboard ASA aktivieren, ASA-Bundesländer auswählen und automatische Warnweiterleitung bewusst freigeben. Mindestens ein zulässiger Warnkanal muss konfiguriert sein. Der laufende Bot prüft die lokale Inbox unabhängig von Online-Quellen alle zwei Sekunden. Die erste frische ASA-Meldung wird berücksichtigt; identische Revisionen werden nicht wiederholt. Gemeinsame Warnungen werden mit gemeinsamer Kennung einmal pro Zielkanal eingereiht. Public und Fragekanäle bleiben ausgenommen. Empfehlungen sind standardmäßig aus, Quellzeiten und vollständige Beschreibung bleiben erhalten.

## Primärquellen

- [DAB+ ASA](https://www.dabplus.de/asa/)
- [Nooelec Datenblatt](https://www.nooelec.com/datasheets/100700)
- [welle.io Quellcode](https://github.com/AlbrechtL/welle.io)
