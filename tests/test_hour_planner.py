"""Whole-hour starts are a preference, never a reason to lose feasible work."""

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from studentflow.planner import propose_schedule, validate_schedule


def stamp(clock, offset="+08:00"):
    if len(clock) == 5:
        clock += ":00"
    return f"2026-10-24T{clock}{offset}"


def task(task_id="study", minutes=60, due="18:00", offset="+08:00", **changes):
    return {
        "id": task_id,
        "title": task_id,
        "status": "confirmed",
        "estimated_minutes": minutes,
        "due_at": stamp(due, offset),
        **changes,
    }


def slot(start="12:00", end="18:00", offset="+08:00"):
    return {"start": stamp(start, offset), "end": stamp(end, offset)}


def checked(tasks, slots=None, busy=None, settings=None):
    slots = [slot()] if slots is None else slots
    busy = [] if busy is None else busy
    settings = {"meals": [], **(settings or {})}
    plan = propose_schedule(tasks, slots, busy, settings)
    assert validate_schedule(plan["blocks"], tasks, slots, busy, settings) == []
    return plan


def totals(plan):
    result = defaultdict(int)
    for block in plan["blocks"]:
        result[block["task_id"]] += block["minutes"]
    return dict(result)


@pytest.mark.parametrize(
    ("now", "expected"),
    [("12:14", "13:00"), ("12:00", "12:00"), ("12:00:01", "13:00")],
)
def test_start_uses_next_hour_and_never_rounds_seconds_backwards(now, expected):
    plan = checked([task()], settings={"now": stamp(now)})
    assert plan["blocks"][0]["start"] == stamp(expected)
    assert totals(plan) == {"study": 60}
    assert not plan["unscheduled"]


def test_each_later_task_starts_on_hour_with_real_durations_and_minimum_rest():
    plan = checked(
        [task("a", 60), task("b", 45), task("c", 45)],
        settings={"now": stamp("12:14")},
    )
    assert [block["start"] for block in plan["blocks"]] == [
        stamp("13:00"), stamp("15:00"), stamp("17:00")
    ]
    assert [block["end"] for block in plan["blocks"]] == [
        stamp("14:00"), stamp("15:45"), stamp("17:45")
    ]
    assert totals(plan) == {"a": 60, "b": 45, "c": 45}
    for previous, following in zip(plan["blocks"], plan["blocks"][1:]):
        assert (
            datetime.fromisoformat(following["start"])
            - datetime.fromisoformat(previous["end"])
        ) >= timedelta(minutes=30)


def test_one_long_task_uses_hour_starts_for_each_split_session():
    plan = checked([task(minutes=180)], settings={"now": stamp("12:14")})
    assert [block["start"] for block in plan["blocks"]] == [stamp("13:00"), stamp("16:00")]
    assert [block["minutes"] for block in plan["blocks"]] == [120, 60]
    assert not plan["unscheduled"]


def test_deadline_too_close_for_rounding_keeps_feasible_odd_start():
    plan = checked([task(minutes=45, due="13:00")], settings={"now": stamp("12:14")})
    assert plan["blocks"][0]["start"] == stamp("12:14")
    assert plan["blocks"][0]["end"] == stamp("12:59")
    assert totals(plan) == {"study": 45}
    assert not plan["unscheduled"]


def test_short_availability_keeps_work_instead_of_waiting_for_hour():
    plan = checked([task()], [slot("12:14", "13:14")])
    assert plan["blocks"][0]["start"] == stamp("12:14")
    assert totals(plan) == {"study": 60}
    assert not plan["unscheduled"]


def test_rounding_earlier_task_cannot_take_minutes_from_a_later_task():
    plan = checked(
        [task("early", 60, "14:30"), task("later", 50, "15:00")],
        [slot("12:14", "15:00")],
    )
    assert totals(plan) == {"early": 60, "later": 50}
    assert not plan["unscheduled"]
    starts = {block["task_id"]: block["start"] for block in plan["blocks"]}
    assert starts == {"early": stamp("12:14"), "later": stamp("14:00")}


def test_urgent_fallback_does_not_disable_hour_preference_for_roomy_task():
    plan = checked(
        [task("urgent", 30, "12:50"), task("roomy", 60, "18:00")],
        settings={"now": stamp("12:14")},
    )
    assert totals(plan) == {"urgent": 30, "roomy": 60}
    assert [(block["task_id"], block["start"]) for block in plan["blocks"]] == [
        ("urgent", stamp("12:14")), ("roomy", stamp("14:00"))
    ]


