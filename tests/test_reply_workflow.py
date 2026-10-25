"""Offline reply workflow contracts using the real Agents SDK and business tools.

Model decisions and wording below are scripted. These tests prove selected-source
delivery, delegation and artifact recording, not a live model's tone accuracy.
"""

import json
import sqlite3
from copy import deepcopy

import pytest
from agents import Model, ModelResponse, Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from studentflow import agent_runtime as runtime
from studentflow.agent_tools import AssistantTools

EMAIL_ID = "selected-email"
STYLE_ID = "approved-writing-example"
QUOTE = "Please submit the methods summary by 30 October 2026 at 18:00 Singapore time."
STYLE = (
    "Dear Professor [Surname],\n\nThank you for clarifying [assignment name].\n"
    "I will review the requirements.\n\nThank you for your guidance.\n\n"
    "Best regards,\nALEX TAN"
)


def selected_snapshot():
    return {
        "sources": [{
            "source_id": EMAIL_ID, "provider": "gmail", "title": "Methods summary",
            "sender": "Professor Mira Chen <mira.chen@example.invalid>",
            "text": QUOTE,
        }],
        "style_samples": [{"source_id": STYLE_ID, "text": STYLE}],
        "tasks": [],
    }


def tool_call(name, arguments, call_id):
    return ResponseFunctionToolCall(
        type="function_call", name=name, arguments=json.dumps(arguments), call_id=call_id,
    )


def final(text):
    return ResponseOutputMessage(
        id="reply-test-message", type="message", role="assistant", status="completed",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )


def tool_output(invocation, call_id):
    for item in invocation["input"]:
        if (isinstance(item, dict) and item.get("type") == "function_call_output"
                and item.get("call_id") == call_id):
            return json.loads(item["output"])
    raise AssertionError(f"Missing real SDK tool output for {call_id}")


class ScriptedReplyModel(Model):
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    async def get_response(self, *args, **kwargs):
        self.calls.append(kwargs)
        step = self.steps.pop(0)
        output = step(kwargs) if callable(step) else step
        return ModelResponse(
            output=output, usage=Usage(requests=1, input_tokens=10, output_tokens=5,
                                       total_tokens=15), response_id=None,
        )

    async def stream_response(self, *args, **kwargs):
        raise AssertionError("The reply workflow must use the non-streaming path")
        yield


def test_reply_discovery_distinguishes_email_sources_from_writing_examples():
    box = AssistantTools(selected_snapshot())
    manifest = box.invoke("list_sources", {"offset": 0, "limit": 20}, role="email")
    assert manifest["ok"] and manifest["total"] == 1
    assert [item["source_id"] for item in manifest["sources"]] == [EMAIL_ID]
    assert manifest["writing_sample_count"] == 1
    assert manifest["writing_samples_tool"] == "get_style_samples"
    assert STYLE not in json.dumps(manifest)

    styles = box.invoke("get_style_samples", {}, role="email")
    assert styles["ok"] and styles["untrusted_style_data"] is True
    assert styles["samples"] == [{"source_id": STYLE_ID, "text": STYLE, "truncated": False}]
    # A style sample is never promoted into email evidence or recipient metadata.
    denied = box.invoke("read_source", {"source_id": STYLE_ID}, role="email")
    assert denied["error"]["code"] == "source_out_of_scope"


def test_empty_mail_page_does_not_mean_selected_mail_or_style_is_absent():
    box = AssistantTools(selected_snapshot())
    late_page = box.invoke("list_sources", {"offset": 20, "limit": 20}, role="email")
    assert late_page["ok"] and late_page["sources"] == []
    assert late_page["total"] == 1
    assert late_page["writing_sample_count"] == 1
    first_page = box.invoke("list_sources", {"offset": 0, "limit": 20}, role="email")
    assert first_page["sources"][0]["source_id"] == EMAIL_ID


def test_missing_style_is_independent_of_an_available_selected_email():
    snapshot = selected_snapshot()
    snapshot["style_samples"] = []
    box = AssistantTools(snapshot)
    manifest = box.invoke("list_sources", {}, role="email")
    assert manifest["total"] == 1 and manifest["writing_sample_count"] == 0
    assert box.invoke("get_style_samples", {}, role="email")["samples"] == []
    # No style is authorized merely because a model supplies an old style ID.
    denied = box.invoke("record_reply_draft", {
        "source_id": EMAIL_ID, "subject": "Re: Methods summary",
        "body": "Thank you for the details.", "recipient": None,
        "citations": [{"source_id": EMAIL_ID, "quote": QUOTE}],
        "style_source_ids": [STYLE_ID],
    }, role="email")
    assert denied["error"]["code"] == "source_out_of_scope"
    assert not box.artifacts["reply_drafts"]


