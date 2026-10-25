"""Real SDK meeting completion contracts with scripted, offline model decisions.

These tests verify evidence delivery, validation and bounded artifact recording;
they are not a live model or Google Chat quality evaluation.
"""

import json
import time
from copy import deepcopy

import pytest
from test_reply_workflow import ScriptedReplyModel, final, tool_call, tool_output

from studentflow import agent_runtime as runtime
from studentflow.agent_tools import AssistantTools

QUOTES = [
    "Alex will prepare the project outline.",
    "We agreed to use the campus survey dataset.",
    "Could we meet on Friday? The time is not confirmed.",
]
REQUEST = (
    "Summarize the selected discussion in English. List confirmed decisions, action items, "
    "open questions and missing information. Do not turn suggestions into confirmed plans."
)


def snapshot():
    return {"sources": [{"source_id": f"chat-{index + 1}", "provider": "google_chat",
                         "text": text, "title": "Project discussion"}
                        for index, text in enumerate(QUOTES)]}


def summary_payload(**changes):
    return {
        "title": "Project discussion", "summary": "The group chose a dataset and assigned the outline.",
        "citations": [{"source_id": f"chat-{index + 1}", "quote": quote}
                      for index, quote in enumerate(QUOTES)],
        "decisions": [{"text": "Use the campus survey dataset.",
                       "citations": [{"source_id": "chat-2", "quote": QUOTES[1]}]}],
        "action_items": [{"title": "Prepare the project outline", "owner": "Alex", "due_at": None,
                          "citations": [{"source_id": "chat-1", "quote": QUOTES[0]}]}],
        "agenda": ["Review the outline and confirm a meeting time."],
        "follow_up": "Please confirm whether Friday works and suggest a time.",
        "open_questions": ["Will the group meet on Friday?"],
        "missing_information": ["The outline deadline and meeting time are not specified."],
        **changes,
    }


def read_steps(source_ids=("chat-1", "chat-2", "chat-3")):
    return [
        [tool_call("list_sources", {"offset": 0, "limit": 20}, "list-discussion")],
        *[[tool_call("read_source", {"source_id": sid, "offset": 0, "limit": 8000},
                     "read-" + sid)] for sid in source_ids],
    ]


def models(meeting_steps, *, operation="summary"):
    result = {role: ScriptedReplyModel([]) for role in runtime.ROLES}
    arguments = {"input": "Review the selected discussion.", "operation": operation}
    if operation is None:
        arguments.pop("operation")
    result["manager"] = ScriptedReplyModel([
        [tool_call("consult_meeting", arguments, "delegate-meeting")],
        [final("The discussion result is ready for review.")],
    ])
    result["meeting"] = ScriptedReplyModel(meeting_steps)
    return result


def clarification(questions=None, source_ids=None, *, call_id="clarify-meeting"):
    return tool_call("request_meeting_clarification", {
        "source_ids": ["chat-3"] if source_ids is None else source_ids,
        "questions": questions or ["Should Friday remain an open question until the group confirms?"],
    }, call_id)


def validation_events(result):
    return [event for event in result.events if event.get("type") == "validation"]


def test_normal_meeting_summary_records_once_and_keeps_uncertainty_without_repair():
    data = snapshot()
    before = deepcopy(data)
    scripted = models(read_steps() + [
        [tool_call("record_meeting_summary", summary_payload(), "record-summary")],
        [final("The discussion summary is ready for review.")],
    ])
    box = AssistantTools(data)
    result = runtime.run_assistant(REQUEST, box, model_override=scripted)
    assert result.status == "completed"
    assert not validation_events(result)
    assert len(result.artifacts["meeting_summaries"]) == 1
    recorded = result.artifacts["meeting_summaries"][0]
    assert recorded["open_questions"] == summary_payload()["open_questions"]
    assert recorded["missing_information"] == summary_payload()["missing_information"]
    assert recorded["action_items"][0]["due_at"] is None
    assert recorded["saved"] is False and recorded["requires_confirmation"] is True
    assert data == before
    output = tool_output(scripted["manager"].calls[-1], "delegate-meeting")
    assert output["artifact_id"] == recorded["id"]
    assert output["tasks_created"] is False and output["sent"] is False
    assert "summary" not in output  # The UI owns the complete structured presentation.
    assert not any(scripted[role].calls for role in ("email", "task", "planning"))


