import copy
import json

import pytest

from meshcore_ai_bot.asa_signal import forward_signal
from meshcore_ai_bot.config import Channel, Config
from meshcore_ai_bot.dab_control import load_settings, save_settings
from meshcore_ai_bot.store import Store


@pytest.fixture
def setup(tmp_path):
    config = Config()
    config.asa.enabled = True
    config.broadcasts.warnings_enabled = True
    config.channels = [Channel("#warnings", broadcasts=["warnings"]), Channel("Public")]
    store = Store(tmp_path / "state.sqlite3")
    now = 1000
    mux = {
        "asa": {"active": True, "is_test": False, "status": "actual", "iid": 15, "level": 1},
        "ensemble": {"label": {"label": "DR Deutschland"}},
        "demodulator": {"time_last_fct0_frame": 999000},
        "receiver": {"software": {"lastchannelchange": 990000}},
    }
    yield config, store, mux, {"channel": "5C", "forward_asa": True}, now
    store.close()


def test_actual_signal_only_targets_warnings_and_survives_restart_without_duplicates(setup):
    config, store, mux, settings, now = setup
    assert forward_signal(config, store, mux, "5C", settings, now) == "queued"
    rows = store.db.execute("SELECT channel,text FROM packets").fetchall()
    assert rows and {row["channel"] for row in rows} == {"#warnings"}
    assert "Kein amtlicher Warntext" in " ".join(row["text"] for row in rows)
    count = len(rows)
    assert forward_signal(config, store, mux, "5C", settings, now + 1) == "duplicate"
    path = store.path
    store.close()
    restarted = Store(path)
    try:
        assert forward_signal(config, restarted, mux, "5C", settings, now + 2) == "duplicate"
        assert restarted.db.execute("SELECT count(*) FROM packets").fetchone()[0] == count
    finally:
        restarted.close()


@pytest.mark.parametrize(
    "change", ["test", "missing_test", "stale", "retune_cache", "disabled", "channel_policy", "asa_disabled"]
)
def test_test_signals_stale_data_and_disabled_routes_never_queue(setup, change):
    config, store, mux, settings, now = setup
    if change == "test":
        mux["asa"]["is_test"] = True
    if change == "missing_test":
        del mux["asa"]["is_test"]
    if change == "stale":
        mux["demodulator"]["time_last_fct0_frame"] = 900000
    if change == "retune_cache":
        mux["receiver"]["software"]["lastchannelchange"] = 999500
    if change == "disabled":
        settings["forward_asa"] = False
    if change == "channel_policy":
        config.broadcasts.warnings_enabled = False
    if change == "asa_disabled":
        config.asa.enabled = False
    assert forward_signal(config, store, mux, "5C", settings, now) != "queued"
    assert store.counts() == {}


def test_tuning_elsewhere_does_not_end_an_active_episode(setup):
    config, store, mux, settings, now = setup
    forward_signal(config, store, mux, "5C", settings, now)
    other = copy.deepcopy(mux)
    other["asa"]["active"] = False
    other["ensemble"]["label"]["label"] = "rbb Berlin"
    assert forward_signal(config, store, other, "7D", settings, now) == "idle"
    assert forward_signal(config, store, mux, "5C", settings, now) == "duplicate"
    mux["asa"]["active"] = False
    forward_signal(config, store, mux, "5C", settings, now)
    mux["asa"]["active"] = True
    assert forward_signal(config, store, mux, "5C", settings, now) == "queued"


def test_queue_full_retries_same_episode_without_losing_signal(setup):
    config, store, mux, settings, now = setup
    store.limits.max_pending_packets = 0
    assert forward_signal(config, store, mux, "5C", settings, now) == "queue_full"
    store.limits.max_pending_packets = 128
    assert forward_signal(config, store, mux, "5C", settings, now) == "queued"


def test_control_settings_validate_and_preserve_other_fields(tmp_path):
    path = tmp_path / "control.json"
    assert load_settings(path)["forward_asa"] is False
    save_settings({"forward_asa": True}, path)
    assert save_settings({"channel": "7D"}, path) == {"version": 1, "channel": "7D", "forward_asa": True}
    before = path.read_bytes()
    with pytest.raises(ValueError):
        save_settings({"channel": "7D;reboot"}, path)
    assert before == path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(before)["channel"] == "7D"