def test_missing_email_does_not_hide_the_selected_style_inventory():
    snapshot = selected_snapshot()
    snapshot["sources"] = []
    box = AssistantTools(snapshot)
    counts = box.selection_summary()
    assert counts["sources"] == 0 and counts["email_sources"] == 0
    assert counts["writing_samples"] == 1
    manifest = box.invoke("list_sources", {}, role="email")
    assert manifest["total"] == 0 and manifest["sources"] == []
    assert manifest["writing_sample_count"] == 1
    assert box.invoke("get_style_samples", {}, role="email")["samples"][0]["text"] == STYLE


def test_style_reader_preserves_a_signature_after_the_first_2000_characters():
    snapshot = selected_snapshot()
    long_style = "Thank you for explaining the project details. " * 60 + "\nBest regards,\nALEX TAN"
    assert 2000 < len(long_style) < 6000
    snapshot["style_samples"][0]["text"] = long_style
    box = AssistantTools(snapshot)
    result = box.invoke("get_style_samples", {"offset": 0, "limit": 1}, role="email")
    assert result["ok"] and result["total"] == 1 and result["next_offset"] is None
    assert result["samples"][0]["text"] == long_style
    assert result["samples"][0]["truncated"] is False
    assert result["samples"][0]["text"].endswith("ALEX TAN")


def test_eight_long_style_samples_are_readable_without_exceeding_tool_output_limit():
    snapshot = selected_snapshot()
    snapshot["style_samples"] = [
        {"source_id": f"style-{index}", "text": "x" * 5950 + f"\nName {index}"}
        for index in range(8)
    ]
    box = AssistantTools(snapshot)
    seen = []
    offset = 0
    while offset is not None:
        page = box.invoke("get_style_samples", {"offset": offset, "limit": 1}, role="email")
        assert page["ok"] and page["total"] == 8
        assert len(page["samples"]) == 1
        sample = page["samples"][0]
        assert sample["truncated"] is False
        assert sample["text"].endswith(f"Name {len(seen)}")
        seen.append(sample["source_id"])
        offset = page["next_offset"]
    assert seen == [f"style-{index}" for index in range(8)]


def test_sdk_email_delegation_reads_both_sources_and_records_only_a_local_draft(monkeypatch):
    def no_external_client(*_args, **_kwargs):
        raise AssertionError("This test must not access a provider or persistence")

    monkeypatch.setattr(runtime, "AsyncOpenAI", no_external_client)
    monkeypatch.setattr(sqlite3, "connect", no_external_client)
    snapshot = selected_snapshot()
    original = deepcopy(snapshot)
    box = AssistantTools(snapshot)

    def after_manifest(invocation):
        manifest = tool_output(invocation, "list-mail")
        assert manifest["ok"] and manifest["writing_sample_count"] == 1
        return [tool_call("read_source", {
            "source_id": manifest["sources"][0]["source_id"], "offset": 0, "limit": 8000,
        }, "read-mail")]

    def after_read(invocation):
        email = tool_output(invocation, "read-mail")
        assert email["text"] == QUOTE
        assert email["sender"] == "Professor Mira Chen <mira.chen@example.invalid>"
        return [tool_call("get_style_samples", {"offset": 0, "limit": 1}, "read-style")]

    def after_style(invocation):
        email = tool_output(invocation, "read-mail")
        styles = tool_output(invocation, "read-style")
        sample = styles["samples"][0]
        assert sample == {"source_id": STYLE_ID, "text": STYLE, "truncated": False}
        signature = sample["text"].split("Best regards,\n", 1)[1]
        # Deliberately scripted content: verifies delivery/recording, not AI quality.
        body = (
            "Dear Professor Chen,\n\nThank you for clarifying the methods summary requirements.\n"
            "I have noted the deadline of 30 October 2026 at 18:00 Singapore time.\n\n"
            "Thank you for your guidance.\n\nBest regards,\n" + signature
        )
        return [tool_call("record_reply_draft", {
            "source_id": email["source_id"], "subject": "Re: " + email["title"],
            "body": body, "recipient": "mira.chen@example.invalid",
            "citations": [{"source_id": email["source_id"], "quote": email["text"]}],
            "style_source_ids": [sample["source_id"]],
        }, "record-draft")]

    def after_record(invocation):
        recorded = tool_output(invocation, "record-draft")
        assert recorded["ok"] and recorded["sent"] is False
        return [final("A local English reply draft is ready for review; no message was sent.")]

    scripted = {role: ScriptedReplyModel([]) for role in runtime.ROLES}
    scripted["manager"] = ScriptedReplyModel([
        [tool_call("consult_email", {
            "input": "Draft a reply using the selected writing example.", "operation": "reply",
        },
                   "delegate-email")],
        [final("Review the editable reply draft below. Nothing has been sent.")],
    ])
    scripted["email"] = ScriptedReplyModel([
        [tool_call("list_sources", {"offset": 0, "limit": 20}, "list-mail")],
        after_manifest, after_read, after_style, after_record,
    ])
    result = runtime.run_assistant(
        "Draft an English reply matching my sample, keeping my name and filling placeholders "
        "from the selected email. Do not send it.", box, model_override=scripted,
    )

    assert result.status == "completed"
    assert len(result.artifacts["reply_drafts"]) == 1
    draft = result.artifacts["reply_drafts"][0]
    assert draft["source_id"] == EMAIL_ID and draft["style_source_ids"] == [STYLE_ID]
    assert draft["subject"] == "Re: Methods summary"
    assert draft["body"].endswith("Best regards,\nALEX TAN")
    assert "[" not in draft["body"] and "]" not in draft["body"]
    assert draft["citations"] == [{"source_id": EMAIL_ID, "quote": QUOTE}]
    assert draft["sent"] is False and draft["saved"] is False
    assert draft["requires_confirmation"] is True
    assert snapshot == original
    assert [event["tool"] for event in box.events] == [
        "list_sources", "read_source", "get_style_samples", "record_reply_draft",
    ]
    assert result.usage["tool_calls"] == 5  # One delegation plus four real business tools.
    assert result.usage["model_calls"] == 7
    assert not scripted["task"].calls and not scripted["planning"].calls
    assert not scripted["meeting"].calls
    assert not any(event.get("status") == "repairing" for event in result.events)
    assert QUOTE not in json.dumps(result.events) and STYLE not in json.dumps(result.events)
    assert "ALEX TAN" not in json.dumps(result.events)

    # Every specialist can see selection counts before interpreting an empty page.
    for role in ("manager", "email"):
        instructions = scripted[role].calls[0]["system_instructions"]
        assert '"email_sources": 1' in instructions
        assert '"writing_samples": 1' in instructions
        assert QUOTE not in instructions and STYLE not in instructions