def test_list_three_reads_then_prose_gets_one_grounded_recording_turn():
    prose = "## Discussion summary\nThe group chose a dataset; Friday is only proposed."

    def record_outcome(invocation):
        assert invocation["model_settings"].tool_choice == "required"
        assert {tool.name for tool in invocation["tools"]} == {
            "record_meeting_summary", "request_meeting_clarification",
        }
        for index, quote in enumerate(QUOTES, 1):
            assert tool_output(invocation, f"read-chat-{index}")["text"] == quote
        assert json.dumps(prose) in json.dumps(invocation["input"])
        return [tool_call("record_meeting_summary", summary_payload(), "repaired-summary")]

    scripted = models(read_steps() + [[final(prose)], record_outcome])
    box = AssistantTools(snapshot())
    result = runtime.run_assistant(REQUEST, box, model_override=scripted)
    assert result.status == "completed"
    assert len(result.artifacts["meeting_summaries"]) == 1
    assert [item["status"] for item in validation_events(result)] == ["repairing", "ok"]
    assert [item["tool"] for item in box.events].count("record_meeting_summary") == 1
    assert len(scripted["meeting"].calls) == 6  # list + three reads + prose + correction
    assert result.usage["model_calls"] == 8
    assert result.usage["tool_calls"] == 6  # delegate + list + three reads + record
    assert result.usage["usage_complete"] is True


@pytest.mark.parametrize("invalid", [
    {"citations": [{"source_id": "chat-1", "quote": "An invented decision."}]},
    {"action_items": [{"title": "Prepare the outline", "owner": "Unknown Person", "due_at": None,
                       "citations": [{"source_id": "chat-1", "quote": QUOTES[0]}]}]},
])
def test_twice_rejected_correction_stays_partial_without_a_third_attempt(invalid):
    scripted = models(read_steps() + [
        [final("A prose report is ready.")],
        [tool_call("record_meeting_summary", summary_payload(**invalid), "invalid-summary")],
        [tool_call("record_meeting_summary", summary_payload(**invalid), "still-invalid-summary")],
    ])
    box = AssistantTools(snapshot())
    result = runtime.run_assistant(REQUEST, box, model_override=scripted)
    assert result.status == "partial"
    assert not result.artifacts["meeting_summaries"]
    assert not result.artifacts["meeting_clarifications"]
    assert [item["tool"] for item in box.events].count("record_meeting_summary") == 2
    assert len(scripted["meeting"].calls) == 7
    assert any(item.get("error_code") == "missing_meeting_summary" for item in result.events)
    assert "ready for review" not in result.answer
    assert "model settings" not in result.answer
    assert "validation" in result.answer


@pytest.mark.parametrize("kind", ["none", "manifest-only", "empty-source", "empty-late-page"])
def test_no_nonblank_evidence_read_cannot_trigger_recording(kind):
    data = snapshot()
    steps = []
    if kind == "manifest-only":
        steps = read_steps(())
    elif kind == "empty-source":
        data["sources"] = [{"source_id": "chat-1", "provider": "google_chat", "text": "   "}]
        steps = read_steps(("chat-1",))
    elif kind == "empty-late-page":
        steps = [[tool_call("read_source", {"source_id": "chat-1", "offset": 1000, "limit": 8000},
                            "empty-read")]]
    scripted = models(steps + [[final("The summary was recorded.")]])
    box = AssistantTools(data)
    result = runtime.run_assistant(REQUEST, box, model_override=scripted)
    assert result.status in {"partial", "failed"}
    assert not result.artifacts["meeting_summaries"]
    assert not any(item["status"] == "repairing" for item in validation_events(result))
    assert len(scripted["meeting"].calls) == len(steps) + 1
    assert "The summary was recorded" not in result.answer


