"""Offline contract tests for the assistant's scoped, non-persisting tools."""

import json
import sqlite3
from copy import deepcopy

import pytest

from studentflow import agent_tools
from studentflow.agent_tools import MAX_CALLS, ROLES, AssistantTools
from studentflow.planner import validate_schedule


def stamp(day=21, clock="09:00"):
    return f"2026-10-{day:02d}T{clock}:00+08:00"


@pytest.fixture
def snapshot():
    return {
        "sources": [
            {"source_id": "email-1", "provider": "gmail", "account": "student@example.invalid",
             "title": "Essay deadline", "text": "Please submit the essay on 23 October at 17:00. It needs 90 minutes.",
             "sent_at": stamp(), "sender": "Teacher <teacher@example.invalid>"},
            {"source_id": "chat-1", "provider": "google_chat", "account": "student@example.invalid",
             "title": "Project discussion", "thread_id": "thread-1",
             "text": "Alex will prepare the slides. The group agreed to meet on Friday.", "sent_at": stamp()},
        ],
        "tasks": [
            {"id": "task-1", "title": "Essay", "due_at": stamp(23, "17:00"),
             "estimated_minutes": 90, "completed_minutes": 30, "status": "confirmed",
             "source_quote": "Please submit the essay", "mail_id": "mail-1", "updated_at": stamp()},
        ],
        "preferences": {
            "available_slots": [{"start": stamp(21, "09:00"), "end": stamp(21, "14:00")}],
            "busy_events": [],
            "settings": {"timezone": "Asia/Singapore", "meals": [], "daily_limit_minutes": 360},
        },
        "completed_by_day": {},
        "calendar_events": [{"id": "event-1", "title": "Seminar", "start": stamp(21, "09:00"),
                             "end": stamp(21, "10:00"), "mode": "online"}],
        "calendar_coverage": {"calendar_ids": ["primary"], "time_min": stamp(21, "00:00"),
                              "time_max": stamp(24, "00:00"), "loaded_at": stamp(21, "08:00")},
        "academic_dates": [{"name": "Reading week", "start": "2026-10-19", "end": "2026-10-25",
                            "source": "User-reviewed academic calendar"}],
        "style_samples": [{"source_id": "style-1", "text": "Dear Professor, thank you for your help."}],
        "now": stamp(21, "08:00"),
    }


def citation(identifier="email-1", quote="Please submit the essay"):
    return {"source_id": identifier, "quote": quote}


def proposal(**changes):
    return {"title": "Submit essay", "citations": [citation()], **changes}


def call(box, name, args=None, role="task"):
    return box.invoke(name, args or {}, role=role)


def test_role_whitelists_and_metadata_events(snapshot):
    box = AssistantTools(snapshot)
    for role in ROLES:
        assert "read_source" in {spec.name for spec in box.specs_for(role)}
    result = call(box, "record_task_proposals", {"proposals": [proposal()]}, role="email")
    assert result["error"]["code"] == "tool_not_allowed"
    assert call(box, "send_email", {"api_key": "SECRET"}, role="email")["ok"] is False
    assert call(box, "read_source", {"source_id": "email-1"}, role="unknown")["ok"] is False
    assert "SECRET" not in json.dumps(box.events)
    assert all(set(event) <= {"sequence", "role", "tool", "status", "error_code"} for event in box.events)
    assert not any(box.artifacts.values())


def test_source_scope_paging_and_manifest(snapshot):
    box = AssistantTools(snapshot)
    manifest = call(box, "list_sources", {"limit": 1})
    assert manifest["next_offset"] == 1
    assert "text" not in manifest["sources"][0]
    read = call(box, "read_source", {"source_id": "chat-1", "offset": 5, "limit": 12})
    assert read["text"] == snapshot["sources"][1]["text"][5:17]
    assert read["untrusted_source_text"] is True
    denied = call(box, "read_source", {"source_id": "../../credentials/key.json"})
    assert denied["error"]["code"] == "source_out_of_scope"
    assert "credentials" not in json.dumps(box.events)


@pytest.mark.parametrize("arguments", ["not json", "[]", {"limit": True}, {"limit": "5"}, {"path": "secret"}])
def test_strict_arguments_and_json_errors(snapshot, arguments):
    box = AssistantTools(snapshot)
    assert box.invoke("list_sources", arguments, role="task")["ok"] is False


def test_json_arguments_work_and_call_limit(snapshot):
    box = AssistantTools(snapshot)
    for _ in range(MAX_CALLS):
        assert box.invoke("list_sources", '{"limit": 1}', role="task")["ok"] is True
    assert call(box, "list_sources")["error"]["code"] == "call_limit"


