import time

from meshcore_ai_bot import dab_control, dab_scan


def configure(monkeypatch, tmp_path):
    control = tmp_path / "control.json"
    dab_control.save_settings({"channel": "5C", "forward_asa": True}, control)
    monkeypatch.setattr(dab_scan, "SCAN_STATE", tmp_path / "scan.json")
    monkeypatch.setattr(dab_scan, "INVENTORY", tmp_path / "ensembles.json")
    monkeypatch.setattr(dab_scan, "load_settings", lambda: dab_control.load_settings(control))
    monkeypatch.setattr(
        dab_scan, "save_settings", lambda changes: dab_control.save_settings(changes, control)
    )
    monkeypatch.setattr(dab_scan, "PRESETS", {k: dab_control.PRESETS[k] for k in ("5C", "7D")})
    monkeypatch.setattr(dab_scan.time, "sleep", lambda duration: None)
    return control


def test_scan_collects_programmes_and_restores_channel_and_asa(monkeypatch, tmp_path):
    control = configure(monkeypatch, tmp_path)
    current = ["5C"]

    def tune(channel):
        current[0] = channel
        dab_scan.save_settings({"channel": channel})

    def mux():
        assert dab_control.load_settings(control)["forward_asa"] is False
        return {
            "ensemble": {"label": {"label": "Ensemble " + current[0]}},
            "demodulator": {"time_last_fct0_frame": time.time() * 1000, "snr": 15},
            "asa": {"ews_ensemble": current[0] == "5C"},
            "services": [
                {"sid": "0x1234", "label": {"label": "Radio"}, "components": [{"transportmode": "audio"}]}
            ],
        }

    monkeypatch.setattr(dab_scan, "tune", tune)
    monkeypatch.setattr(dab_scan, "mux", mux)
    dab_scan.scan()
    assert dab_control.load_settings(control) == {"version": 1, "channel": "5C", "forward_asa": True}
    inventory = dab_scan.read_json(dab_scan.INVENTORY, {})
    assert inventory["complete"]
    assert [r["channel"] for r in inventory["ensembles"]] == ["5C", "7D"]
    assert inventory["ensembles"][0]["services"][0]["audio"]
    assert inventory["ensembles"][0]["asa_observed"]
    assert not inventory["ensembles"][1]["asa_observed"]
    assert not dab_scan.scan_status()["running"]


def test_interrupted_scan_restores_original_settings(monkeypatch, tmp_path):
    control = configure(monkeypatch, tmp_path)
    visits = []

    def tune(channel):
        visits.append(channel)
        if channel == "7D":
            raise dab_scan.ScanCancelled()
        dab_scan.save_settings({"channel": channel})

    monkeypatch.setattr(dab_scan, "tune", tune)
    monkeypatch.setattr(dab_scan, "mux", lambda: {})
    dab_scan.scan()
    assert visits == ["5C", "7D", "5C"]
    assert dab_control.load_settings(control)["channel"] == "5C"
    assert dab_control.load_settings(control)["forward_asa"]
    assert dab_scan.scan_status()["error"] == "Suchlauf unterbrochen."


def test_band_iii_contains_all_38_receiver_blocks():
    assert len(dab_control.PRESETS) == 38
    assert dab_control.PRESETS["5A"]["frequency_mhz"] == 174.928
    assert dab_control.PRESETS["13F"]["frequency_mhz"] == 239.2