def test_no_sources_can_produce_an_explicit_clarification_without_summary_or_repair():
    scripted = models([
        [clarification(["Which discussion messages or meeting notes should I summarize?"], [])],
        [final("Select the discussion messages or notes first.")],
    ])
    result = runtime.run_assistant(REQUEST, AssistantTools({}), model_override=scripted)
    assert result.status == "completed"
    assert not result.artifacts["meeting_summaries"]
    assert not validation_events(result)
    question = result.artifacts["meeting_clarifications"][0]
    assert question["source_ids"] == [] and question["needs_clarification"] is True
    assert question["summary_recorded"] is False
    assert question["tasks_created"] is False and question["sent"] is False


def test_no_sources_and_a_prose_success_claim_cannot_be_completed():
    scripted = models([[final("A complete summary is ready.")]])
    result = runtime.run_assistant(REQUEST, AssistantTools({}), model_override=scripted)
    assert result.status == "failed"
    assert not result.artifacts["meeting_summaries"]
    assert "Select the relevant readable messages or notes" in result.answer
    assert len(scripted["meeting"].calls) == 1


@pytest.mark.parametrize("source_ids", [(), ("chat-1",)])
def test_normal_record_without_complete_source_reads_is_explicitly_partial(source_ids):
    scripted = models(read_steps(source_ids) + [
        [tool_call("record_meeting_summary", summary_payload(), "premature-summary")],
        [final("The whole discussion has been summarized.")],
    ])
    result = runtime.run_assistant(REQUEST, AssistantTools(snapshot()), model_override=scripted)
    assert result.status == "partial"
    assert len(result.artifacts["meeting_summaries"]) == 1
    assert "may be incomplete" in result.answer
    assert any(item.get("error_code") == "incomplete_meeting_evidence" for item in result.events)
    assert not any(item["status"] == "repairing" for item in validation_events(result))


@pytest.mark.parametrize("clarification_first", [True, False])
@pytest.mark.parametrize("source_ids", [(), ("chat-1",), ("chat-1", "chat-2", "chat-3")])
def test_clarification_order_cannot_hide_a_summary_with_incomplete_evidence(
    clarification_first, source_ids,
):
    outcomes = [[clarification()],
                [tool_call("record_meeting_summary", summary_payload(), "summary-with-question")]]
    if not clarification_first:
        outcomes.reverse()
    scripted = models(read_steps(source_ids) + outcomes + [[final("The result is ready.")]])
    result = runtime.run_assistant(REQUEST, AssistantTools(snapshot()), model_override=scripted)
    complete = len(source_ids) == 3
    assert result.status == ("completed" if complete else "partial")
    assert len(result.artifacts["meeting_summaries"]) == 1
    assert len(result.artifacts["meeting_clarifications"]) == 1
    assert not any(item["status"] == "repairing" for item in validation_events(result))
    if complete:
        assert not validation_events(result)
    else:
        assert "may be incomplete" in result.answer
        assert any(item.get("error_code") == "incomplete_meeting_evidence" for item in result.events)
        outcome = tool_output(scripted["manager"].calls[-1], "delegate-meeting")
        assert outcome["ok"] is False
        assert outcome["artifact_ids"] == [result.artifacts["meeting_summaries"][0]["id"]]


def test_original_ask_first_request_is_forwarded_and_clarification_skips_repair():
    request = "Before summarizing, ask whether I want the tentative Friday plan included."

    def ask_first(invocation):
        assert request in json.dumps(invocation["input"])
        assert "Review the selected discussion." in json.dumps(invocation["input"])
        return [clarification()]

    scripted = models([ask_first, [final("Please confirm the intended scope.")]])
    result = runtime.run_assistant(request, AssistantTools(snapshot()), model_override=scripted)
    assert result.status == "completed"
    assert not result.artifacts["meeting_summaries"]
    assert len(result.artifacts["meeting_clarifications"]) == 1
    assert not validation_events(result)


