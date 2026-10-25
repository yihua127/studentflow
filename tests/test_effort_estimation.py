"""Offline, real-SDK tests for missing-effort suggestions and strict validation."""

import asyncio
import copy
import json

import pytest
from agents import Model, ModelResponse, Usage
from agents.models.interface import ModelTracing
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from studentflow import effort_estimation as estimation


class ScriptedModel(Model):
    def __init__(self, output=None, error=None, slow=False):
        self.output, self.error, self.slow = output, error, slow
        self.calls = []
        self.cancelled = False

    async def get_response(self, *args, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        if self.slow:
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        output = self.output
        if not isinstance(output, list):
            output = [ResponseOutputMessage(
                id="message", role="assistant", status="completed", type="message",
                content=[ResponseOutputText(
                    type="output_text", text=json.dumps(output), annotations=[],
                )],
            )]
        return ModelResponse(
            output=output, usage=Usage(requests=1, input_tokens=20, output_tokens=10, total_tokens=30),
            response_id=None,
        )

    async def stream_response(self, *args, **kwargs):
        raise AssertionError("No streaming")
        yield


def item(identifier="a", minutes=60, **changes):
    return {"task_id": identifier, "suggested_minutes": minutes,
            "estimate_reason": "Allow time to read and review the chapter.",
            "assumptions": ["One short chapter; no user speed history is available."], **changes}


def task(identifier="a", **changes):
    return {"id": identifier, "title": "Read and review one chapter", **changes}


def estimate(tasks, items, **kwargs):
    model = ScriptedModel({"estimates": items})
    result = estimation.estimate_task_efforts(tasks, "", "", model_override=model, **kwargs)
    return result, model


def user_payload(model):
    content = model.calls[0]["input"]
    if isinstance(content, list):
        content = content[-1]["content"]
    return json.loads(content)


def test_only_missing_or_zero_effort_is_estimated_without_mutation():
    tasks = [task("a", completed_minutes=20), task("b", estimated_minutes=0),
             task("known", estimated_minutes=90, completed_minutes=10)]
    before = copy.deepcopy(tasks)
    result, model = estimate(tasks, [item("a", 60), item("b", 45)])
    assert set(result["estimates"]) == {"a", "b"}
    assert result["estimates"]["a"]["suggested_minutes"] == 60  # Total, not 60 + progress.
    assert tasks == before
    assert not result["warnings"]
    assert len(model.calls) == 1
    invocation = model.calls[0]
    assert not invocation["tools"] and not invocation["handoffs"]
    assert invocation["model_settings"].store is False
    assert invocation["model_settings"].retry.max_retries == 0
    assert invocation["tracing"] == ModelTracing.DISABLED
    assert invocation["output_schema"].is_strict_json_schema() is True
    payload = user_payload(model)
    assert [record["id"] for record in payload["tasks"]] == ["a", "b"]
    assert payload["tasks"][0]["completed_minutes"] == 20
    assert "total_active_work" in payload["minutes_definition"]


def test_no_eligible_task_never_calls_a_model_or_client(monkeypatch):
    def forbidden(**kwargs):
        raise AssertionError("No client")
    monkeypatch.setattr(estimation, "AsyncOpenAI", forbidden)
    model = ScriptedModel()
    result = estimation.estimate_task_efforts(
        [task(estimated_minutes=75)], "", "", model_override=model,
    )
    assert result == {"estimates": {}, "warnings": []}
    assert not model.calls


def test_insufficient_material_can_return_null_with_clarification():
    result, model = estimate([task(title="Attend the 14:00–16:00 meeting")], [item(
        minutes=None, estimate_reason="Meeting duration is not evidence of separate preparation work.",
        assumptions=["Specify any preparation or follow-up deliverable before estimating active work."],
    )])
    assert result["estimates"]["a"]["suggested_minutes"] is None
    assert not result["warnings"]
    assert "meeting's calendar duration" in model.calls[0]["system_instructions"]
    assert "UNTRUSTED DATA" in model.calls[0]["system_instructions"]


@pytest.mark.parametrize("value", [True, False, -1, 3.5, "0", "60"])
def test_invalid_existing_effort_is_not_mistaken_for_missing(value):
    result, model = estimate([task(estimated_minutes=value)], [item()])
    assert result["estimates"] == {}
    assert result["warnings"]
    assert not model.calls


@pytest.mark.parametrize("value", [True, -1, 2.5, "20"])
def test_invalid_progress_rejected_before_model(value):
    result, model = estimate([task(completed_minutes=value)], [item()])
    assert result["warnings"] and not result["estimates"] and not model.calls


def test_batch_limit_and_duplicate_input_ids_fail_before_model():
    for tasks in ([task(str(index)) for index in range(31)], [task(), task()]):
        result, model = estimate(tasks, [item()])
        assert result["warnings"] and not result["estimates"] and not model.calls


def test_text_is_bounded_and_only_allowlisted_fields_reach_model():
    tasks = [task(title="T" * 900, source_quote="Q" * 3000, source_text="S" * 9000,
                  secret="unrelated-private-value", attachment_bytes=b"must-not-serialize")]
    result, model = estimate(tasks, [item()], familiarity="F" * 1500)
    payload = user_payload(model)
    entry = payload["tasks"][0]
    assert sum(len(entry[key]) for key in ("title", "source_quote", "source_text")) <= 4000
    assert entry["text_truncated"] is True
    assert len(payload["familiarity"]) == 1000
    assert "unrelated-private-value" not in json.dumps(payload)
    assert "attachment_bytes" not in json.dumps(payload)
    assert len(result["warnings"]) == 2


@pytest.mark.parametrize("items", [
    [item("a")], [item("a"), item("unknown")], [item("a"), item("a"), item("b")],
])
def test_missing_unknown_or_duplicate_output_id_rejects_the_whole_batch(items):
    result, _ = estimate([task("a"), task("b")], items)
    assert set(result["estimates"]) == {"a", "b"}
    assert all(value["suggested_minutes"] is None for value in result["estimates"].values())
    assert result["warnings"]


@pytest.mark.parametrize("value", [True, False, 0, -5, 22.5, "60"])
def test_output_minutes_are_strict_positive_integers_or_null(value):
    result, _ = estimate([task()], [item(minutes=value)])
    assert result["estimates"]["a"]["suggested_minutes"] is None
    assert result["warnings"]


@pytest.mark.parametrize("minutes", [1, 10000])
def test_effort_schema_accepts_both_supported_boundaries(minutes):
    result, _ = estimate([task()], [item(minutes=minutes)])
    assert result["estimates"]["a"]["suggested_minutes"] == minutes
    assert not result["warnings"]


def test_effort_over_storage_limit_is_rejected_instead_of_silently_capped():
    result, model = estimate([task()], [item(minutes=10001)])
    assert result["estimates"]["a"]["suggested_minutes"] is None
    assert result["warnings"]
    schema = model.calls[0]["output_schema"].json_schema()
    minute_options = schema["$defs"]["EffortSuggestion"]["properties"]["suggested_minutes"]["anyOf"]
    assert any(option.get("maximum") == 10000 for option in minute_options)


@pytest.mark.parametrize("changes", [
    {"estimate_reason": " "}, {"assumptions": []}, {"assumptions": [" "]},
    {"approved": True},
])
def test_unjustified_or_extra_approval_fields_are_rejected(changes):
    result, _ = estimate([task()], [item(**changes)])
    assert result["estimates"]["a"]["suggested_minutes"] is None
    assert result["warnings"]


def test_suggested_total_cannot_be_below_recorded_completed_minutes():
    tasks = [task(completed_minutes=90)]
    result, _ = estimate(tasks, [item(minutes=30)])
    assert result["estimates"]["a"]["suggested_minutes"] is None
    assert result["warnings"]
    assert tasks[0]["completed_minutes"] == 90


def test_model_cannot_use_a_network_or_write_tool():
    model = ScriptedModel([ResponseFunctionToolCall(
        type="function_call", name="send_email", call_id="forbidden",
        arguments='{"to":"outside@example.invalid"}',
    )])
    result = estimation.estimate_task_efforts([task()], "", "", model_override=model)
    assert result["estimates"]["a"]["suggested_minutes"] is None
    assert result["warnings"]
    assert len(model.calls) == 1
    assert model.calls[0]["tools"] == []


def test_timeout_cancels_the_single_call(monkeypatch):
    monkeypatch.setattr(estimation, "REQUEST_TIMEOUT_SECONDS", 0.1)
    model = ScriptedModel({"estimates": [item()]}, slow=True)
    result = estimation.estimate_task_efforts([task()], "", "", model_override=model)
    assert model.cancelled
    assert len(model.calls) == 1
    assert result["estimates"]["a"]["suggested_minutes"] is None
    assert "timed out" in result["warnings"][0]


def test_provider_errors_do_not_expose_task_body_or_key(caplog):
    private = "sk-private-key with confidential task body"
    model = ScriptedModel(error=RuntimeError(private))
    result = estimation.estimate_task_efforts([task()], "", "", model_override=model)
    assert result["warnings"]
    assert private not in str(result) + caplog.text
    assert len(model.calls) == 1


def test_client_uses_explicit_key_official_endpoint_no_retries_and_closes(monkeypatch):
    created = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.options, self.closed = kwargs, False
            created.append(self)

        async def close(self):
            self.closed = True

    model = ScriptedModel(error=RuntimeError("synthetic provider failure"))
    monkeypatch.setenv("OPENAI_BASE_URL", "https://untrusted.invalid")
    monkeypatch.setattr(estimation, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(estimation, "OpenAIResponsesModel", lambda **kwargs: model)
    result = estimation.estimate_task_efforts([task()], "synthetic-key", "synthetic-model")
    assert result["warnings"]
    assert created[0].options == {
        "api_key": "synthetic-key", "base_url": "https://api.openai.com/v1",
        "timeout": 45.0, "max_retries": 0,
    }
    assert created[0].closed
    assert "synthetic-key" not in str(model.calls)


def test_missing_key_returns_null_for_every_eligible_id_without_a_client(monkeypatch):
    def forbidden(**kwargs):
        raise AssertionError("No client")
    monkeypatch.setattr(estimation, "AsyncOpenAI", forbidden)
    result = estimation.estimate_task_efforts([task("a"), task("b")], "", "model")
    assert set(result["estimates"]) == {"a", "b"}
    assert all(value["suggested_minutes"] is None for value in result["estimates"].values())
    assert result["warnings"]
