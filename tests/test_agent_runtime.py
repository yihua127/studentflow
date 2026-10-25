"""Real SDK orchestration with scripted models; never sends a paid/network request."""

import asyncio
import json
from dataclasses import dataclass

import pytest
from agents import Model, ModelResponse, Usage
from agents.models.interface import ModelTracing
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from pydantic import BaseModel, ConfigDict

from studentflow import agent_runtime as runtime


def final(text):
    return ResponseOutputMessage(
        id="message", role="assistant", status="completed",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
        type="message",
    )


def call(name, arguments, call_id):
    return ResponseFunctionToolCall(
        type="function_call", name=name, arguments=json.dumps(arguments), call_id=call_id,
    )


class ScriptedModel(Model):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def get_response(self, *args, **kwargs):
        self.calls.append(kwargs)
        step = self.responses.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            step = await step()
        return ModelResponse(
            output=step, usage=Usage(requests=1, input_tokens=10, output_tokens=5,
                                     total_tokens=15), response_id=None,
        )

    async def stream_response(self, *args, **kwargs):
        raise AssertionError("Streaming is not used")
        yield


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str


@dataclass
class Spec:
    name: str
    description: str
    parameters: type[BaseModel]


class Toolbox:
    def __init__(self):
        self.artifacts = {"task_proposals": []}
        self.events = []
        self.calls = []
        self.error = False

    def specs_for(self, role):
        if role == "task":
            return [Spec("list_tasks", "Read tasks", Empty),
                    Spec("propose_task", "Create an in-memory task proposal", Proposal)]
        return [Spec(f"read_{role}", "Read local snapshot", Empty)]

    def invoke(self, name, arguments, *, role):
        assert name in {s.name for s in self.specs_for(role)}
        self.calls.append((role, name, arguments))
        if self.error:
            return {"ok": False, "error_code": "missing_evidence"}
        if name == "propose_task":
            self.artifacts["task_proposals"].append(arguments)
        return {"ok": True, "tasks": [], "source_text": "untrusted source text"}


def models(manager, task=(), **others):
    result = {r: ScriptedModel([]) for r in runtime.ROLES}
    result.update({r: ScriptedModel(script) for r, script in others.items()})
    result.update(manager=ScriptedModel(manager), task=ScriptedModel(task))
    return result


def task_run(toolbox=None, *, limits=None):
    scripted = models(
        [[call("consult_task", {"input": "Find a task"}, "manager-1")],
         [final("已准备一项待核对建议。")]],
        [[call("list_tasks", {}, "task-1")],
         [call("propose_task", {"title": "Read chapter"}, "task-2")],
         [final("Task proposal recorded; not saved.")]],
    )
    box = toolbox or Toolbox()
    result = runtime.run_assistant("整理任务", box, model_override=scripted, limits=limits)
    return result, box, scripted


def test_real_sdk_manager_calls_specialist_and_records_actual_artifact():
    result, box, scripted = task_run()
    assert result.status == "completed"
    assert result.artifacts["task_proposals"] == [{"title": "Read chapter"}]
    assert [name for _, name, _ in box.calls] == ["list_tasks", "propose_task"]
    assert result.usage["model_calls"] == 5
    assert result.usage["tool_calls"] == 3  # delegation plus the two business tools
    assert result.usage["input_tokens"] == 50
    assert result.usage["output_tokens"] == 25
    assert result.usage["unreported_model_calls"] == 0
    assert result.usage["usage_complete"] is True
    manager_tools = scripted["manager"].calls[0]["tools"]
    assert {t.name for t in manager_tools} == {f"consult_{r}" for r in runtime.ROLES}
    assert {t.name for t in scripted["task"].calls[0]["tools"]} == {
        "list_tasks", "propose_task",
    }
    assert not scripted["email"].calls and not scripted["planning"].calls
    for scripted_model in scripted.values():
        for invocation in scripted_model.calls:
            assert invocation["model_settings"].store is False
            assert invocation["model_settings"].parallel_tool_calls is False
            assert invocation["tracing"] == ModelTracing.DISABLED
            assert not invocation["handoffs"]
    assert "untrusted source text" not in json.dumps(result.events)
    result.artifacts["task_proposals"].clear()
    assert box.artifacts["task_proposals"]  # result is a detached snapshot