def draft_payload(**changes):
    return {
        "source_id": EMAIL_ID, "subject": "Re: Methods summary",
        "body": "Dear Professor Chen,\n\nThank you for the details.\n\nBest regards,\nALEX TAN",
        "recipient": "mira.chen@example.invalid",
        "citations": [{"source_id": EMAIL_ID, "quote": QUOTE}],
        "style_source_ids": [STYLE_ID], **changes,
    }


def read_reply_steps(*, include_style=True):
    steps = [
        [tool_call("list_sources", {"offset": 0, "limit": 20}, "list-mail")],
        [tool_call("read_source", {"source_id": EMAIL_ID, "offset": 0, "limit": 8000},
                   "read-mail")],
    ]
    if include_style:
        steps.append([tool_call("get_style_samples", {"offset": 0, "limit": 1}, "read-style")])
    return steps


def reply_models(email_steps):
    models = {role: ScriptedReplyModel([]) for role in runtime.ROLES}
    models["manager"] = ScriptedReplyModel([
        [tool_call("consult_email", {
            "input": "Draft an English reply using my selected sample; do not add commitments.",
            "operation": "reply",
        }, "delegate-email")],
        [final("The reply is ready to review.")],
    ])
    models["email"] = ScriptedReplyModel(email_steps)
    return models


def test_reply_text_without_an_artifact_gets_one_grounded_recording_attempt():
    plain_draft = draft_payload()["body"]

    def record_existing_draft(invocation):
        assert invocation["model_settings"].tool_choice == "required"
        assert {tool.name for tool in invocation["tools"]} == {
            "record_reply_draft", "request_reply_clarification",
        }
        assert tool_output(invocation, "read-mail")["text"] == QUOTE
        assert tool_output(invocation, "read-style")["samples"][0]["text"] == STYLE
        assert json.dumps(plain_draft, ensure_ascii=False) in json.dumps(
            invocation["input"], ensure_ascii=False,
        )
        return [tool_call("record_reply_draft", draft_payload(), "record-repaired-draft")]

    scripted = reply_models(read_reply_steps() + [[final(plain_draft)], record_existing_draft])
    box = AssistantTools(selected_snapshot())
    result = runtime.run_assistant("Draft my reply.", box, model_override=scripted)
    assert result.status == "completed"
    assert len(result.artifacts["reply_drafts"]) == 1
    assert result.artifacts["reply_drafts"][0]["body"] == plain_draft
    assert result.artifacts["reply_drafts"][0]["sent"] is False
    assert result.artifacts["reply_drafts"][0]["saved"] is False
    validation = [event for event in result.events if event.get("type") == "validation"]
    assert [event["status"] for event in validation] == ["repairing", "ok"]
    assert [event["tool"] for event in box.events].count("record_reply_draft") == 1
    assert len(scripted["email"].calls) == 5


