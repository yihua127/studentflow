"""Placeholder rejection and recoverable reply recording with scripted SDK models.

No live model, mailbox, or database is used. Unknown recipient titles are not
inferred from a style sample: the scripted corrected greeting is neutral.
"""

import pytest
from test_reply_workflow import (
    EMAIL_ID,
    draft_payload,
    final,
    read_reply_steps,
    reply_models,
    selected_snapshot,
    tool_call,
    tool_output,
)

from studentflow import agent_runtime as runtime
from studentflow.agent_tools import AssistantTools
from studentflow.reply_validation import unresolved_placeholders


@pytest.mark.parametrize("marker", [
    "[Surname]", "[assignment]", "[assignment name]", "[deadline]", "[due_date]",
    "[full-name]", "[Your name]", "[Original subject]", "{{recipient_name}}",
    "{{assignment_name}}", "<your name>", "[老师姓名]", "[截止日期]",
])
def test_common_reply_template_markers_are_detected(marker):
    assert unresolved_placeholders(f"Please review {marker}.") == [marker]


@pytest.mark.parametrize("text", [
    "Please see reference [1].", "The evidence is discussed in [Smith2026].",
    "The evidence is discussed in [Smith 2026].", "The interval is [0, 1].",
    "For this expression, [x + y] / 2 is equivalent to (x + y) / 2.",
    "The comparison is x < y and y > z.", "Best regards,\nALEX TAN",
])
def test_citations_math_and_completed_signatures_are_not_template_markers(text):
    assert unresolved_placeholders(text) == []


def office_snapshot():
    snapshot = selected_snapshot()
    snapshot["sources"][0]["sender"] = "course@example.invalid"
    return snapshot


def placeholder_draft(**changes):
    return draft_payload(
        recipient="course@example.invalid",
        body="Dear Professor [Surname],\n\nThank you for the details.\n\nBest regards,\nALEX TAN",
        **changes,
    )


@pytest.mark.parametrize("field", ["subject", "body"])
def test_tool_rejects_unresolved_markers_before_creating_a_draft(field):
    box = AssistantTools(office_snapshot())
    draft = draft_payload(recipient="course@example.invalid", body="Hello,\nThank you.")
    draft[field] = "Re: [assignment name]" if field == "subject" else "Dear Professor [Surname],"
    result = box.invoke("record_reply_draft", draft, role="email")
    assert result["ok"] is False
    assert result["error"]["code"] == "unresolved_placeholders"
    assert "Hello" in result["error"]["message"]
    assert not box.artifacts["reply_drafts"]


def test_sdk_corrects_reported_surname_placeholder_to_a_neutral_greeting():
    corrected = draft_payload(
        recipient="course@example.invalid",
        body="Hello,\n\nThank you for the details.\n\nBest regards,\nALEX TAN",
    )

    def after_rejection(invocation):
        failure = tool_output(invocation, "placeholder-attempt")
        assert failure["ok"] is False
        assert failure["error"]["code"] == "unresolved_placeholders"
        assert "neutral greeting" in failure["error"]["message"]
        return [tool_call("record_reply_draft", corrected, "corrected-draft")]

    scripted = reply_models(read_reply_steps() + [
        [tool_call("record_reply_draft", placeholder_draft(), "placeholder-attempt")],
        after_rejection,
        [final("An editable reply with a neutral greeting is ready for review.")],
    ])
    box = AssistantTools(office_snapshot())
    result = runtime.run_assistant("Draft my reply without guessing a name or title.", box,
                                   model_override=scripted)
    assert result.status == "completed"
    assert len(result.artifacts["reply_drafts"]) == 1
    draft = result.artifacts["reply_drafts"][0]
    assert draft["body"] == corrected["body"]
    assert "Professor" not in draft["body"] and "Chen" not in draft["body"]
    assert not unresolved_placeholders(draft["subject"] + "\n" + draft["body"])
    assert draft["saved"] is False and draft["sent"] is False
    attempts = [event for event in box.events if event["tool"] == "record_reply_draft"]
    assert [event["status"] for event in attempts] == ["error", "ok"]
    assert not any(event.get("status") == "repairing" for event in result.events)


def test_lingering_template_markers_end_partial_without_an_editable_artifact():
    scripted = reply_models(read_reply_steps() + [
        [tool_call("record_reply_draft", placeholder_draft(), "placeholder-attempt")],
        [final("Here is the draft, still addressed to Professor [Surname].")],
        [tool_call("record_reply_draft", placeholder_draft(), "bad-corrective-attempt")],
    ])
    box = AssistantTools(office_snapshot())
    result = runtime.run_assistant("Draft my reply.", box, model_override=scripted)
    assert result.status == "partial"
    assert not result.artifacts["reply_drafts"]
    attempts = [event for event in box.events if event["tool"] == "record_reply_draft"]
    assert len(attempts) == 2 and all(event["status"] == "error" for event in attempts)
    assert "ready to review" not in result.answer


def test_explicit_clarification_for_the_failed_source_clears_pending_correction():
    question = "Which greeting would you like me to use for this sender?"
    scripted = reply_models(read_reply_steps() + [
        [tool_call("record_reply_draft", placeholder_draft(), "placeholder-attempt")],
        [tool_call("request_reply_clarification", {
            "source_id": EMAIL_ID, "questions": [question],
        }, "clarify-greeting")],
        [final(question)],
    ])
    scripted["manager"].steps[-1] = [final(question)]
    box = AssistantTools(office_snapshot())
    result = runtime.run_assistant("Ask me if any important reply detail is missing.", box,
                                   model_override=scripted)
    assert result.status == "completed" and result.answer == question
    assert not result.artifacts["reply_drafts"]
    assert not any(event.get("status") == "repairing" for event in result.events)


def test_clarification_for_a_different_source_does_not_clear_the_failed_reply():
    snapshot = office_snapshot()
    other = dict(snapshot["sources"][0], source_id="different-email", title="Other message")
    snapshot["sources"].append(other)
    scripted = reply_models(read_reply_steps() + [
        [tool_call("record_reply_draft", placeholder_draft(), "placeholder-attempt")],
        [tool_call("request_reply_clarification", {
            "source_id": "different-email", "questions": ["How should I reply to the other message?"],
        }, "clarify-other-source")],
        [final("Please confirm how to reply to the other message.")],
    ])
    box = AssistantTools(snapshot)
    result = runtime.run_assistant("Draft my reply.", box, model_override=scripted)
    assert result.status == "partial"
    assert not result.artifacts["reply_drafts"]
    assert "unresolved template" in result.answer