def test_task_search_returns_evidence_not_merge_decision(snapshot):
    box = AssistantTools(snapshot)
    result = call(box, "search_tasks", {"query": "Essay"})
    assert result["tasks"][0]["id"] == "task-1"
    assert result["tasks"][0]["evidence"][0]["source_quote"] == "Please submit the essay"
    assert "not merge decisions" in result["note"]
    result["tasks"][0]["title"] = "Changed by caller"
    assert call(box, "search_tasks")["tasks"][0]["title"] == "Essay"


def test_task_proposals_preserve_unknowns_and_are_idempotent(snapshot):
    box = AssistantTools(snapshot)
    original = deepcopy(snapshot)
    first = call(box, "record_task_proposals", {"proposals": [proposal()]})
    second = call(box, "record_task_proposals", {"proposals": [proposal(), proposal()]})
    assert first["ok"] and second["ok"]
    assert first["artifact_ids"] == second["artifact_ids"]
    assert len(box.artifacts["task_proposals"]) == 1
    saved = box.artifacts["task_proposals"][0]
    assert saved["due_at"] is None and saved["estimated_minutes"] is None
    assert saved["status"] == "proposal" and saved["requires_confirmation"]
    assert saved["saved"] is False
    assert snapshot == original


def test_same_title_in_different_sources_is_not_automatically_merged(snapshot):
    box = AssistantTools(snapshot)
    items = [proposal(), proposal(citations=[citation("chat-1", "Alex will prepare the slides.")])]
    assert call(box, "record_task_proposals", {"proposals": items})["ok"]
    assert len(box.artifacts["task_proposals"]) == 2


@pytest.mark.parametrize("bad_quote", ["please submit the essay", "The deadline was postponed", "   "])
def test_invalid_citation_rejects_entire_batch(snapshot, bad_quote):
    box = AssistantTools(snapshot)
    items = [proposal(), proposal(title="Other task", citations=[citation(quote=bad_quote)])]
    result = call(box, "record_task_proposals", {"proposals": items})
    assert result["error"]["code"] == "invalid_citation"
    assert not box.artifacts["task_proposals"]


def test_update_and_duplicate_require_existing_scoped_task(snapshot):
    box = AssistantTools(snapshot)
    for relation in ("duplicate", "update", "conflict"):
        denied = call(box, "record_task_proposals", {"proposals": [proposal(relation=relation, existing_task_id="unselected")]})
        assert denied["error"]["code"] == "task_out_of_scope"
        assert call(box, "record_task_proposals", {"proposals": [proposal(relation=relation, existing_task_id="task-1", due_at=stamp(24, "17:00"))]})["ok"]
    assert all(item["previous"]["completed_minutes"] == 30 for item in box.artifacts["task_proposals"])
    assert snapshot["tasks"][0]["due_at"] == stamp(23, "17:00")
    assert call(box, "record_task_proposals", {"proposals": [proposal(existing_task_id="task-1")]})["ok"] is False


def test_effort_suggestion_separate_from_source_fact(snapshot):
    box = AssistantTools(snapshot)
    assert call(box, "record_task_proposals", {"proposals": [proposal(suggested_minutes=60)]})["ok"] is False
    assert call(box, "record_task_proposals", {"proposals": [proposal(suggested_minutes=60, estimate_reason="A first-pass estimate; review complexity.")]})["ok"]
    value = box.artifacts["task_proposals"][0]
    assert value["estimated_minutes"] is None and value["suggested_minutes"] == 60
    assert call(box, "record_task_proposals", {"proposals": [proposal(suggested_minutes=60, estimated_minutes=90, estimate_reason="Guess")]})["ok"] is False


@pytest.mark.parametrize("due", ["tomorrow", "2026-10-23", "2026-10-23T17:00:00"])
def test_ambiguous_or_naive_dates_rejected(snapshot, due):
    result = call(AssistantTools(snapshot), "record_task_proposals", {"proposals": [proposal(due_at=due)]})
    assert result["error"]["code"] == "invalid_datetime"


def test_triage_cites_its_own_source_and_does_not_touch_mail(snapshot):
    box = AssistantTools(snapshot)
    item = {"source_id": "email-1", "category": "needs_action", "reason": "An essay is requested", "citations": [citation()]}
    assert call(box, "record_triage", {"items": [item]}, role="email")["mailbox_changed"] is False
    item["citations"] = [citation("chat-1", "Alex will prepare the slides.")]
    assert call(box, "record_triage", {"items": [item]}, role="email")["ok"] is False
    assert len(box.artifacts["triage"]) == 1


