import pytest

from meshcore_ai_bot.dab_control import effective_channel, news_window, scan_allowed


@pytest.mark.parametrize(
    "second,reserved,scan",
    [
        (0, True, False),
        (349, True, False),
        (350, False, True),
        (2969, False, True),
        (2970, False, False),
        (3569, False, False),
        (3570, True, False),
        (3599, True, False),
        (3600, True, False),
    ],
)
def test_hourly_dlf_reservation_preserves_listener_choice(second, reserved, scan):
    settings = {"channel": "7D", "forward_asa": True}
    assert news_window(second) is reserved
    assert effective_channel(settings, second) == ("5C" if reserved else "7D")
    assert settings["channel"] == "7D"
    assert scan_allowed(second) is scan
