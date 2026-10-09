"""Constraint tests using synthetic inputs only, never development answers."""

from datetime import datetime, timedelta

import pytest

from studentflow.planner import propose_schedule, validate_schedule


def stamp(clock, day="2026-10-12", offset="+08:00"):
    return f"{day}T{clock}:00{offset}"


def slot(start="08:00", end="22:00", day="2026-10-12"):
    return {"start": stamp(start, day), "end": stamp(end, day)}


def task(minutes=60, task_id="t1", due="22:00", **changes):
    value = {
        "id": task_id,
        "title": task_id,
        "due_at": stamp(due),
        "estimated_minutes": minutes,
        "status": "confirmed",
    }
    value.update(changes)
    return value


def checked(tasks, slots=None, busy=None, settings=None):
    slots = [slot()] if slots is None else slots
    busy = [] if busy is None else busy
    settings = {} if settings is None else settings
    plan = propose_schedule(tasks, slots, busy, settings)
    assert validate_schedule(plan["blocks"], tasks, slots, busy, settings) == []
    return plan


def test_meals_daily_cap_max_block_and_break():
    plan = checked([task(800)])
    assert sum(block["minutes"] for block in plan["blocks"]) == 360
    assert plan["unscheduled"][0]["remaining_minutes"] == 440
    assert max(block["minutes"] for block in plan["blocks"]) <= 120
    for block in plan["blocks"]:
        start, end = datetime.fromisoformat(block["start"]), datetime.fromisoformat(block["end"])
        for meal_start, meal_end in [("08:00", "08:30"), ("11:30", "12:00"), ("17:30", "18:00")]:
            assert end <= datetime.fromisoformat(stamp(meal_start)) or start >= datetime.fromisoformat(stamp(meal_end))
    for first, second in zip(plan["blocks"], plan["blocks"][1:]):
        assert datetime.fromisoformat(second["start"]) - datetime.fromisoformat(first["end"]) >= timedelta(minutes=30)


def test_duplicate_overlapping_slots_cannot_inflate_capacity():
    tasks = [task(300)]
    regular = checked(tasks, [slot("09:00", "11:00")])
    duplicated = checked(tasks, [slot("09:00", "11:00"), slot("09:00", "11:00"), slot("09:30", "10:30")])
    assert regular == duplicated
    assert sum(block["minutes"] for block in regular["blocks"]) == 120


def test_rest_between_separate_slots_for_the_same_task():
    plan = checked(
        [task(120)],
        [slot("09:00", "10:00"), slot("10:10", "11:10")],
        settings={"break_minutes": 30},
    )
    assert plan["blocks"][1]["start"] == stamp("10:30")
    assert sum(block["minutes"] for block in plan["blocks"]) == 100
    assert plan["unscheduled"][0]["remaining_minutes"] == 20


def test_earliest_deadline_first_and_never_after_deadline():
    plan = checked([task(60, "later", "13:00"), task(90, "urgent", "09:00")])
    assert plan["blocks"][0]["task_id"] == "urgent"
    assert plan["blocks"][0]["end"] == stamp("09:00")
    assert plan["unscheduled"][0]["remaining_minutes"] == 60


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "pending"},
        {"due_at": None},
        {"due_at": "2026-10-12T09:00:00"},
        {"estimated_minutes": 0},
        {"estimated_minutes": -20},
        {"estimated_minutes": True},
    ],
)
def test_incomplete_or_unconfirmed_tasks_excluded(changes):
    plan = checked([task(**changes)])
    assert not plan["blocks"]
    assert len(plan["unscheduled"]) == 1
    assert plan["unscheduled"][0]["reason"]


def test_insufficient_capacity_retains_remaining_minutes_without_false_proof():
    plan = checked([task(180)], [slot("09:00", "10:00")])
    assert plan["unscheduled"][0]["remaining_minutes"] == 120
    assert "不代表" in plan["unscheduled"][0]["reason"]


def test_default_onsite_commute_both_sides():
    busy = [{**slot("10:00", "11:00"), "mode": "onsite"}]
    plan = checked([task(300)], [slot("09:00", "12:30")], busy)
    assert plan["blocks"][0]["end"] == stamp("09:20")
    assert plan["blocks"][1]["start"] == stamp("12:00")
    assert "尚未查询真实路线" in plan["warnings"][0]