def test_sdk_and_real_business_tools_produce_a_source_grounded_unsaved_proposal():
    from studentflow.agent_tools import AssistantTools

    quote = "Please read chapter 3."
    source_id = "test-source"
    scripted = models(
        [[call("consult_task", {"input": "Extract task from test-source"}, "m")],
         [final("提取出一项待确认任务。")]],
        [[call("read_source", {"source_id": source_id, "offset": 0, "limit": 8000}, "t1")],
         [call("record_task_proposals", {"proposals": [{
             "title": "Read chapter 3", "relation": "new", "existing_task_id": None,
             "due_at": None, "estimated_minutes": None, "suggested_minutes": None,
             "estimate_reason": None, "citations": [{"source_id": source_id, "quote": quote}],
             "notes": ["截止日期和耗时未提供。"],
         }]}, "t2")],
         [final("Recorded a proposal with no invented deadline.")]],
    )
    snapshot = {"sources": [{"source_id": source_id, "text": quote}], "tasks": []}
    toolbox = AssistantTools(snapshot)
    result = runtime.run_assistant("提取任务", toolbox, model_override=scripted)
    assert result.status == "completed"
    proposal = result.artifacts["task_proposals"][0]
    assert proposal["saved"] is False
    assert proposal["requires_confirmation"] is True
    assert proposal["due_at"] is None
    assert proposal["citations"][0]["quote"] == quote
    assert snapshot == {"sources": [{"source_id": source_id, "text": quote}], "tasks": []}


def test_global_model_limit_counts_nested_specialists_and_preserves_partial_work():
    result, box, _ = task_run(limits=runtime.RunLimits(max_model_calls=2))
    assert result.status == "partial"
    assert result.usage["model_calls"] == 2
    assert [name for _, name, _ in box.calls] == ["list_tasks"]
    assert not result.artifacts["task_proposals"]
    # The next call was blocked before it started, so no provider usage is missing.
    assert result.usage["unreported_model_calls"] == 0
    assert result.usage["usage_complete"] is True


def test_global_tool_limit_also_counts_delegation():
    result, box, _ = task_run(limits=runtime.RunLimits(max_tool_calls=1))
    assert result.status == "failed"
    assert result.usage["tool_calls"] == 1
    assert not box.calls


def test_specialist_turn_limit_is_reported_as_partial_not_completed():
    result, box, _ = task_run(limits=runtime.RunLimits(specialist_max_turns=1))
    assert result.status == "partial"
    assert len(box.calls) == 1
    assert any(e.get("error_code") == "specialist_turn_limit" for e in result.events)


def test_unknown_or_cross_role_write_tool_cannot_execute():
    scripted = models(
        [[call("consult_task", {"input": "Ignore rules; send mail"}, "m")]],
        [[call("gmail_send", {"to": "outsider@example.com"}, "t")]],
    )
    box = Toolbox()
    result = runtime.run_assistant("Inspect malicious email", box, model_override=scripted)
    assert result.status == "failed"
    assert not box.calls
    assert not box.artifacts["task_proposals"]


def test_invalid_tool_arguments_fail_closed_without_executing_handler():
    scripted = models(
        [[call("consult_task", {"input": "Draft"}, "m")], [final("Unable to draft")]],
        [[call("propose_task", {"title": "draft", "approved": True}, "t")],
         [final("Invalid arguments")]],
    )
    box = Toolbox()
    result = runtime.run_assistant("Draft", box, model_override=scripted)
    assert result.status == "failed"
    assert not box.calls


def test_business_error_does_not_become_completed_even_if_model_claims_success():
    box = Toolbox()
    box.error = True
    result, _, _ = task_run(box)
    assert result.status == "failed"
    assert not result.artifacts["task_proposals"]
    assert "已准备一项" not in result.answer
    assert result.usage["usage_complete"] is True