def test_corrective_turn_can_clarify_instead_of_forcing_a_summary():
    def ask_first(invocation):
        assert invocation["model_settings"].tool_choice == "required"
        assert {tool.name for tool in invocation["tools"]} == {
            "record_meeting_summary", "request_meeting_clarification",
        }
        return [clarification()]

    scripted = models(read_steps() + [[final("Should I include the tentative plan?")], ask_first])
    result = runtime.run_assistant("Read the discussion, but ask before drafting its summary.",
                                   AssistantTools(snapshot()), model_override=scripted)
    assert result.status == "completed"
    assert not result.artifacts["meeting_summaries"]
    assert len(result.artifacts["meeting_clarifications"]) == 1
    assert [item["status"] for item in validation_events(result)] == ["repairing", "ok"]


@pytest.mark.parametrize("partial", ["one-of-three", "late-page", "page-gap"])
def test_incomplete_read_coverage_only_allows_clarification(partial):
    data = snapshot()
    steps = read_steps(("chat-1",))
    if partial != "one-of-three":
        data["sources"] = data["sources"][:1]
        if partial == "late-page":
            offsets = [(10, 8000)]
        else:
            offsets = [(0, 5), (10, 8000)]
        steps = [[tool_call("read_source", {"source_id": "chat-1", "offset": offset, "limit": limit},
                            f"page-{index}")] for index, (offset, limit) in enumerate(offsets)]

    def clarify_coverage(invocation):
        assert {tool.name for tool in invocation["tools"]} == {"request_meeting_clarification"}
        assert "has not been fully read" in invocation["input"][-1]["content"]
        return [clarification(["Some selected text has not been read. Could you narrow the selection?"],
                              ["chat-1"])]

    scripted = models(steps + [[final("Here is my partial report.")], clarify_coverage])
    result = runtime.run_assistant(REQUEST, AssistantTools(data), model_override=scripted)
    assert result.status == "completed"
    assert not result.artifacts["meeting_summaries"]
    assert len(result.artifacts["meeting_clarifications"]) == 1


def test_contiguous_paginated_attachment_reads_allow_a_supported_summary():
    text = QUOTES[0] + " " + QUOTES[1]
    split = 20
    data = {"sources": [{"source_id": "notes", "provider": "attachment", "text": text},
                        {"source_id": "blank", "provider": "google_chat", "text": "  "}]}
    payload = {"title": "Meeting notes", "summary": "The group selected a dataset.",
               "citations": [{"source_id": "notes", "quote": QUOTES[1]}]}

    def record(invocation):
        assert "record_meeting_summary" in {tool.name for tool in invocation["tools"]}
        return [tool_call("record_meeting_summary", payload, "record-notes")]

    scripted = models([
        [tool_call("read_source", {"source_id": "notes", "offset": 0, "limit": split}, "page-1")],
        [tool_call("read_source", {"source_id": "notes", "offset": split, "limit": 8000}, "page-2")],
        [final("A notes summary is ready.")], record,
    ])
    result = runtime.run_assistant(REQUEST, AssistantTools(data), model_override=scripted)
    assert result.status == "completed"
    assert len(result.artifacts["meeting_summaries"]) == 1
    assert result.artifacts["meeting_summaries"][0]["open_questions"] == []
    assert result.artifacts["meeting_summaries"][0]["missing_information"] == []


@pytest.mark.parametrize("operation", ["other", None])
def test_general_advice_and_legacy_delegation_do_not_require_a_summary(operation):
    scripted = models([[final("A useful agenda starts by reviewing unresolved decisions.")]],
                      operation=operation)
    result = runtime.run_assistant("How should a group discussion be structured?", AssistantTools({}),
                                   model_override=scripted)
    assert result.status == "completed"
    assert not any(result.artifacts.values())
    assert not validation_events(result)