def test_invalid_repaired_draft_does_not_become_a_success_or_trigger_another_attempt():
    invalid = draft_payload(citations=[{"source_id": EMAIL_ID, "quote": "Invented evidence."}])
    scripted = reply_models(read_reply_steps() + [
        [final("Here is a reply; I did not record it.")],
        [tool_call("record_reply_draft", invalid, "invalid-repair")],
    ])
    box = AssistantTools(selected_snapshot())
    result = runtime.run_assistant("Draft my reply.", box, model_override=scripted)
    assert result.status == "partial"
    assert not result.artifacts["reply_drafts"]
    assert [event["tool"] for event in box.events].count("record_reply_draft") == 1
    assert any(event.get("error_code") == "missing_reply_draft" for event in result.events)
    assert "ready to review" not in result.answer
    assert len(scripted["email"].calls) == 5


def test_reply_without_an_optional_writing_example_can_still_record_a_neutral_draft():
    snapshot = selected_snapshot()
    snapshot["style_samples"] = []
    neutral = draft_payload(style_source_ids=[], body="Hello,\n\nThank you for the details.")
    scripted = reply_models(read_reply_steps(include_style=False) + [
        [final(neutral["body"])],
        [tool_call("record_reply_draft", neutral, "neutral-repair")],
    ])
    box = AssistantTools(snapshot)
    result = runtime.run_assistant("Draft a neutral English reply.", box, model_override=scripted)
    assert result.status == "completed"
    assert len(result.artifacts["reply_drafts"]) == 1
    assert result.artifacts["reply_drafts"][0]["style_source_ids"] == []
    assert result.artifacts["reply_drafts"][0]["body"] == neutral["body"]
    assert not any(event["tool"] == "get_style_samples" for event in box.events)


@pytest.mark.parametrize("read_email", [False, True])
def test_reply_repair_requires_actual_mail_and_required_style_reads(read_email):
    steps = read_reply_steps(include_style=False) if read_email else []
    scripted = reply_models(steps + [[final("A reply is ready, but the needed tools were not read.")]])
    box = AssistantTools(selected_snapshot())
    result = runtime.run_assistant("Draft my reply using my example.", box, model_override=scripted)
    assert result.status in {"partial", "failed"}
    assert not result.artifacts["reply_drafts"]
    assert not any(event.get("status") == "repairing" for event in result.events)
    assert not any(event["tool"] == "record_reply_draft" for event in box.events)
    assert any(event.get("error_code") == "missing_reply_draft" for event in result.events)


def test_reply_with_no_selected_email_remains_a_clarification_without_forced_recording():
    snapshot = selected_snapshot()
    snapshot["sources"] = []
    scripted = reply_models([
        [tool_call("list_sources", {"offset": 0, "limit": 20}, "list-mail")],
        [tool_call("get_style_samples", {"offset": 0, "limit": 1}, "read-style")],
        [final("Your writing example is available; select the email you want to answer.")],
    ])
    scripted["manager"].steps[-1] = [final("Select the email you want to answer.")]
    box = AssistantTools(snapshot)
    result = runtime.run_assistant("Draft my reply.", box, model_override=scripted)
    assert result.status == "completed"
    assert not result.artifacts["reply_drafts"]
    assert not any(event.get("status") == "repairing" for event in result.events)
    assert not any(event["tool"] == "record_reply_draft" for event in box.events)


@pytest.mark.parametrize("limits", [
    runtime.RunLimits(max_model_calls=5),
    runtime.RunLimits(max_tool_calls=4),
])
def test_reply_recording_repair_obeys_the_shared_run_budget(limits):
    scripted = reply_models(read_reply_steps() + [
        [final("An English reply draft, without a recorded artifact.")],
        [tool_call("record_reply_draft", draft_payload(), "budgeted-repair")],
    ])
    box = AssistantTools(selected_snapshot())
    result = runtime.run_assistant("Draft my reply.", box, model_override=scripted, limits=limits)
    assert result.status == "partial"
    assert not result.artifacts["reply_drafts"]
    assert result.usage["model_calls"] <= limits.max_model_calls
    assert result.usage["tool_calls"] <= limits.max_tool_calls
    assert not any(event["tool"] == "record_reply_draft" for event in box.events)