def test_online_has_no_commute():
    busy = [{**slot("10:00", "11:00"), "mode": "online"}]
    plan = checked([task(300)], [slot("09:00", "11:30")], busy)
    assert plan["blocks"][0]["end"] == stamp("10:00")
    assert plan["blocks"][1]["start"] == stamp("11:00")
    assert not plan["warnings"]


def test_manual_commute_overrides_default():
    busy = [{**slot("10:00", "11:00"), "mode": "onsite", "commute_minutes": 10}]
    plan = checked([task(300)], [slot("09:00", "11:30")], busy)
    assert plan["blocks"][0]["end"] == stamp("09:50")
    assert plan["blocks"][1]["start"] == stamp("11:10")
    assert not plan["warnings"]


def test_back_to_back_same_location_and_overlapping_busy_events():
    busy = [
        {**slot("10:00", "11:00"), "mode": "onsite", "location": "Room A"},
        {**slot("11:00", "12:00"), "mode": "onsite", "location": "Room A"},
        {**slot("11:45", "12:15"), "mode": "online"},
    ]
    plan = checked([task(300)], [slot("09:00", "14:00")], busy)
    assert plan["blocks"][0]["end"] == stamp("09:20")
    assert plan["blocks"][1]["start"] == stamp("12:40")


def test_now_clips_slots_and_expired_task_stays_unscheduled():
    plan = checked([task(30, "expired", "09:00"), task(60, "valid")], settings={"now": stamp("10:15")})
    assert plan["blocks"][0]["start"] == stamp("10:15")
    assert plan["unscheduled"][0]["task_id"] == "expired"


def test_multiday_cap_is_per_local_day():
    tasks = [task(720, due_at=stamp("22:00", "2026-10-13"))]
    plan = checked(tasks, [{"start": stamp("00:00"), "end": stamp("23:59", "2026-10-13")}])
    assert sum(block["minutes"] for block in plan["blocks"]) == 720
    assert len({block["start"][:10] for block in plan["blocks"]}) == 2


def test_dst_uses_local_dates_and_correct_offset():
    tasks = [task(600, due_at="2026-11-01T22:00:00-05:00")]
    slots = [{"start": "2026-10-31T00:00:00-04:00", "end": "2026-11-01T23:59:00-05:00"}]
    plan = checked(tasks, slots, settings={"timezone": "America/New_York"})
    assert sum(block["minutes"] for block in plan["blocks"]) == 600
    assert any(block["start"].endswith("-04:00") for block in plan["blocks"])
    assert any(block["start"].endswith("-05:00") for block in plan["blocks"])


def test_soft_buffer_does_not_hide_actual_capacity():
    plan = checked([task(60, due="10:00")], [slot("09:00", "10:00")], settings={"buffer_minutes": 1440})
    assert not plan["unscheduled"]
    assert any("建议提前量" in message for message in plan["warnings"])


def test_invalid_busy_event_fails_closed():
    tasks = [task()]
    plan = propose_schedule(tasks, [slot()], [{"start": "not a date", "end": stamp("10:00")}], {})
    assert not plan["blocks"]
    assert plan["warnings"]
    assert validate_schedule([], tasks, [slot()], [{"start": "bad"}], {})


def test_validator_rejects_tampered_plan():
    tasks = [task(60, due="10:00")]
    blocks = [
        {"task_id": "t1", "title": "x", "start": stamp("09:00"), "end": stamp("10:30"), "minutes": 90},
        {"task_id": "t1", "title": "x", "start": stamp("10:00"), "end": stamp("11:00"), "minutes": 60},
    ]
    issues = validate_schedule(blocks, tasks, [slot()], [], {})
    assert any("截止" in issue for issue in issues)
    assert any("重叠" in issue for issue in issues)
    assert any("超过预计耗时" in issue for issue in issues)


def test_validator_checks_rest_and_unconfirmed_tasks():
    tasks = [task(120)]
    blocks = [
        {"task_id": "t1", "title": "x", "start": stamp("09:00"), "end": stamp("10:00"), "minutes": 60},
        {"task_id": "t1", "title": "x", "start": stamp("10:15"), "end": stamp("11:15"), "minutes": 60},
    ]
    assert any("休息不足" in issue for issue in validate_schedule(blocks, tasks, [slot()], [], {}))
    tasks[0]["status"] = "pending"
    assert any("尚未确认" in issue for issue in validate_schedule(blocks, tasks, [slot()], [], {}))


def test_missing_and_duplicate_ids_not_scheduled():
    plan = checked([task(), task(), task(task_id="")])
    assert not plan["blocks"]
    assert len(plan["unscheduled"]) == 3