@pytest.mark.parametrize("limits", [
    runtime.RunLimits(max_model_calls=6), runtime.RunLimits(max_tool_calls=5),
])
def test_corrective_recording_shares_original_model_and_tool_budgets(limits):
    scripted = models(read_steps() + [
        [final("A prose report is ready.")],
        [tool_call("record_meeting_summary", summary_payload(), "budgeted-record")],
    ])
    box = AssistantTools(snapshot())
    result = runtime.run_assistant(REQUEST, box, model_override=scripted, limits=limits)
    assert result.status == "partial"
    assert not result.artifacts["meeting_summaries"]
    assert result.usage["model_calls"] <= limits.max_model_calls
    assert result.usage["tool_calls"] <= limits.max_tool_calls
    assert not any(item["tool"] == "record_meeting_summary" for item in box.events)


def test_corrective_recording_cannot_reset_elapsed_time_budget():
    def delayed_record(_invocation):
        time.sleep(1.1)
        return [tool_call("record_meeting_summary", summary_payload(), "late-record")]

    scripted = models(read_steps() + [[final("A prose report is ready.")], delayed_record])
    result = runtime.run_assistant(REQUEST, AssistantTools(snapshot()), model_override=scripted,
                                   limits=runtime.RunLimits(max_total_seconds=1.0))
    assert result.status == "partial"
    assert not result.artifacts["meeting_summaries"]
    assert any(item.get("error_code") == "time_limit" for item in result.events)


def test_successful_duplicate_record_is_valid_for_current_invocation_without_repair():
    box = AssistantTools(snapshot())
    previous = box.invoke("record_meeting_summary", summary_payload(), role="meeting")
    assert previous["ok"]
    scripted = models(read_steps() + [
        [tool_call("record_meeting_summary", summary_payload(), "record-same-summary")],
        [final("The recorded result is ready.")],
    ])
    result = runtime.run_assistant(REQUEST, box, model_override=scripted)
    assert result.status == "completed"
    assert len(result.artifacts["meeting_summaries"]) == 1
    assert result.artifacts["meeting_summaries"][0]["id"] == previous["artifact_id"]
    assert not validation_events(result)


def test_prior_record_alone_does_not_satisfy_a_new_summary_invocation():
    box = AssistantTools(snapshot())
    assert box.invoke("record_meeting_summary", summary_payload(), role="meeting")["ok"]
    scripted = models([[final("I recorded the summary.")]])
    result = runtime.run_assistant(REQUEST, box, model_override=scripted)
    assert result.status == "failed"
    assert len(result.artifacts["meeting_summaries"]) == 1  # Earlier validated work is retained.
    assert any(item.get("error_code") == "missing_meeting_summary" for item in result.events)
    assert "I recorded the summary" not in result.answer


def test_correction_ignored_as_prose_does_not_trigger_an_unbounded_retry():
    scripted = models(read_steps() + [[final("Here is a prose summary.")],
                                     [final("I will keep responding with prose.")]])
    result = runtime.run_assistant(REQUEST, AssistantTools(snapshot()), model_override=scripted)
    assert result.status == "partial"
    assert not result.artifacts["meeting_summaries"]
    assert len(scripted["meeting"].calls) == 6


@pytest.mark.parametrize("arguments", [
    {"questions": ["  "]}, {"questions": []},
    {"source_ids": ["unselected"], "questions": ["Which plan is confirmed?"]},
])
def test_meeting_clarification_validates_selected_scope_and_specific_questions(arguments):
    box = AssistantTools(snapshot())
    result = box.invoke("request_meeting_clarification", arguments, role="meeting")
    assert result["ok"] is False
    assert not any(box.artifacts.values())


@pytest.mark.parametrize("role", ["email", "task", "planning"])
def test_meeting_clarification_is_not_exposed_to_other_specialists(role):
    box = AssistantTools(snapshot())
    result = box.invoke("request_meeting_clarification", {"questions": ["Which discussion?"]}, role=role)
    assert result["error"]["code"] == "tool_not_allowed"
    assert not any(box.artifacts.values())


@pytest.mark.parametrize("field", ["open_questions", "missing_information"])
def test_summary_rejects_blank_question_or_missing_information_entries(field):
    box = AssistantTools(snapshot())
    result = box.invoke("record_meeting_summary", summary_payload(**{field: ["   "]}), role="meeting")
    assert result["error"]["code"] == "invalid_meeting_detail"
    assert not box.artifacts["meeting_summaries"]