def test_partial_schedule_does_not_lose_existing_feasible_minutes():
    plan = checked([task(minutes=120)], [slot("12:14", "13:14")])
    assert totals(plan) == {"study": 60}
    assert plan["unscheduled"][0]["remaining_minutes"] == 60
    assert plan["blocks"][0]["start"] == stamp("12:14")


@pytest.mark.parametrize(("mode", "commute"), [("online", 0), ("onsite", 20)])
def test_fixed_event_odd_end_travel_and_meals_stay_protected(mode, commute):
    busy = [{**slot("10:00", "10:20"), "mode": mode, "commute_minutes": commute}]
    original_busy = deepcopy(busy)
    plan = checked(
        [task(minutes=90, due="15:00")],
        [slot("09:00", "15:00")],
        busy,
        {"meals": [{"start": "11:30", "end": "12:00"}]},
    )
    assert totals(plan) == {"study": 90}
    assert busy == original_busy
    assert all(datetime.fromisoformat(block["start"]).minute == 0 for block in plan["blocks"])
    excluded_start = datetime.fromisoformat(stamp("10:00")) - timedelta(minutes=commute)
    excluded_end = datetime.fromisoformat(stamp("10:20")) + timedelta(minutes=commute)
    for block in plan["blocks"]:
        start = datetime.fromisoformat(block["start"])
        end = datetime.fromisoformat(block["end"])
        assert end <= excluded_start or start >= excluded_end
        assert end <= datetime.fromisoformat(stamp("11:30")) or start >= datetime.fromisoformat(stamp("12:00"))


def test_local_hour_uses_configured_timezone_not_utc_hour():
    plan = checked(
        [task(offset="+05:45")],
        [slot(offset="+05:45")],
        settings={"timezone": "Asia/Kathmandu", "now": stamp("12:14", "+05:45")},
    )
    assert plan["blocks"][0]["start"] == stamp("13:00", "+05:45")
    assert totals(plan) == {"study": 60}


def test_hour_preference_preserves_daily_budget_and_remaining_progress():
    plan = checked(
        [task(minutes=120, completed_minutes=60)],
        settings={"now": stamp("12:14"), "completed_by_day": {"2026-10-24": 330}},
    )
    assert plan["blocks"][0]["start"] == stamp("13:00")
    assert totals(plan) == {"study": 30}
    assert plan["unscheduled"][0]["remaining_minutes"] == 30


def test_hour_preference_is_deterministic_and_does_not_mutate_inputs():
    inputs = (
        [task("later", 45, "15:00"), task("early", 60, "14:30")],
        [slot("12:14", "15:00")],
        [],
        {"meals": [], "now": stamp("12:14")},
    )
    snapshot = deepcopy(inputs)
    first = propose_schedule(*inputs)
    second = propose_schedule(*inputs)
    assert first == second
    assert inputs == snapshot
    assert validate_schedule(first["blocks"], *inputs) == []


def test_current_time_does_not_clip_tomorrow_morning_availability():
    plan = checked(
        [task(due_at="2026-10-25T18:00:00+08:00")],
        [{"start": "2026-10-25T09:00:00+08:00", "end": "2026-10-25T12:00:00+08:00"}],
        settings={"now": stamp("12:14")},
    )
    assert plan["blocks"][0]["start"] == "2026-10-25T09:00:00+08:00"
    assert totals(plan) == {"study": 60}


def test_work_that_cannot_fit_today_continues_at_nine_tomorrow():
    plan = checked(
        [task(minutes=120, due_at="2026-10-25T18:00:00+08:00")],
        [
            slot("09:00", "13:00"),
            {"start": "2026-10-25T09:00:00+08:00", "end": "2026-10-25T12:00:00+08:00"},
        ],
        settings={"now": stamp("12:14")},
    )
    assert plan["blocks"][0]["start"] == "2026-10-25T09:00:00+08:00"
    assert plan["blocks"][0]["end"] == "2026-10-25T11:00:00+08:00"
    assert totals(plan) == {"study": 120}
    assert not plan["unscheduled"]
