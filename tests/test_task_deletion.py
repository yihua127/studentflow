"""Manual deletion preserves source records and leaves remaining tasks usable."""

import json
from datetime import datetime

import pytest
from streamlit.testing.v1 import AppTest

from studentflow.daily_review import due_daily_reviews, planned_days
from studentflow.storage import Store

APP_SOURCE = """
import os
from pathlib import Path
from studentflow.storage import Store
from studentflow.tasks_ui import render_tasks
render_tasks(Store(Path(os.environ['STUDENTFLOW_DELETE_TEST']) / 'tasks.sqlite3'))
"""
REVIEW_TIME = datetime.fromisoformat("2030-01-01T21:00:00+08:00")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDENTFLOW_DELETE_TEST", str(tmp_path))
    return Store(tmp_path / "tasks.sqlite3")


def task(store, title="Report", **changes):
    return store.save_task({
        "title": title,
        "mail_id": "message-one",
        "mail_subject": "Report assignment",
        "source_quote": "Please prepare the report.",
        "source_filename": "assignment.pdf",
        "estimated_minutes": 60,
        "due_at": "2030-01-02T17:00:00+08:00",
        "status": "confirmed",
        **changes,
    })


def saved_plan(store, identifiers):
    tasks = {item["id"]: item for item in store.tasks()}
    plan = {
        "id": "plan-one",
        "saved_at": "2030-01-01T08:00:00+08:00",
        "inputs": {
            "tasks": list(tasks.values()),
            "settings": {"timezone": "Asia/Singapore", "study_end": "20:00"},
        },
        "result": {
            "blocks": [{
                "task_id": identifier,
                "title": tasks[identifier]["title"],
                "start": f"2030-01-01T{9 + index:02}:00:00+08:00",
                "end": f"2030-01-01T{10 + index:02}:00:00+08:00",
                "minutes": 60,
            } for index, identifier in enumerate(identifiers)],
            "unscheduled": [{"task_id": identifier, "title": tasks[identifier]["title"],
                             "minutes": 15, "reason": "Not enough time"}
                            for identifier in identifiers],
        },
    }
    with store.connect() as db:
        db.execute("INSERT INTO plans VALUES (?, ?)", (plan["id"], json.dumps(plan)))
    return plan["id"]


def checked(app):
    app.run(timeout=30)
    assert not app.exception
    return app


def field(elements, label):
    return next(element for element in elements if element.label == label)


@pytest.mark.parametrize("completed_minutes", [0, 60], ids=["unfinished", "completed"])
def test_delete_either_category_persists_and_preserves_other_tasks_and_attachment(
    workspace, tmp_path, completed_minutes,
):
    identifier = task(workspace, completed_minutes=completed_minutes)
    retained_id = task(workspace, "Keep this task", completed_minutes=15)
    retained = next(item for item in workspace.tasks() if item["id"] == retained_id)
    attachment = tmp_path / "assignment.pdf"
    attachment.write_bytes(b"original attachment content")
    selected = next(item for item in workspace.tasks() if item["id"] == identifier)

    assert workspace.delete_task(identifier, expected_updated_at=selected["updated_at"]) is True
    reopened = Store(workspace.path)
    assert reopened.tasks() == [retained]
    assert reopened.delete_task(identifier) is False
    assert attachment.read_bytes() == b"original attachment content"


def test_delete_rejects_stale_confirmation_without_changing_task(workspace):
    identifier = task(workspace)
    original_version = workspace.tasks()[0]["updated_at"]
    latest = workspace.update_task(identifier, {"completed_minutes": 15})

    with pytest.raises(ValueError, match="changed"):
        workspace.delete_task(identifier, expected_updated_at=original_version)
    assert workspace.tasks() == [latest]
    assert workspace.delete_task(identifier, expected_updated_at=latest["updated_at"]) is True


def test_deleted_task_reextraction_gets_fresh_id_without_restoring_old_plan(workspace):
    original_id = task(workspace)
    saved_plan(workspace, [original_id])
    workspace.delete_task(original_id)

    reopened = Store(workspace.path)
    replacement_id = task(reopened)
    assert replacement_id != original_id
    assert reopened.latest_plan()["result"]["blocks"] == []
    assert reopened.latest_plan()["result"]["unscheduled"] == []
    assert reopened.latest_plan()["inputs"]["tasks"] == []
    assert due_daily_reviews(reopened.plans(), [], REVIEW_TIME) == []
    assert reopened.delete_task(replacement_id) is True
    assert task(Store(workspace.path)) not in {original_id, replacement_id}


