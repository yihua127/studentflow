"""Recurring availability, migration and dated exclusions at planning boundaries."""

from copy import deepcopy
from datetime import date, datetime, time

import pytest

from studentflow.availability import (
    dated_override,
    normalize_availability,
    prune_expired_availability,
    resolve_availability,
    weekly_rule,
)
from studentflow.planning_ui import (
    default_preferences,
    make_draft,
    planning_signature,
    read_preferences,
    window,
    write_preferences,
)

NOW = "2030-01-07T08:00:00+08:00"  # Monday
DAY = date(2030, 1, 7)


def prefs():
    value = default_preferences()
    value["weekly_availability"] = [weekly_rule([0, 2], time(9), time(12))]
    value["availability_horizon_days"] = 7
    return value


def periods(value, now=NOW):
    return [(item["start"], item["end"]) for item in resolve_availability(value, now)]


def test_legacy_migration_preserves_future_slots_settings_and_unrelated_data(tmp_path):
    old = default_preferences()
    for field in ("weekly_availability", "availability_overrides", "availability_horizon_days"):
        old.pop(field)
    old["available_slots"] = [window(DAY, time(13), time(15))]
    old["settings"]["meals"] = []
    old["custom_setting"] = {"keep": True}
    path = tmp_path / "preferences.json"
    write_preferences(path, old)
    before = path.read_bytes()
    migrated = read_preferences(path)
    assert path.read_bytes() == before  # reading never silently rewrites originals
    assert migrated["available_slots"] == old["available_slots"]
    assert migrated["settings"] == old["settings"]
    assert migrated["custom_setting"] == old["custom_setting"]
    assert migrated["weekly_availability"] == []
    assert migrated["availability_overrides"] == []
    assert migrated["availability_horizon_days"] == 28
    assert periods(migrated) == [("2030-01-07T13:00:00+08:00", "2030-01-07T15:00:00+08:00")]


def test_multiple_weekdays_recur_without_materializing_saved_copies():
    value = prefs()
    value["availability_horizon_days"] = 14
    original = deepcopy(value)
    assert [start[:10] for start, _ in periods(value)] == [
        "2030-01-07",
        "2030-01-09",
        "2030-01-14",
        "2030-01-16",
    ]
    assert value == original


def test_union_of_recurrence_legacy_and_extra_time_has_no_duplicates():
    value = prefs()
    value["weekly_availability"].append(weekly_rule([0], time(10), time(13)))
    value["available_slots"] = [window(DAY, time(11), time(14))]
    value["availability_overrides"] = [dated_override(DAY, "add", time(13), time(15))]
    assert periods(value)[0] == ("2030-01-07T09:00:00+08:00", "2030-01-07T15:00:00+08:00")
    assert len(periods(value)) == 2


def test_removed_hours_split_windows_and_override_even_later_additions():
    value = prefs()
    value["availability_overrides"] = [
        dated_override(DAY, "remove", time(10), time(11)),
        dated_override(DAY, "add", time(9), time(13)),
    ]
    assert periods(value)[:2] == [
        ("2030-01-07T09:00:00+08:00", "2030-01-07T10:00:00+08:00"),
        ("2030-01-07T11:00:00+08:00", "2030-01-07T13:00:00+08:00"),
    ]
    value["weekly_availability"].append(weekly_rule([0], time(8), time(20)))
    assert all(
        not (start < "2030-01-07T11:00:00+08:00" and end > "2030-01-07T10:00:00+08:00")
        for start, end in periods(value)
    )


def test_overlapping_exclusions_and_day_off_do_not_resurrect_hours():
    value = prefs()
    value["availability_overrides"] = [
        dated_override(DAY, "remove", time(9), time(11)),
        dated_override(DAY, "remove", time(10), time(12)),
        dated_override(DAY, "off"),
        dated_override(DAY, "add", time(15), time(18)),
    ]
    assert periods(value) == [("2030-01-09T09:00:00+08:00", "2030-01-09T12:00:00+08:00")]
    # Explicit removal of all exclusions is how the routine is restored.
    value["availability_overrides"] = []
    assert periods(value)[0][0] == "2030-01-07T09:00:00+08:00"


