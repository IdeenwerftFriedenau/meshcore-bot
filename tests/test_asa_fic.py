import binascii

import pytest

from meshcore_ai_bot.asa_fic import FicState, location_code, matches_location, parse_fig


def fib(*figs):
    body = b"".join(figs)
    assert len(body) <= 30
    body += b"\xff" * (30 - len(body))
    return body + (binascii.crc_hqx(body, 0xFFFF) ^ 0xFFFF).to_bytes(2, "big")


def test_annex_f_published_london_reference():
    assert location_code(51.5187412, -0.1434571) == (10, 0xB736BB)


def test_real_received_regional_signal_is_not_for_friedenau():
    event = parse_fig(bytes.fromhex("090f418f01c894090033"))
    assert event["kind"] == "trigger" and not event["is_test"]
    assert event["locations"] == [{"zone": 1, "digits": 5, "code": 0x89409, "subcodes": 0x33}]
    assert not matches_location(event["locations"], 52.472, 13.331)


def test_other_ensemble_test_is_not_misread_as_tuned_real_alert():
    # The former decoder read the EId 0x418f as phase 1 / stage 0 / IId 15.
    event = parse_fig(bytes.fromhex("044f418ff3"))
    assert event["is_test"] and event["stage"] == 7
    assert event["target_id"] == "ensemble:418f"


def test_crc_and_discard_are_enforced():
    state = FicState()
    real = fib(bytes.fromhex("030f4183"))
    state.feed(real[:-1] + bytes([real[-1] ^ 1]), 100)
    assert not state.snapshot(100)["validated"]
    state.feed(fib(bytes.fromhex("032f4183")), 101)
    assert not state.snapshot(101)["signals"]
    state.feed(real, 102)
    assert state.snapshot(102)["signals"][0]["active"]
    state.feed(fib(bytes.fromhex("018f")), 103)
    assert not state.snapshot(103)["signals"][0]["active"]
    assert not state.snapshot(120)["validated"]


def test_incomplete_location_set_cannot_authorize_alert():
    state = FicState()
    # One further FIG is required. First location is outside the receiver area.
    state.feed(fib(bytes.fromhex("050f41834108")), 100)
    assert state.snapshot(100)["signals"] == []
    # Last continuation supplies the local area. Both locations must survive.
    state.feed(fib(bytes.fromhex("058f41830109")), 100.1)
    signals = state.snapshot(100.1)["signals"]
    assert len(signals) == 1 and len(signals[0]["locations"]) == 2
    assert matches_location(signals[0]["locations"], 52.472, 13.331)


@pytest.mark.parametrize("raw", ["010f", "020f41", "050f418301f9", "090f418f01c89409", "044f418f"])
def test_truncated_or_invalid_fields_fail_closed(raw):
    assert parse_fig(bytes.fromhex(raw)) is None