def test_delete_preserves_recorded_progress_and_raw_plan_history(workspace):
    identifier = task(workspace)
    plan_id = saved_plan(workspace, [identifier])
    recorded = workspace.record_daily_review(
        "2030-01-01", plan_id, {identifier: 30}, now=REVIEW_TIME,
    )
    with workspace.connect() as db:
        original_plans = db.execute("SELECT id, payload FROM plans").fetchall()

    workspace.delete_task(identifier)
    reopened = Store(workspace.path)
    assert reopened.daily_reviews() == [recorded]
    assert reopened.progress_by_day() == {"2030-01-01": 30}
    assert reopened.record_daily_review(
        "2030-01-01", plan_id, {identifier: 30}, now=REVIEW_TIME,
    ) == recorded
    with reopened.connect() as db:
        assert db.execute("SELECT id, payload FROM plans").fetchall() == original_plans
    assert reopened.latest_plan()["result"]["blocks"] == []


def test_daily_review_remains_usable_after_one_planned_task_is_deleted(workspace):
    deleted_id = task(workspace)
    retained_id = task(workspace, "Remaining report", estimated_minutes=120)
    plan_id = saved_plan(workspace, [deleted_id, retained_id])
    workspace.delete_task(deleted_id)

    reopened = Store(workspace.path)
    plan = reopened.latest_plan()
    assert [block["task_id"] for block in plan["result"]["blocks"]] == [retained_id]
    assert [item["task_id"] for item in plan["result"]["unscheduled"]] == [retained_id]
    assert [item["id"] for item in plan["inputs"]["tasks"]] == [retained_id]
    assert planned_days(reopened.plans())["2030-01-01"]["minutes"] == {retained_id: 60}
    due, = due_daily_reviews(reopened.plans(), reopened.daily_reviews(), REVIEW_TIME)
    recorded = reopened.record_daily_review(
        due["date"], plan_id, due["minutes"], now=REVIEW_TIME,
    )
    assert recorded["minutes"] == {retained_id: 60}
    assert reopened.tasks()[0]["remaining_minutes"] == 60


@pytest.mark.parametrize("category", ["Unfinished", "Completed"])
def test_delete_ui_confirms_and_refreshes_counts_selection_and_draft(workspace, category):
    completed = 60 if category == "Completed" else 0
    identifier = task(workspace, completed_minutes=completed)
    retained_id = task(workspace, "Keep in category", completed_minutes=completed)
    other_id = task(workspace, "Other category", completed_minutes=60 - completed)
    app = checked(AppTest.from_string(APP_SOURCE))
    app.radio(key="tasks_category").set_value(category)
    checked(app)
    app.session_state["planner_draft"] = {"signature": "stale draft"}

    assert app.selectbox(key="tasks_selected_" + category).value == identifier
    assert field(app.button, "Delete task").disabled
    field(app.checkbox, "I want to delete this task.").check()
    checked(app)
    assert not field(app.button, "Delete task").disabled
    field(app.button, "Delete task").click()
    checked(app)

    assert {item["id"] for item in workspace.tasks()} == {retained_id, other_id}
    assert app.radio(key="tasks_category").value == category
    assert set(app.radio(key="tasks_category").options) == {"Unfinished (1)", "Completed (1)"}
    assert app.selectbox(key="tasks_selected_" + category).value == retained_id
    assert list(app.dataframe[0].value["Task"]) == ["Keep in category"]
    assert field(app.checkbox, "I want to delete this task.").value is False
    assert field(app.button, "Delete task").disabled
    assert "planner_draft" not in app.session_state


@pytest.mark.parametrize("change", ["selection", "external_edit"])
def test_delete_ui_resets_confirmation_when_selected_task_changes(workspace, change):
    identifier = task(workspace)
    second_id = task(workspace, "Second task")
    app = checked(AppTest.from_string(APP_SOURCE))
    field(app.checkbox, "I want to delete this task.").check()
    checked(app)
    original_key = field(app.checkbox, "I want to delete this task.").key
    assert not field(app.button, "Delete task").disabled

    if change == "selection":
        app.selectbox(key="tasks_selected_Unfinished").set_value(second_id)
    else:
        workspace.update_task(identifier, {"completed_minutes": 15})
    checked(app)

    assert field(app.checkbox, "I want to delete this task.").key != original_key
    assert field(app.checkbox, "I want to delete this task.").value is False
    assert field(app.button, "Delete task").disabled
    assert len(workspace.tasks()) == 2
    if change == "selection":
        app.selectbox(key="tasks_selected_Unfinished").set_value(identifier)
        checked(app)
        assert field(app.checkbox, "I want to delete this task.").value is False
        assert field(app.button, "Delete task").disabled


@pytest.mark.parametrize("category", ["Unfinished", "Completed"])
def test_delete_ui_last_task_shows_empty_state(workspace, category):
    task(workspace, completed_minutes=60 if category == "Completed" else 0)
    app = checked(AppTest.from_string(APP_SOURCE))
    app.radio(key="tasks_category").set_value(category)
    checked(app)
    field(app.checkbox, "I want to delete this task.").check()
    checked(app)
    field(app.button, "Delete task").click()
    checked(app)

    assert workspace.tasks() == []
    assert any("No tasks yet" in message.value for message in app.info)
    assert not app.selectbox and not app.text_input and not app.number_input
    assert not any(button.label == "Delete task" for button in app.button)
