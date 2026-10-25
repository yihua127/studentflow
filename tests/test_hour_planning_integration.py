"""Whole-hour preferences through the UI draft and real offline agent tools."""

from copy import deepcopy
from datetime import datetime

from studentflow import planning_ui, scheduling_context
from studentflow.agent_tools import AssistantTools
from studentflow.planning_ui import default_preferences, make_draft
from studentflow.scheduling_context import saved_plan_is_fresh, with_fixed_events

NOW = "2030-01-07T12:14:00+08:00"


def fixtures():
    preferences = default_preferences()
    preferences["settings"]["meals"] = []
    preferences["available_slots"] = [{
        "id": "afternoon", "start": "2030-01-07T12:00:00+08:00",
        "end": "2030-01-07T18:00:00+08:00",
    }]
    tasks = [{
        "id": "notes", "title": "Review practice notes", "status": "confirmed",
        "estimated_minutes": 60, "completed_minutes": 0,
        "due_at": "2030-01-07T18:00:00+08:00",
    }]
    return preferences, tasks


def agent_plan(preferences, tasks, fixed_events=None):
    tools = AssistantTools({
        "now": NOW, "preferences": preferences, "tasks": tasks,
        "fixed_events": fixed_events or [], "completed_by_day": {},
    })
    response = tools.invoke("generate_study_plan", {}, role="planning")
    assert response["ok"] and response["saved"] is False
    assert not response["validation_errors"]
    return tools.artifacts["plans"][0]


def test_web_and_planning_agent_share_whole_hour_start_preference():
    preferences, tasks = fixtures()
    before = deepcopy((preferences, tasks))
    web = make_draft(tasks, preferences, NOW, {})
    agent = agent_plan(preferences, tasks)
    assert not web["validation_errors"]
    assert web["result"]["blocks"] == agent["result"]["blocks"]
    block = web["result"]["blocks"][0]
    assert block["start"] == "2030-01-07T13:00:00+08:00"
    assert block["end"] == "2030-01-07T14:00:00+08:00"
    assert block["minutes"] == 60
    assert (preferences, tasks) == before


def test_future_day_keeps_morning_availability_despite_current_afternoon_time():
    preferences, tasks = fixtures()
    preferences["available_slots"] = [{
        "id": "tomorrow", "start": "2030-01-08T09:00:00+08:00",
        "end": "2030-01-08T18:00:00+08:00",
    }]
    tasks[0]["due_at"] = "2030-01-08T18:00:00+08:00"
    web = make_draft(tasks, preferences, NOW, {})
    agent = agent_plan(preferences, tasks)
    assert not web["validation_errors"]
    assert web["result"]["blocks"] == agent["result"]["blocks"]
    assert web["result"]["blocks"][0]["start"] == "2030-01-08T09:00:00+08:00"


def test_web_and_agent_keep_non_hour_capacity_when_deadline_requires_it():
    preferences, tasks = fixtures()
    tasks[0]["due_at"] = "2030-01-07T13:14:00+08:00"
    web = make_draft(tasks, preferences, NOW, {})
    agent = agent_plan(preferences, tasks)
    assert not web["validation_errors"]
    assert web["result"]["blocks"] == agent["result"]["blocks"]
    assert not web["result"]["unscheduled"]
    assert web["result"]["blocks"][0]["start"] == NOW
    assert sum(block["minutes"] for block in web["result"]["blocks"]) == 60


def test_fixed_appointment_keeps_exact_times_and_blocks_both_planning_paths():
    preferences, tasks = fixtures()
    event = {
        "id": "presentation", "account": "student@example.invalid",
        "title": "Presentation", "kind": "presentation", "status": "confirmed",
        "start_at": "2030-01-07T13:10:00+08:00",
        "end_at": "2030-01-07T13:40:00+08:00",
        "in_person": False, "travel_minutes": 0, "location": "",
        "sources": [{"account": "student@example.invalid", "source_quote": "13:10 to 13:40"}],
    }
    before = deepcopy((preferences, tasks, event))
    web = make_draft(tasks, with_fixed_events(preferences, [event]), NOW, {})
    agent = agent_plan(preferences, tasks, [event])
    assert not web["validation_errors"]
    assert web["result"]["blocks"] == agent["result"]["blocks"]
    assert sum(block["minutes"] for block in web["result"]["blocks"]) == 60
    for block in web["result"]["blocks"]:
        assert datetime.fromisoformat(block["start"]).minute == 0
        assert block["end"] <= event["start_at"] or block["start"] >= event["end_at"]
    assert (preferences, tasks, event) == before


def test_policy_upgrade_invalidates_draft_confirmation_signature(monkeypatch):
    preferences, tasks = fixtures()
    current = planning_ui.planning_signature(tasks, preferences, NOW)
    monkeypatch.setattr(planning_ui, "PLANNING_POLICY_VERSION", "previous-policy")
    previous = planning_ui.planning_signature(tasks, preferences, NOW)
    assert current != previous


def test_policy_upgrade_invalidates_calendar_preview_but_keeps_saved_plan(monkeypatch):
    preferences, tasks = fixtures()
    plan = make_draft(tasks, preferences, NOW, {})
    before = deepcopy(plan)
    current = scheduling_context.source_fingerprint(tasks, preferences, [], {})
    monkeypatch.setattr(scheduling_context, "PLANNING_POLICY_VERSION", "previous-policy")
    previous = scheduling_context.source_fingerprint(tasks, preferences, [], {})
    assert current != previous
    assert saved_plan_is_fresh(plan, tasks, preferences, [], {}, NOW)
    assert plan == before