def rejected_payload(code):
    if code == "invalid_citation":
        return summary_payload(citations=[{"source_id": "chat-1", "quote": "An invented quotation."}])
    if code == "invalid_tool_arguments":
        return summary_payload(action_items=None)
    actions = summary_payload()["action_items"]
    if code == "unsupported_owner":
        actions[0]["owner"] = "Sam"
    elif code == "invalid_datetime":
        actions[0]["due_at"] = "2026-10-30"
    return summary_payload(action_items=actions)


@pytest.mark.parametrize("code", [
    "invalid_citation", "unsupported_owner", "invalid_datetime", "invalid_tool_arguments",
])
def test_first_rejected_record_gets_one_validation_guided_correction(code):
    def correct(invocation):
        assert invocation["model_settings"].tool_choice == "required"
        output = tool_output(invocation, "rejected-record")
        assert output["ok"] is False
        assert output.get("error_code", output.get("error", {}).get("code")) == code
        assert {tool.name for tool in invocation["tools"]} == {
            "record_meeting_summary", "request_meeting_clarification",
        }
        return [tool_call("record_meeting_summary", summary_payload(), "corrected-record")]

    scripted = models(read_steps() + [
        [final("A prose summary is ready.")],
        [tool_call("record_meeting_summary", rejected_payload(code), "rejected-record")],
        correct,
    ])
    box = AssistantTools(snapshot())
    result = runtime.run_assistant(REQUEST, box, model_override=scripted)
    assert result.status == "completed"
    assert len(result.artifacts["meeting_summaries"]) == 1
    assert result.artifacts["meeting_summaries"][0]["action_items"][0]["due_at"] is None
    assert result.usage["model_calls"] == 9
    assert result.usage["tool_calls"] == 7
    assert result.usage["usage_complete"] is True
    assert len(scripted["meeting"].calls) == 7
    errors = [event for event in result.events if event["status"] == "error"]
    assert len(errors) == 1 and errors[0]["error_code"] == code
    assert [event["status"] for event in validation_events(result)] == [
        "repairing", "recovered", "ok",
    ]


def test_normal_loop_validation_correction_no_longer_poisoned_by_old_tool_error():
    scripted = models(read_steps() + [
        [tool_call("record_meeting_summary", rejected_payload("invalid_citation"), "bad-record")],
        [tool_call("record_meeting_summary", summary_payload(), "good-record")],
        [final("The summary is ready.")],
    ])
    result = runtime.run_assistant(REQUEST, AssistantTools(snapshot()), model_override=scripted)
    assert result.status == "completed"
    assert len(result.artifacts["meeting_summaries"]) == 1
    assert [event["status"] for event in validation_events(result)] == ["recovered"]


def test_rejected_record_may_resolve_to_clarification_without_inventing_facts():
    scripted = models(read_steps() + [
        [final("A report is ready.")],
        [tool_call("record_meeting_summary", rejected_payload("invalid_datetime"), "bad-date")],
        [clarification(["What is the outline deadline, including its time and timezone?"],
                       source_ids=["chat-1"])],
    ])
    result = runtime.run_assistant(REQUEST, AssistantTools(snapshot()), model_override=scripted)
    assert result.status == "completed"
    assert not result.artifacts["meeting_summaries"]
    assert len(result.artifacts["meeting_clarifications"]) == 1
    assert validation_events(result)[-1]["tool"] == "request_meeting_clarification"


def test_corrected_meeting_record_does_not_clear_unrelated_read_failure():
    scripted = models([
        [tool_call("read_source", {"source_id": "not-selected"}, "bad-read")],
        *read_steps(),
        [tool_call("record_meeting_summary", rejected_payload("invalid_citation"), "bad-record")],
        [tool_call("record_meeting_summary", summary_payload(), "good-record")],
        [final("The corrected summary is ready.")],
    ])
    result = runtime.run_assistant(REQUEST, AssistantTools(snapshot()), model_override=scripted)
    assert result.status == "partial"
    assert len(result.artifacts["meeting_summaries"]) == 1
    assert any(event.get("tool") == "read_source" and event["status"] == "error"
               for event in result.events)
    assert [event["status"] for event in validation_events(result)] == ["recovered"]