def test_reply_draft_and_selected_style_only(snapshot):
    box = AssistantTools(snapshot)
    draft = {"source_id": "email-1", "subject": "Re: Essay", "body": "Thank you for the details.",
             "recipient": "teacher@example.invalid", "citations": [citation()], "style_source_ids": ["style-1"]}
    assert call(box, "record_reply_draft", draft, role="email")["sent"] is False
    assert box.artifacts["reply_drafts"][0]["sent"] is False
    draft["recipient"] = "unverified@example.invalid"
    assert call(box, "record_reply_draft", draft, role="email")["error"]["code"] == "unknown_recipient"
    draft["recipient"] = None
    draft["style_source_ids"] = ["unselected-style"]
    assert call(box, "record_reply_draft", draft, role="email")["error"]["code"] == "source_out_of_scope"


def test_reply_clarification_returns_questions_without_a_draft_or_writes(snapshot, monkeypatch):
    def reject_database(*_args, **_kwargs):
        raise AssertionError("Reply clarification must not open a database.")

    monkeypatch.setattr(sqlite3, "connect", reject_database)
    original = deepcopy(snapshot)
    box = AssistantTools(snapshot)
    result = call(box, "request_reply_clarification", {
        "source_id": "email-1", "questions": ["  Should I accept the requested deadline?  "],
    }, role="email")
    assert result == {
        "ok": True, "needs_clarification": True, "source_id": "email-1",
        "questions": ["Should I accept the requested deadline?"],
        "draft_recorded": False, "sent": False,
    }
    assert not any(box.artifacts.values())
    assert snapshot == original
    assert "requested deadline" not in json.dumps(box.events)


def test_reply_clarification_allows_a_missing_target_but_not_an_unselected_id(snapshot):
    box = AssistantTools({})
    result = call(box, "request_reply_clarification", {
        "source_id": None, "questions": ["Which email would you like to answer?"],
    }, role="email")
    assert result["ok"] and result["needs_clarification"]
    assert result["source_id"] is None and not any(box.artifacts.values())
    box = AssistantTools(snapshot)
    for identifier in ("unselected-email", "style-1"):
        result = call(box, "request_reply_clarification", {
            "source_id": identifier, "questions": ["Which date should I use?"],
        }, role="email")
        assert result["error"]["code"] == "source_out_of_scope"
    assert not any(box.artifacts.values())


@pytest.mark.parametrize("questions", [[], [""], ["  \n "], ["x" * 1001], ["Question?"] * 6])
def test_reply_clarification_rejects_blank_or_excessive_questions(snapshot, questions):
    box = AssistantTools(snapshot)
    result = call(box, "request_reply_clarification", {
        "source_id": "email-1", "questions": questions,
    }, role="email")
    assert result["ok"] is False
    assert not any(box.artifacts.values())


@pytest.mark.parametrize("role", ["task", "planning", "meeting"])
def test_reply_clarification_is_available_only_to_the_email_specialist(snapshot, role):
    box = AssistantTools(snapshot)
    assert "request_reply_clarification" in {spec.name for spec in box.specs_for("email")}
    assert "request_reply_clarification" not in {spec.name for spec in box.specs_for(role)}
    result = call(box, "request_reply_clarification", {
        "source_id": "email-1", "questions": ["Which date should I use?"],
    }, role=role)
    assert result["error"]["code"] == "tool_not_allowed"
    assert not any(box.artifacts.values())


def test_meeting_summary_quotes_and_owners(snapshot):
    box = AssistantTools(snapshot)
    proof = citation("chat-1", "Alex will prepare the slides.")
    value = {"title": "Project decisions", "summary": "Slides need preparation.", "citations": [proof],
             "action_items": [{"title": "Prepare slides", "owner": "Alex", "due_at": None, "citations": [proof]}]}
    result = call(box, "record_meeting_summary", value, role="meeting")
    assert result["ok"] and result["tasks_created"] is False and result["sent"] is False
    value["action_items"][0]["owner"] = "Unknown Person"
    assert call(box, "record_meeting_summary", value, role="meeting")["error"]["code"] == "unsupported_owner"


