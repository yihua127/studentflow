"""Real SDK delegation and service boundaries, using offline scripted models."""

import asyncio
import json

import pytest
from agents import Model, ModelResponse, Usage
from agents.models.interface import ModelTracing
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from studentflow import inbox_agents as runtime


def final(text="Done"):
    return ResponseOutputMessage(
        id="message", role="assistant", status="completed", type="message",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )


def call(name, arguments=None, call_id="call"):
    return ResponseFunctionToolCall(type="function_call", name=name,
                                    arguments=json.dumps(arguments or {}), call_id=call_id)


class ScriptedModel(Model):
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    async def get_response(self, *args, **kwargs):
        self.calls.append(kwargs)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            step = await step()
        return ModelResponse(output=step, usage=Usage(requests=1, input_tokens=10,
                                                      output_tokens=5, total_tokens=15),
                             response_id=None)

    async def stream_response(self, *args, **kwargs):
        raise AssertionError("No streaming")
        yield


def models(*, manager=None, task=None, fixed_event=None):
    return {
        "manager": ScriptedModel(manager if manager is not None else [
            [call("consult_task", {"input": "Analyze tasks"}, "m1")],
            [call("consult_fixed_event", {"input": "Analyze arrangements"}, "m2")],
            [final()],
        ]),
        "task": ScriptedModel(task if task is not None else [
            [call("analyze_learning_tasks")], [final()],
        ]),
        "fixed_event": ScriptedModel(fixed_event if fixed_event is not None else [
            [call("analyze_fixed_arrangements")], [final()],
        ]),
    }


def run(task=None, event=None, scripted=None):
    return runtime.run_inbox_agents(
        task or (lambda: {"tasks": [{"title": "Study"}]}),
        event or (lambda: {"events": [{"title": "Exam"}]}),
        "", "", model_override=scripted or models(),
    )


def test_actual_sdk_manager_runs_both_specialists_with_scoped_tools():
    scripted = models()
    result = run(scripted=scripted)
    assert result["status"] == "completed"
    assert result["task_result"] == {"tasks": [{"title": "Study"}]}
    assert result["event_result"] == {"events": [{"title": "Exam"}]}
    assert result["usage"]["model_calls"] == 7
    assert result["usage"]["tool_calls"] == 4
    assert result["usage"]["service_calls"] == 2
    assert result["usage"]["total_tokens"] == 105
    assert result["usage"]["usage_complete"] is False
    assert result["usage"]["orchestration_usage_complete"] is True
    assert result["usage"]["usage_scope"] == "orchestration_only"
    assert result["usage"]["service_tokens_included"] is False
    assert {t.name for t in scripted["manager"].calls[0]["tools"]} == {
        "consult_task", "consult_fixed_event",
    }
    assert {t.name for t in scripted["task"].calls[0]["tools"]} == {"analyze_learning_tasks"}
    assert {t.name for t in scripted["fixed_event"].calls[0]["tools"]} == {
        "analyze_fixed_arrangements",
    }
    for model in scripted.values():
        for invocation in model.calls:
            assert invocation["model_settings"].store is False
            assert invocation["model_settings"].parallel_tool_calls is False
            assert invocation["tracing"] == ModelTracing.DISABLED
            assert not invocation["handoffs"]
    assert any(e.get("tool") == "consult_task" for e in result["events"])
    assert any(e.get("tool") == "analyze_fixed_arrangements" for e in result["events"])


def test_full_results_and_specialist_prose_never_reach_other_models_or_event_logs():
    payload = {"tasks": [{"title": "PRIVATE MAIL BODY AND API KEY"}], "metadata": "SECRET"}
    scripted = models(task=[[call("analyze_learning_tasks")], [final("INVENTED PRIVATE REPORT")]])
    result = run(task=lambda: payload, scripted=scripted)
    assert result["task_result"] == payload
    all_inputs = repr([m.calls for m in scripted.values()])
    assert "PRIVATE MAIL BODY" not in all_inputs
    assert "SECRET" not in all_inputs
    assert "INVENTED PRIVATE REPORT" not in repr(scripted["manager"].calls)
    assert "PRIVATE" not in json.dumps(result["events"])
    result["task_result"]["tasks"].clear()
    assert payload["tasks"]  # Detached proposal result.


def test_repeated_tools_and_delegation_call_each_service_only_once():
    calls = []
    scripted = models(
        manager=[
            [call("consult_task", {"input": "Tasks"}, "m1")],
            [call("consult_task", {"input": "Retry tasks"}, "m2")],
            [call("consult_fixed_event", {"input": "Events"}, "m3")], [final()],
        ],
        task=[
            [call("analyze_learning_tasks", call_id="t1")],
            [call("analyze_learning_tasks", call_id="t2")], [final()],
            [call("analyze_learning_tasks", call_id="t3")], [final()],
        ],
    )
    result = run(task=lambda: calls.append("task") or {"tasks": []}, scripted=scripted)
    assert result["status"] == "completed"
    assert calls == ["task"]
    assert result["usage"]["service_calls"] == 2
    assert sum(e["status"] == "reused" for e in result["events"]) == 2


def test_failure_is_memoized_other_branch_survives_and_exception_is_redacted():
    calls = []

    def failing():
        calls.append("failed")
        raise RuntimeError("private-email@example.com sk-secret provider exception")

    scripted = models(task=[
        [call("analyze_learning_tasks", call_id="t1")],
        [call("analyze_learning_tasks", call_id="t2")], [final()],
    ])
    result = run(task=failing, scripted=scripted)
    assert calls == ["failed"]
    assert result["status"] == "partial"
    assert result["task_result"] is None
    assert result["event_result"] == {"events": [{"title": "Exam"}]}
    assert "task_analysis_failed" in result["warnings"]
    assert "sk-secret" not in json.dumps(result)
    assert "private-email" not in repr([m.calls for m in scripted.values()])