def test_later_delegation_success_does_not_clear_earlier_unresolved_validation():
    scripted = models([
        [tool_call("record_meeting_summary", rejected_payload("invalid_citation"), "first-bad")],
        [final("I could not record the first result.")],
        *read_steps(),
        [tool_call("record_meeting_summary", summary_payload(), "second-good")],
        [final("The second result is ready.")],
    ])
    scripted["manager"] = ScriptedReplyModel([
        [tool_call("consult_meeting", {"input": "First summary", "operation": "summary"}, "first")],
        [tool_call("consult_meeting", {"input": "Second summary", "operation": "summary"}, "second")],
        [final("The second result is ready.")],
    ])
    result = runtime.run_assistant(REQUEST, AssistantTools(snapshot()), model_override=scripted)
    assert result.status == "partial"
    assert len(result.artifacts["meeting_summaries"]) == 1
    assert "quotation" in result.answer
    assert not any(event["status"] == "recovered" for event in validation_events(result))


@pytest.mark.parametrize("limits,code", [
    (runtime.RunLimits(max_model_calls=7), "model_call_limit"),
    (runtime.RunLimits(max_tool_calls=6), "tool_call_limit"),
])
def test_validation_retry_cannot_reset_the_original_shared_budget(limits, code):
    scripted = models(read_steps() + [
        [final("A prose report is ready.")],
        [tool_call("record_meeting_summary", rejected_payload("invalid_citation"), "bad-record")],
        [tool_call("record_meeting_summary", summary_payload(), "good-record")],
    ])
    result = runtime.run_assistant(REQUEST, AssistantTools(snapshot()), model_override=scripted,
                                   limits=limits)
    assert result.status == "partial"
    assert not result.artifacts["meeting_summaries"]
    assert result.usage["model_calls"] <= limits.max_model_calls
    assert result.usage["tool_calls"] <= limits.max_tool_calls
    assert any(event.get("error_code") == code for event in result.events)


def test_corrected_record_still_requires_complete_selected_evidence():
    scripted = models(read_steps(("chat-1",)) + [
        [tool_call("record_meeting_summary", rejected_payload("invalid_citation"), "bad-record")],
        [tool_call("record_meeting_summary", summary_payload(), "good-record")],
        [final("The corrected summary is ready.")],
    ])
    result = runtime.run_assistant(REQUEST, AssistantTools(snapshot()), model_override=scripted)
    assert result.status == "partial"
    assert len(result.artifacts["meeting_summaries"]) == 1
    assert any(event.get("error_code") == "incomplete_meeting_evidence" for event in result.events)
    assert not any(event["status"] == "recovered" for event in validation_events(result))


def test_malformed_record_json_has_safe_feedback_and_bounded_correction():
    malformed = tool_call("record_meeting_summary", {}, "broken-json")
    malformed.arguments = '{"title":'

    def correct(invocation):
        error = tool_output(invocation, "broken-json")
        assert error == {"ok": False, "error_code": "invalid_tool_arguments"}
        return [tool_call("record_meeting_summary", summary_payload(), "correct-json")]

    scripted = models(read_steps() + [[final("A report is ready.")], [malformed], correct])
    result = runtime.run_assistant(REQUEST, AssistantTools(snapshot()), model_override=scripted)
    assert result.status == "completed"
    assert len(result.artifacts["meeting_summaries"]) == 1
    assert len(scripted["meeting"].calls) == 7
    assert any(event.get("error_code") == "invalid_tool_arguments" for event in result.events)