def test_verified_academic_dates_and_date_arithmetic(snapshot):
    box = AssistantTools(snapshot)
    assert call(box, "get_academic_dates", {"query": "Reading week"})["available"]
    missing = call(box, "get_academic_dates", {"query": "Final exam"})
    assert missing["available"] is False and missing["dates"] == []
    result = call(box, "calculate_date", {"base_datetime": stamp(22, "17:00"), "next_weekday": 4})
    assert result["result"] == stamp(23, "17:00")
    assert call(box, "calculate_date", {"base_datetime": "2026-10-22"})["ok"] is False


def test_plan_uses_real_planner_calendar_and_remaining_work_without_writes(snapshot, monkeypatch):
    original = deepcopy(snapshot)
    box = AssistantTools(snapshot)
    def reject_database(*_args, **_kwargs):
        raise AssertionError("Tools must never open a database")
    monkeypatch.setattr(sqlite3, "connect", reject_database)
    result = call(box, "generate_study_plan", role="planning")
    assert result["ok"] and result["scheduled_minutes"] == 60
    assert result["saved"] is False
    plan = box.artifacts["plans"][0]
    assert plan["result"]["blocks"][0]["start"] == stamp(21, "10:00")
    inputs = plan["inputs"]
    assert validate_schedule(plan["result"]["blocks"], inputs["tasks"], inputs["slots"], inputs["busy"], inputs["settings"]) == []
    assert snapshot == original


def test_plan_respects_completed_daily_capacity(snapshot):
    snapshot["completed_by_day"] = {"2026-10-21": 320}
    result = call(AssistantTools(snapshot), "generate_study_plan", role="planning")
    assert result["scheduled_minutes"] == 40
    assert result["unscheduled"][0]["remaining_minutes"] == 20


def test_plan_cannot_escape_selected_tasks_or_calendar_coverage(snapshot):
    box = AssistantTools(snapshot)
    assert call(box, "generate_study_plan", {"task_ids": ["other-task"]}, role="planning")["error"]["code"] == "task_out_of_scope"
    snapshot["calendar_coverage"]["time_max"] = stamp(21, "12:00")
    box = AssistantTools(snapshot)
    result = call(box, "generate_study_plan", role="planning")
    assert result["error"]["code"] == "calendar_range_incomplete"
    assert not box.artifacts["plans"]


def test_no_calendar_coverage_is_explicit_not_assumed_free(snapshot):
    snapshot.pop("calendar_coverage")
    snapshot["calendar_events"] = []
    box = AssistantTools(snapshot)
    context = call(box, "get_planning_context", role="planning")
    assert context["calendar_available"] is False
    result = call(box, "generate_study_plan", role="planning")
    assert any("not verified" in warning for warning in result["warnings"])


def test_ui_empty_calendar_coverage_allows_local_plan_with_warning(snapshot):
    snapshot["calendar_coverage"] = {}
    snapshot["calendar_events"] = []
    box = AssistantTools(snapshot)
    result = call(box, "generate_study_plan", role="planning")
    assert result["ok"] and result["scheduled_minutes"] == 60
    assert any("not verified" in warning for warning in result["warnings"])
    assert box.artifacts["plans"][0]["calendar_coverage"] is None


def test_selected_reply_metadata_is_available_without_full_source_manifest(snapshot):
    box = AssistantTools(snapshot)
    source = call(box, "read_source", {"source_id": "email-1"}, role="email")
    assert source["sender"] == "Teacher <teacher@example.invalid>"
    assert "sender" not in call(box, "list_sources", role="email")["sources"][0]


def test_chat_read_preserves_supplied_speaker_and_thread_metadata_without_guessing(snapshot):
    snapshot["sources"][1].update(
        text="I will do it.", sender_id="users/123", sender_name=None,
        sender_type="HUMAN", thread_reply=True,
    )
    result = call(AssistantTools(snapshot), "read_source", {"source_id": "chat-1"}, role="meeting")
    assert result["provider"] == "google_chat"
    assert result["thread_id"] == "thread-1"
    assert result["sender_id"] == "users/123" and result["sender_name"] is None
    assert result["sender_type"] == "HUMAN" and result["thread_reply"] is True


def test_invalid_calendar_time_prevents_usable_plan(snapshot):
    snapshot["calendar_events"][0]["start"] = "2026-10-21T09:00:00"
    box = AssistantTools(snapshot)
    result = call(box, "generate_study_plan", role="planning")
    assert result["block_count"] == 0 and result["validation_errors"]


