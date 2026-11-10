"""The inbox list shows dates in the student's local (Singapore) time."""

from studentflow.inbox_workspace import _short_date


def test_short_date_converts_sender_zone_to_singapore():
    # Sent 22:45 Monday in California = 14:45 Tuesday in Singapore.
    assert _short_date("Mon, 09 Nov 2026 22:45:00 -0800") == "10 Nov"
    assert _short_date("2026-11-09T22:45:00-08:00") == "10 Nov"


def test_short_date_keeps_naive_and_invalid_values():
    assert _short_date("2026-11-09T22:45:00") == "09 Nov"
    assert _short_date("not a date") == ""
    assert _short_date(None) == ""