def test_unexpected_record_failure_does_not_receive_validation_retry(monkeypatch):
    box = AssistantTools(snapshot())
    original = box.invoke

    def fail_record(name, args, role):
        if name == "record_meeting_summary":
            return {"ok": False, "error": {"code": "tool_failed", "message": "Synthetic failure"}}
        return original(name, args, role)

    monkeypatch.setattr(box, "invoke", fail_record)
    scripted = models(read_steps() + [
        [final("A report is ready.")],
        [tool_call("record_meeting_summary", summary_payload(), "failed-record")],
    ])
    result = runtime.run_assistant(REQUEST, box, model_override=scripted)
    assert result.status == "partial"
    assert not result.artifacts["meeting_summaries"]
    assert len(scripted["meeting"].calls) == 6
    assert any(event.get("error_code") == "tool_failed" for event in result.events)
    assert "Synthetic failure" not in json.dumps(result.events)


@pytest.mark.parametrize("outcome_kind", ["summary", "clarification"])
@pytest.mark.parametrize("failure_after_outcome", [True, False])
def test_only_validation_errors_before_a_recorded_outcome_are_recovered(
    outcome_kind, failure_after_outcome,
):
    recorded = (tool_call("record_meeting_summary", summary_payload(), "valid-outcome")
                if outcome_kind == "summary" else clarification(call_id="valid-outcome"))
    rejected = tool_call("record_meeting_summary", rejected_payload("invalid_citation"), "bad-revision")
    steps = [recorded, rejected] if failure_after_outcome else [rejected, recorded]
    scripted = models(read_steps() + [[step] for step in steps] + [
        [final("The revised summary is ready.")],
    ])
    result = runtime.run_assistant(REQUEST, AssistantTools(snapshot()), model_override=scripted)
    assert result.status == ("partial" if failure_after_outcome else "completed")
    category = "meeting_summaries" if outcome_kind == "summary" else "meeting_clarifications"
    assert len(result.artifacts[category]) == 1
    outcome = tool_output(scripted["manager"].calls[-1], "delegate-meeting")
    assert outcome["ok"] is not failure_after_outcome
    recovered = [event for event in validation_events(result) if event["status"] == "recovered"]
    assert bool(recovered) is not failure_after_outcome
    if failure_after_outcome:
        assert outcome["error_code"] == "invalid_citation"
        assert outcome["artifact_ids"] == [result.artifacts[category][0]["id"]]
        assert "revised summary is ready" not in result.answer


def test_failed_revision_after_a_recovered_record_still_keeps_run_partial():
    scripted = models(read_steps() + [
        [tool_call("record_meeting_summary", rejected_payload("invalid_citation"), "first-bad")],
        [tool_call("record_meeting_summary", summary_payload(), "corrected-record")],
        [tool_call("record_meeting_summary", rejected_payload("unsupported_owner"), "last-bad")],
        [final("The revised summary is ready.")],
    ])
    result = runtime.run_assistant(REQUEST, AssistantTools(snapshot()), model_override=scripted)
    assert result.status == "partial"
    assert len(result.artifacts["meeting_summaries"]) == 1
    assert "owner" in result.answer and "quotation did not" not in result.answer
    assert [event["status"] for event in validation_events(result)] == ["recovered"]
    outcome = tool_output(scripted["manager"].calls[-1], "delegate-meeting")
    assert outcome["ok"] is False and outcome["error_code"] == "unsupported_owner"


def test_later_failed_record_in_same_repair_batch_is_not_hidden_by_first_success():
    scripted = models(read_steps() + [
        [final("A report is ready.")],
        [tool_call("record_meeting_summary", summary_payload(), "first-success"),
         tool_call("record_meeting_summary", rejected_payload("invalid_citation"), "later-failure")],
    ])
    result = runtime.run_assistant(REQUEST, AssistantTools(snapshot()), model_override=scripted)
    assert result.status == "partial"
    assert len(result.artifacts["meeting_summaries"]) == 1
    assert not any(event["status"] in {"recovered", "ok"} for event in validation_events(result))
    outcome = tool_output(scripted["manager"].calls[-1], "delegate-meeting")
    assert outcome["ok"] is False and outcome["error_code"] == "invalid_citation"