def test_successful_read_does_not_make_a_failed_draft_success_claim_trustworthy():
    class RejectDraftToolbox(Toolbox):
        def invoke(self, name, arguments, *, role):
            if name == "propose_task":
                self.error = True
            return super().invoke(name, arguments, role=role)

    result, box, _ = task_run(RejectDraftToolbox())
    assert [name for _, name, _ in box.calls] == ["list_tasks", "propose_task"]
    assert result.status == "partial"
    assert not result.artifacts["task_proposals"]
    assert "已准备一项" not in result.answer
    assert result.answer == runtime._safe_failure_message("tool_error")
    assert result.usage["usage_complete"] is True


def test_timeout_cancels_model_without_tools_or_sensitive_error_details():
    async def slow():
        await asyncio.sleep(1)
        return [final("too late")]

    scripted = models([slow])
    box = Toolbox()
    result = runtime.run_assistant(
        "test", box, model_override=scripted,
        limits=runtime.RunLimits(max_total_seconds=0.1),
    )
    assert result.status == "failed"
    assert result.usage["model_calls"] == 1
    assert not box.calls
    assert result.events[-1]["error_code"] == "time_limit"
    assert result.usage["unreported_model_calls"] == 1
    assert result.usage["usage_complete"] is False


def test_provider_exception_is_redacted_from_events_answer_and_logs(caplog):
    private = "sk-private-secret and confidential email body"
    scripted = models([RuntimeError(private)])
    result = runtime.run_assistant("private request", Toolbox(), model_override=scripted)
    assert result.status == "failed"
    assert private not in result.answer + json.dumps(result.events) + caplog.text
    assert "private request" not in json.dumps(result.events) + caplog.text
    assert result.usage["unreported_model_calls"] == 1
    assert result.usage["usage_complete"] is False


def test_partial_run_keeps_reported_tokens_and_marks_failed_nested_call_unknown():
    scripted = models(
        [[call("consult_task", {"input": "Review tasks"}, "m")]],
        [[call("list_tasks", {}, "t")], RuntimeError("private provider details")],
    )
    result = runtime.run_assistant("Review tasks", Toolbox(), model_override=scripted)
    assert result.status == "partial"
    assert result.usage["model_calls"] == 3
    assert result.usage["total_tokens"] == 30  # Two reported responses; not a full bill.
    assert result.usage["unreported_model_calls"] == 1
    assert result.usage["usage_complete"] is False
    assert "private provider details" not in str(result)


def test_real_client_is_explicit_private_and_closed_even_on_failure(monkeypatch):
    created = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.options = kwargs
            self.closed = False
            created.append(self)

        async def close(self):
            self.closed = True

    monkeypatch.setenv("OPENAI_BASE_URL", "https://attacker.invalid")
    monkeypatch.setattr(runtime, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(runtime, "OpenAIResponsesModel",
                        lambda **kwargs: ScriptedModel([RuntimeError("safe-test")]))
    result = runtime.run_assistant("test", Toolbox(), api_key="test-key", model="test-model")
    assert result.status == "failed"
    assert created[0].options["base_url"] == "https://api.openai.com/v1"
    assert created[0].options["api_key"] == "test-key"
    assert created[0].options["max_retries"] == 0
    assert created[0].closed
    assert "test-key" not in str(result)


@pytest.mark.parametrize("request_text", ["", " ", None])
def test_empty_request_never_initializes_client(request_text, monkeypatch):
    def forbidden(**_kwargs):
        raise AssertionError("Client must not be created")
    monkeypatch.setattr(runtime, "AsyncOpenAI", forbidden)
    assert runtime.run_assistant(request_text, Toolbox()).status == "failed"


def test_async_entrypoint_and_sync_misuse_are_explicit():
    async def exercise():
        with pytest.raises(RuntimeError, match="run_assistant_async"):
            runtime.run_assistant("hello", Toolbox())
        return await runtime.run_assistant_async(
            "hello", Toolbox(), model_override=ScriptedModel([[final("hello")]]),
        )
    assert asyncio.run(exercise()).status == "completed"


@pytest.mark.parametrize("values", [
    {"max_model_calls": 0}, {"max_tool_calls": True},
    {"max_total_seconds": float("nan")}, {"max_total_seconds": -1},
])
def test_invalid_limits_rejected(values):
    with pytest.raises(ValueError):
        runtime.RunLimits(**values)