def test_deadline_cluster_includes_calendar_exams_and_excludes_completed(snapshot):
    snapshot["tasks"] = [
        {"id": "t1", "title": "Outline", "due_at": stamp(21, "17:00"), "estimated_minutes": 30},
        {"id": "t2", "title": "Essay", "due_at": stamp(22, "09:00"), "estimated_minutes": 30},
        {"id": "t3", "title": "Slides", "due_at": stamp(23, "16:00"), "estimated_minutes": 30},
        {"id": "done", "title": "Done", "due_at": stamp(22, "17:00"), "estimated_minutes": 30, "completed_minutes": 30},
        {"id": "unknown", "title": "Date missing", "due_at": None},
    ]
    snapshot["calendar_events"] = [{"id": "exam", "title": "Midterm", "start": stamp(22, "12:00"), "end": stamp(22, "13:00")}]
    result = call(AssistantTools(snapshot), "check_deadline_risks")
    assert result["ok"] and result["window_hours"] == 48
    assert {item["id"] for item in result["clusters"][0]["items"]} == {"t1", "t2", "t3", "exam"}
    assert result["tasks_without_valid_deadlines"] == 1
    assert result["calendar_coverage"] == snapshot["calendar_coverage"]


def test_snapshot_is_frozen_and_output_size_is_bounded(snapshot):
    box = AssistantTools(snapshot)
    snapshot["sources"][0]["text"] = "Changed after approval"
    assert "Please submit" in call(box, "read_source", {"source_id": "email-1"})["text"]
    huge = deepcopy(snapshot)
    huge["academic_dates"] = [{"title": "X" * 30_000}]
    result = call(AssistantTools(huge), "get_academic_dates")
    assert result["error"]["code"] == "output_limit"


def test_no_duplicate_ids_allowed(snapshot):
    snapshot["sources"].append(snapshot["sources"][0])
    with pytest.raises(ValueError, match="distinct"):
        AssistantTools(snapshot)


def test_failed_second_artifact_rolls_back_whole_call_but_preserves_prior_results(snapshot, monkeypatch):
    box = AssistantTools(snapshot)
    assert call(box, "record_task_proposals", {"proposals": [proposal()]})["ok"]
    before = deepcopy(box.artifacts)
    original = box._artifact
    created = []

    def fail_second(category, value):
        if created:
            raise RuntimeError("private handler details must not escape")
        result = original(category, value)
        created.append(result["id"])
        return result

    monkeypatch.setattr(box, "_artifact", fail_second)
    items = [
        {"source_id": "email-1", "category": "needs_action", "reason": "Essay", "citations": [citation()]},
        {"source_id": "chat-1", "category": "needs_action", "reason": "Slides",
         "citations": [citation("chat-1", "Alex will prepare the slides.")]},
    ]
    result = call(box, "record_triage", {"items": items}, role="email")
    assert len(created) == 1 and result["ok"] is False
    assert box.artifacts == before
    assert box.events[-1]["status"] == "error"
    assert "private handler details" not in json.dumps(result)


def test_plan_output_limit_rolls_back_created_plan(snapshot, monkeypatch):
    box = AssistantTools(snapshot)
    assert call(box, "record_task_proposals", {"proposals": [proposal()]})["ok"]
    before = deepcopy(box.artifacts)
    monkeypatch.setattr(agent_tools, "MAX_OUTPUT_CHARS", 80)
    result = call(box, "generate_study_plan", role="planning")
    assert result["error"]["code"] == "output_limit"
    assert box.artifacts == before and not box.artifacts["plans"]


def test_risks_deduplicate_repeated_authoritative_calendar_source_only(snapshot):
    snapshot["tasks"] = []
    event = {"id": "exam", "source_id": "calendar-source-one", "title": "Midterm",
             "start": stamp(22, "12:00"), "end": stamp(22, "13:00")}
    snapshot["calendar_events"] = [event, deepcopy(event)]
    result = call(AssistantTools(snapshot), "check_deadline_risks", {"minimum_items": 2})
    assert result["clusters"] == []
    snapshot["calendar_events"][1]["source_id"] = "calendar-source-two"
    result = call(AssistantTools(snapshot), "check_deadline_risks", {"minimum_items": 2})
    assert len(result["clusters"][0]["items"]) == 2


def test_risks_do_not_merge_same_title_or_bare_event_id(snapshot):
    snapshot["tasks"] = []
    event = {"id": "same-bare-id", "title": "Midterm", "start": stamp(22, "12:00"), "end": stamp(22, "13:00")}
    snapshot["calendar_events"] = [event | {"calendar_id": "A"}, event | {"calendar_id": "B"}]
    result = call(AssistantTools(snapshot), "check_deadline_risks", {"minimum_items": 2})
    assert len(result["clusters"][0]["items"]) == 2