def test_unvisited_branch_is_incomplete_even_when_manager_claims_success():
    actions = []
    scripted = models(manager=[
        [call("consult_task", {"input": "Tasks"})], [final("Both analyses completed!")],
    ])
    result = run(event=lambda: actions.append("event") or {}, scripted=scripted)
    assert result["status"] == "partial"
    assert result["event_result"] is None
    assert actions == []
    assert "fixed_event_analysis_not_run" in result["warnings"]


def test_no_tools_means_no_success_or_silent_callback_fallback():
    calls = []
    scripted = models(manager=[[final("Done everything")]])
    result = run(task=lambda: calls.append("task") or {},
                 event=lambda: calls.append("event") or {}, scripted=scripted)
    assert result["status"] == "failed"
    assert calls == []
    assert set(result["warnings"]) == {"task_analysis_not_run", "fixed_event_analysis_not_run"}


@pytest.mark.parametrize("payload", [{"status": "partial", "tasks": [1]},
                                     {"status": "failed", "tasks": []}, {"ok": False}])
def test_service_partial_or_failed_status_cannot_become_completed(payload):
    result = run(task=lambda: payload)
    assert result["status"] == "partial"
    assert result["task_result"] == payload
    assert "task_analysis_incomplete" in result["warnings"]


def test_invalid_arguments_do_not_execute_service():
    calls = []
    scripted = models(task=[
        [call("analyze_learning_tasks", {"account": "other@example.com"})], [final()],
    ])
    result = run(task=lambda: calls.append("task") or {}, scripted=scripted)
    assert calls == []
    assert result["event_result"] is not None
    assert result["status"] == "partial"
    assert "invalid_tool_arguments" in result["warnings"]


def test_cross_role_tool_cannot_execute_other_service():
    calls = []
    scripted = models(task=[[call("analyze_fixed_arrangements")]])
    result = run(task=lambda: calls.append("task") or {},
                 event=lambda: calls.append("event") or {"events": []}, scripted=scripted)
    assert calls == ["event"]
    assert result["status"] == "partial"
    assert "task_analysis_not_run" in result["warnings"]


def test_shared_model_limit_preserves_results_and_blocks_further_calls(monkeypatch):
    monkeypatch.setattr(runtime, "MAX_MODEL_CALLS", 2)
    calls = []
    result = run(event=lambda: calls.append("event") or {})
    assert result["usage"]["model_calls"] == 2
    assert result["task_result"] is not None
    assert result["event_result"] is None
    assert result["status"] == "partial"
    assert calls == []
    assert "model_call_limit" in result["warnings"]


def test_shared_tool_limit_includes_delegation_and_service_calls(monkeypatch):
    monkeypatch.setattr(runtime, "MAX_TOOL_CALLS", 1)
    calls = []
    result = run(task=lambda: calls.append("task") or {})
    assert result["usage"]["tool_calls"] == 1
    assert result["usage"]["service_calls"] == 0
    assert result["status"] == "failed"
    assert calls == []
    assert "tool_call_limit" in result["warnings"]


def test_explicit_service_time_is_excluded_from_orchestration_budget(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    # _State's dataclass default_factory captured the original function.
    original = runtime._State

    def state():
        return original(started=clock[0])

    monkeypatch.setattr(runtime, "_State", state)

    def slow_service():
        clock[0] += 1000
        return {"tasks": []}

    result = run(task=slow_service)
    assert result["status"] == "completed"
    assert result["usage"]["service_seconds"] == 1000
    assert result["usage"]["elapsed_seconds"] == 1000


def test_model_timeout_never_invokes_services(monkeypatch):
    async def blocked():
        await asyncio.sleep(0.05)
        return [final()]

    monkeypatch.setattr(runtime, "MODEL_TIMEOUT_SECONDS", 0.001)
    result = run(scripted=models(manager=[blocked]))
    assert result["status"] == "failed"
    assert result["usage"]["service_calls"] == 0
    assert result["usage"]["orchestration_usage_complete"] is False
    assert "model_timeout" in result["warnings"]


def test_unconfigured_model_never_initializes_provider_or_services(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Should not initialize anything")

    monkeypatch.setattr(runtime, "AsyncOpenAI", forbidden)
    result = runtime.run_inbox_agents(forbidden, forbidden, "", "")
    assert result["status"] == "failed"
    assert result["usage"]["model_calls"] == 0
    assert result["usage"]["service_calls"] == 0
    assert "model_not_configured" in result["warnings"]


def test_provider_error_is_safe_and_never_becomes_a_success():
    scripted = models(manager=[RuntimeError("sk-private private mail body")])
    result = run(scripted=scripted)
    assert result["status"] == "failed"
    assert "model_error" in result["warnings"]
    assert "sk-private" not in json.dumps(result)


def test_invalid_callback_payload_does_not_retry_or_hide_sibling_result():
    result = run(task=lambda: None)
    assert result["status"] == "partial"
    assert result["task_result"] is None
    assert result["event_result"] is not None
    assert result["usage"]["service_calls"] == 2


def test_sync_entry_rejects_active_loop_without_executing_callback():
    async def active():
        with pytest.raises(RuntimeError, match="await run_inbox_agents_async"):
            run()

    asyncio.run(active())
