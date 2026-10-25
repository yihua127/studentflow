"""Recurrence, fixed appointments and stale Calendar export across real boundaries."""

from copy import deepcopy
from datetime import datetime, time

from studentflow.availability import dated_override, weekly_rule
from studentflow.planning_ui import default_preferences, make_draft
from studentflow.scheduling_context import (
    saved_plan_is_fresh,
    source_fingerprint,
    with_fixed_events,
)

NOW = "2030-01-07T08:00:00+08:00"


def fixtures():
    day = datetime.fromisoformat(NOW).date()
    preferences = default_preferences()
    preferences["weekly_availability"] = [weekly_rule([day.weekday()], time(9), time(17))]
    tasks = [{"id": "task", "title": "Prepare slides", "status": "confirmed",
              "due_at": "2030-01-09T18:00:00+08:00", "estimated_minutes": 60,
              "completed_minutes": 15}]
    event = {"id": "exam", "account": "student@example.invalid", "title": "Exam",
             "kind": "exam", "start_at": "2030-01-07T09:00:00+08:00",
             "end_at": "2030-01-07T10:00:00+08:00", "status": "confirmed",
             "in_person": False, "travel_minutes": 0, "location": "",
             "sources": [{"account": "student@example.invalid", "source_quote": "Exam at 9 to 10."}]}
    return preferences, tasks, event


def test_fixed_appointments_block_recurring_plans_and_preserve_preferences():
    preferences, tasks, event = fixtures()
    before = deepcopy(preferences)
    effective = with_fixed_events(preferences, [event])
    plan = make_draft(tasks, effective, NOW, {})
    assert not plan["validation_errors"]
    assert plan["result"]["blocks"][0]["start"] == "2030-01-07T10:00:00+08:00"
    assert sum(block["minutes"] for block in plan["result"]["blocks"]) == 45
    assert preferences == before
    assert saved_plan_is_fresh(plan, tasks, preferences, [event], {}, NOW)


def test_calendar_sync_rejects_changed_tasks_events_or_removed_availability():
    preferences, tasks, event = fixtures()
    plan = make_draft(tasks, with_fixed_events(preferences, [event]), NOW, {})
    changed_tasks = deepcopy(tasks)
    changed_tasks[0]["completed_minutes"] = 30
    assert not saved_plan_is_fresh(plan, changed_tasks, preferences, [event], {}, NOW)
    changed_event = event | {"end_at": "2030-01-07T10:30:00+08:00"}
    assert not saved_plan_is_fresh(plan, tasks, preferences, [changed_event], {}, NOW)
    changed_preferences = deepcopy(preferences)
    changed_preferences["availability_overrides"] = [dated_override(
        datetime.fromisoformat(NOW).date(), "off",
    )]
    assert not saved_plan_is_fresh(plan, tasks, changed_preferences, [event], {}, NOW)


def test_expired_slot_cleanup_does_not_invalidate_an_unchanged_future_plan():
    preferences, tasks, event = fixtures()
    preferences["available_slots"].append({"id": "old", "start": "2030-01-06T09:00:00+08:00",
                                            "end": "2030-01-06T17:00:00+08:00"})
    plan = make_draft(tasks, with_fixed_events(preferences, [event]), NOW, {})
    preferences["available_slots"] = []
    assert saved_plan_is_fresh(plan, tasks, preferences, [event], {}, NOW)
    changed = deepcopy(preferences)
    changed["settings"]["daily_limit_minutes"] = 30
    assert not saved_plan_is_fresh(plan, tasks, changed, [event], {}, NOW)


def test_snapshot_fingerprint_changes_with_event_edits_but_not_wall_clock():
    preferences, tasks, event = fixtures()
    initial = source_fingerprint(tasks, preferences, [event], {})
    assert initial == source_fingerprint(deepcopy(tasks), deepcopy(preferences), [deepcopy(event)], {})
    assert initial != source_fingerprint(tasks, preferences, [event | {"title": "Changed exam"}], {})


def test_ongoing_session_does_not_block_export_of_later_unchanged_sessions():
    preferences, tasks, event = fixtures()
    tasks[0]["estimated_minutes"] = 180
    plan = make_draft(tasks, with_fixed_events(preferences, [event]), NOW, {})
    assert saved_plan_is_fresh(plan, tasks, preferences, [event], {}, "2030-01-07T10:15:00+08:00")
