"""Exercise Inbox delegation and effort estimation together through the real SDK."""

import pytest
from streamlit.testing.v1 import AppTest
from test_effort_estimation import ScriptedModel as EffortModel
from test_effort_estimation import item
from test_inbox_agents import final, models
from test_unified_inbox_ui import APP_SOURCE, _analyze, _setup, _tables

from studentflow import batch_ui, effort_estimation, inbox_agents
from studentflow.storage import Store


def setup_estimation(tmp_path, monkeypatch, *, provider_failure=False, skip_tasks=False):
    database, _ = _setup(tmp_path, monkeypatch)
    extract = batch_ui.extract_tasks
    calls = {"coordinator": 0, "effort_models": []}

    def missing_effort(*args, **kwargs):
        result = extract(*args, **kwargs)
        for task in result["tasks"]:
            if task["title"] == "Prepare mock presentation slides":
                task["estimated_minutes"] = None
        return result

    def coordinate(tasks, events, api_key, model):
        calls["coordinator"] += 1
        scripted = models(manager=[[final()]] if skip_tasks else None)
        return inbox_agents.run_inbox_agents(tasks, events, "", "", model_override=scripted)

    def estimate(tasks, api_key, model):
        scripted = EffortModel(
            {"estimates": [item(task["id"], 60) for task in tasks]},
            error=RuntimeError("private-provider-content") if provider_failure else None,
        )
        calls["effort_models"].append(scripted)
        return effort_estimation.estimate_task_efforts(tasks, "", "", model_override=scripted)

    monkeypatch.setattr(batch_ui, "extract_tasks", missing_effort)
    monkeypatch.setattr(batch_ui, "run_inbox_agents", coordinate)
    monkeypatch.setattr(batch_ui, "estimate_task_efforts", estimate)
    return database, calls


def test_inbox_estimates_missing_effort_through_real_sdk_and_reuses_complete_result(
    tmp_path, monkeypatch,
):
    database, calls = setup_estimation(tmp_path, monkeypatch)
    first = AppTest.from_string(APP_SOURCE).run(timeout=30)
    _analyze(first)
    table = _tables(first, "Task")[0]
    assert list(table["Study time (min)"]) == [45, 60]
    assert table.iloc[1]["AI suggestion (min)"] == 60
    assert table.iloc[0]["Source-stated time (min)"] == 45
    assert not first.session_state["batch_analysis_run"]["task_result"]["effort"]["warnings"]
    assert len(calls["effort_models"]) == 1
    assert len(calls["effort_models"][0].calls) == 1
    assert Store(database).tasks() == [] and Store(database).list_fixed_events() == []

    second = AppTest.from_string(APP_SOURCE).run(timeout=30)
    _analyze(second)
    assert list(_tables(second, "Task")[0]["Study time (min)"]) == [45, 60]
    assert second.session_state["batch_analysis_run"]["cached"] is True
    assert calls["coordinator"] == 1 and len(calls["effort_models"]) == 1


def test_disabled_effort_never_invokes_estimator_after_inbox_delegation(tmp_path, monkeypatch):
    database, calls = setup_estimation(tmp_path, monkeypatch)
    app = AppTest.from_string(APP_SOURCE).run(timeout=30)
    next(c for c in app.checkbox if c.label == "Suggest study time when it is not stated").uncheck()
    _analyze(app)
    assert list(_tables(app, "Task")[0]["Study time (min)"]) == [45, 0]
    assert calls["effort_models"] == []
    assert Store(database).tasks() == []


def test_effort_provider_failure_preserves_review_and_is_not_cached_as_success(
    tmp_path, monkeypatch,
):
    database, calls = setup_estimation(tmp_path, monkeypatch, provider_failure=True)
    for _ in range(2):
        app = AppTest.from_string(APP_SOURCE).run(timeout=30)
        _analyze(app)
        assert list(_tables(app, "Task")[0]["Study time (min)"]) == [45, 0]
        assert len(_tables(app, "Event")[0]) == 2
        outcome = app.session_state["batch_analysis_run"]
        assert outcome["task_result"]["effort"]["warnings"]
        assert outcome["cached"] is False
        assert "private-provider-content" not in repr(outcome)
    assert calls["coordinator"] == 2
    assert len(calls["effort_models"]) == 2
    assert all(len(model.calls) == 1 for model in calls["effort_models"])
    assert Store(database).tasks() == [] and Store(database).list_fixed_events() == []


@pytest.mark.parametrize("suggest_effort", [True, False])
def test_unvisited_task_branch_cannot_trigger_postprocessing_model_calls(
    tmp_path, monkeypatch, suggest_effort,
):
    database, calls = setup_estimation(tmp_path, monkeypatch, skip_tasks=True)
    app = AppTest.from_string(APP_SOURCE).run(timeout=30)
    next(c for c in app.checkbox if c.label == "Suggest study time when it is not stated").set_value(
        suggest_effort,
    )
    _analyze(app)
    assert not _tables(app, "Task")
    assert calls["effort_models"] == []
    assert Store(database).tasks() == []
