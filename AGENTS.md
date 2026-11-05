# GitHub und Pi synchron halten

- Der Benutzer verlangt, Änderungen immer mit GitHub synchron zu halten.
- Änderungen inklusive der Pi-Integration im Repository speichern, sinnvoll prüfen,
  committen und nach GitHub übertragen. Vor einer regulären Pi-Auslieferung muss
  der veröffentlichte Stand die GitHub-Prüfungen bestehen. Notfallkorrekturen im
  selben Arbeitsauftrag nachziehen.
- Nach der Auslieferung Commit-ID und Laufzeitstatus auf dem Pi prüfen. Keine
  unbemerkten Änderungen nur in der laufenden Installation zurücklassen.
- Zugangsdaten, `.env`, Gesprächsdatenbanken und persönliche Laufzeitdaten niemals
  nach GitHub übertragen.
- Vor Veränderungen am Pi sichern. Der bestehende Gateway bleibt alleiniger
  Besitzer des MeshCore-USB-Geräts; DAB+ und App-Empfang erhalten.
- ASA-Testsignale nicht als echte Warnungen aussenden. Empfangene Signalisierung
  klar von einem amtlichen Warntext und von einer Entwarnung unterscheiden.