def test_idempotent_recorded_reply_counts_as_a_real_artifact_without_repair():
    box = AssistantTools(selected_snapshot())
    prior = box.invoke("record_reply_draft", draft_payload(), role="email")
    assert prior["ok"]
    scripted = reply_models(read_reply_steps() + [
        [tool_call("record_reply_draft", draft_payload(), "same-draft")],
        [final("The recorded draft is ready for review.")],
    ])
    result = runtime.run_assistant("Draft my reply again.", box, model_override=scripted)
    assert result.status == "completed"
    assert len(result.artifacts["reply_drafts"]) == 1
    assert result.artifacts["reply_drafts"][0]["id"] == prior["artifact_id"]
    assert not any(event.get("status") == "repairing" for event in result.events)


def test_email_receives_the_original_user_constraints_and_can_clarify_before_drafting():
    original_request = (
        "Before drafting a reply, ask me which points to mention. Do not draft until I answer. "
        "Keep all explanations in English."
    )
    question = "Which points would you like me to include in the reply?"

    def preserve_original_request(invocation):
        # The manager's scripted delegation only says to draft. The original
        # user constraint must reach the specialist independently of that summary.
        assert original_request in json.dumps(invocation["input"], ensure_ascii=False)
        assert QUOTE not in json.dumps(invocation["input"], ensure_ascii=False)
        return [tool_call("request_reply_clarification", {
            "source_id": EMAIL_ID, "questions": [question],
        }, "clarify-first")]

    def after_clarification(invocation):
        clarified = tool_output(invocation, "clarify-first")
        assert clarified["ok"] and clarified["needs_clarification"]
        assert clarified["questions"] == [question]
        assert clarified["draft_recorded"] is False and clarified["sent"] is False
        return [final(question)]

    scripted = reply_models([preserve_original_request, after_clarification])
    scripted["manager"].steps[-1] = [final(question)]
    box = AssistantTools(selected_snapshot())
    result = runtime.run_assistant(original_request, box, model_override=scripted)
    assert result.status == "completed" and result.answer == question
    assert not any(box.artifacts.values())
    assert [event["tool"] for event in box.events] == ["request_reply_clarification"]
    assert not any(event.get("status") == "repairing" for event in result.events)


def test_corrective_turn_can_record_a_clarification_instead_of_forcing_a_draft():
    question = "Would you like the reply to acknowledge receipt only?"

    def choose_clarification(invocation):
        assert invocation["model_settings"].tool_choice == "required"
        assert {tool.name for tool in invocation["tools"]} == {
            "record_reply_draft", "request_reply_clarification",
        }
        assert tool_output(invocation, "read-mail")["text"] == QUOTE
        return [tool_call("request_reply_clarification", {
            "source_id": EMAIL_ID, "questions": [question],
        }, "repair-clarification")]

    scripted = reply_models(read_reply_steps() + [[final(question)], choose_clarification])
    scripted["manager"].steps[-1] = [final(question)]
    box = AssistantTools(selected_snapshot())
    result = runtime.run_assistant(
        "Help me reply, but ask whether I want a receipt acknowledgment before drafting.",
        box, model_override=scripted,
    )
    assert result.status == "completed" and result.answer == question
    assert not any(box.artifacts.values())
    assert [event["tool"] for event in box.events].count("request_reply_clarification") == 1
    assert not any(event["tool"] == "record_reply_draft" for event in box.events)
    assert sum(event.get("status") == "repairing" for event in result.events) == 1
    assert not any(event.get("status") == "error" for event in result.events)
    assert len(scripted["email"].calls) == 5


@pytest.mark.parametrize("empty_source", [False, True])
def test_empty_email_read_does_not_justify_a_corrective_draft_attempt(empty_source):
    snapshot = selected_snapshot()
    if empty_source:
        snapshot["sources"][0]["text"] = "   "
    steps = read_reply_steps()
    if not empty_source:
        steps[1] = [tool_call("read_source", {
            "source_id": EMAIL_ID, "offset": len(QUOTE) + 100, "limit": 8000,
        }, "read-mail")]
    scripted = reply_models(steps + [[final("A draft is ready without visible email content.")]])
    box = AssistantTools(snapshot)
    result = runtime.run_assistant("Draft my reply.", box, model_override=scripted)
    assert result.status == "partial"
    assert not result.artifacts["reply_drafts"]
    assert not any(event.get("status") == "repairing" for event in result.events)
    assert not any(event["tool"] == "record_reply_draft" for event in box.events)
    assert any(event.get("error_code") == "missing_reply_draft" for event in result.events)