def test_prune_uses_end_not_date_and_preserves_weekly_rules_and_history():
    value = prefs()
    value["available_slots"] = [
        window(DAY, time(8), time(10)),
        window(DAY, time(9), time(12)),
        window(date(2030, 1, 8), time(9), time(12)),
    ]
    value["availability_overrides"] = [
        dated_override(DAY, "remove", time(8), time(10)),
        dated_override(DAY, "remove", time(10), time(11)),
        dated_override(date(2030, 1, 6), "off"),
        dated_override(DAY, "off"),
    ]
    value["saved_plan"] = {"historic": True}
    before = deepcopy(value)
    cleaned, count = prune_expired_availability(value, "2030-01-07T10:00:00+08:00")
    assert count == 3
    assert len(cleaned["available_slots"]) == 2
    assert len(cleaned["availability_overrides"]) == 2
    assert cleaned["weekly_availability"] == before["weekly_availability"]
    assert cleaned["saved_plan"] == before["saved_plan"]
    assert value == before
    assert prune_expired_availability(cleaned, "2030-01-07T10:00:00+08:00")[1] == 0


def test_today_is_clipped_to_now_but_future_part_is_kept():
    value = prefs()
    value["availability_overrides"] = [dated_override(DAY, "remove", time(10), time(11))]
    assert periods(value, "2030-01-07T10:30:00+08:00")[0] == (
        "2030-01-07T11:00:00+08:00",
        "2030-01-07T12:00:00+08:00",
    )
    assert periods(value, "2030-01-07T11:15:00+08:00")[0] == (
        "2030-01-07T11:15:00+08:00",
        "2030-01-07T12:00:00+08:00",
    )


def test_horizon_is_bounded_by_local_date_and_retains_later_saved_changes():
    value = prefs()
    value["availability_horizon_days"] = 1
    value["availability_overrides"] = [dated_override(date(2030, 1, 9), "add", time(15), time(18))]
    assert len(periods(value)) == 1
    assert len(value["availability_overrides"]) == 1
    value["availability_horizon_days"] = 3
    assert len(periods(value)) == 3
    assert periods(value, "2030-01-06T16:00:00Z") == periods(value, "2030-01-07T00:00:00+08:00")


@pytest.mark.parametrize("horizon", [0, 91, True, 2.5, "28"])
def test_invalid_horizon_rejected(horizon):
    value = prefs()
    value["availability_horizon_days"] = horizon
    with pytest.raises(ValueError, match="Plan ahead"):
        normalize_availability(value)


@pytest.mark.parametrize(
    "days,start,end",
    [
        ([], time(9), time(10)),
        ([7], time(9), time(10)),
        ([True], time(9), time(10)),
        ([0], time(12), time(9)),
    ],
)
def test_invalid_weekly_rule_rejected(days, start, end):
    with pytest.raises(ValueError):
        weekly_rule(days, start, end)


def test_no_availability_is_invented():
    assert resolve_availability(default_preferences(), NOW) == []


def test_rules_exceptions_and_horizon_changes_invalidate_draft_signature():
    value = prefs()
    signature = planning_signature([], value, NOW)
    for field, replacement in [
        ("weekly_availability", [weekly_rule([1], time(9), time(12))]),
        ("availability_overrides", [dated_override(DAY, "off")]),
        ("availability_horizon_days", 8),
    ]:
        changed = deepcopy(value)
        changed[field] = replacement
        assert planning_signature([], changed, NOW) != signature


def test_make_draft_uses_resolved_windows_and_remaining_effort():
    value = prefs()
    value["settings"]["meals"] = []
    value["availability_overrides"] = [dated_override(DAY, "remove", time(9), time(10))]
    tasks = [
        {
            "id": "test",
            "title": "Study",
            "status": "confirmed",
            "estimated_minutes": 90,
            "completed_minutes": 30,
            "due_at": "2030-01-07T15:00:00+08:00",
        }
    ]
    draft = make_draft(tasks, value, NOW, {})
    assert not draft["validation_errors"]
    assert draft["result"]["blocks"][0]["start"] == "2030-01-07T10:00:00+08:00"
    assert sum(block["minutes"] for block in draft["result"]["blocks"]) == 60
    assert draft["inputs"]["slots"] == resolve_availability(value, NOW)


def test_maximum_horizon_does_not_expand_forever():
    value = prefs()
    value["weekly_availability"] = [weekly_rule(list(range(7)), time(9), time(10))]
    value["availability_horizon_days"] = 90
    assert len(resolve_availability(value, NOW)) == 90
    with pytest.raises(ValueError, match="timezone"):
        resolve_availability(value, datetime(2030, 1, 7, 9))  # noqa: DTZ001 -- verify rejection
