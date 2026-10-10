"""Task editing journeys use real local persistence and Streamlit form state."""

import csv
import io

import pytest
from streamlit.testing.v1 import AppTest

from studentflow.storage import Store
from studentflow.tasks_ui import tasks_csv

APP_SOURCE = """
import os
from pathlib import Path
from studentflow.storage import Store
from studentflow.tasks_ui import render_tasks
render_tasks(Store(Path(os.environ['STUDENTFLOW_TEST_TASKS']) / 'tasks.sqlite3'))
"""


def checked(app):
    app.run(timeout=30)
    assert not app.exception
    return app


def field(elements, label):
    return next(element for element in elements if element.label == label)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDENTFLOW_TEST_TASKS", str(tmp_path))
    return Store(tmp_path / "tasks.sqlite3")


def save_task(store, **changes):
    return store.save_task({
        "title": "Prepare report", "mail_id": "message-one", "mail_subject": "Report assignment",
        "status": "pending", "due_at": None, "estimated_minutes": None,
        "source_quote": "Please prepare the report.", **changes,
    })


def save_changes(app):
    field(app.checkbox, "I have reviewed these task details.").check()
    field(app.button, "Save changes").click()
    checked(app)


def test_task_editor_confirms_missing_details_and_renames_same_record(workspace):
    task_id = save_task(workspace)
    app = checked(AppTest.from_string(APP_SOURCE))
    assert any("Confirmed requires a deadline" in caption.value for caption in app.caption)
    field(app.text_input, "Task name").set_value("Prepare final report")
    field(app.text_input, "Deadline (Singapore time)").set_value("2026-10-16 17:00")
    field(app.number_input, "Estimated study time (minutes)").set_value(120)
    save_changes(app)

    task, = workspace.tasks()
    assert task["id"] == task_id and task["title"] == "Prepare final report"
    assert task["status"] == "confirmed" and task["estimated_minutes"] == 120
    assert task["due_at"] == "2026-10-16T17:00:00+08:00"
    assert task["source_quote"] == "Please prepare the report."
    assert app.dataframe[0].value.iloc[0]["Remaining (min)"] == 120
    assert app.radio(key="tasks_category").value == "Unfinished"
    assert field(app.checkbox, "I have reviewed these task details.").value is False


def test_editor_tracks_partial_completion_and_moves_both_directions(workspace):
    save_task(workspace, status="confirmed", due_at="2026-10-16T17:00:00+08:00", estimated_minutes=60)
    app = checked(AppTest.from_string(APP_SOURCE))
    field(app.number_input, "Completed study time (minutes)").set_value(15)
    save_changes(app)
    assert app.radio(key="tasks_category").value == "Unfinished"
    assert app.dataframe[0].value.iloc[0]["Remaining (min)"] == 45

    field(app.number_input, "Completed study time (minutes)").set_value(60)
    save_changes(app)
    task, = workspace.tasks()
    assert task["is_completed"] and task["remaining_minutes"] == 0
    assert app.radio(key="tasks_category").value == "Completed"
    assert app.dataframe[0].value.iloc[0]["Progress"] == "Completed"

    field(app.number_input, "Estimated study time (minutes)").set_value(90)
    save_changes(app)
    task, = workspace.tasks()
    assert task["id"] and not task["is_completed"] and task["remaining_minutes"] == 30
    assert app.radio(key="tasks_category").value == "Unfinished"


def test_unknown_estimate_stays_unfinished_and_can_save_missing_deadline(workspace):
    save_task(workspace)
    app = checked(AppTest.from_string(APP_SOURCE))
    assert app.radio(key="tasks_category").value == "Unfinished"
    assert app.dataframe[0].value.iloc[0]["Remaining (min)"] is None
    field(app.number_input, "Estimated study time (minutes)").set_value(45)
    save_changes(app)
    task, = workspace.tasks()
    assert task["status"] == "pending" and not task["is_completed"]
    assert task["remaining_minutes"] == 45 and task["due_at"] is None
    assert any("Needs details" in message.value for message in app.success)


@pytest.mark.parametrize("invalid", ["review", "deadline", "over_completed"])
def test_invalid_edit_does_not_mutate_saved_task(workspace, invalid):
    save_task(workspace, estimated_minutes=60)
    before = workspace.tasks()
    app = checked(AppTest.from_string(APP_SOURCE))
    field(app.text_input, "Task name").set_value("Unreviewed change")
    if invalid != "review":
        field(app.checkbox, "I have reviewed these task details.").check()
    if invalid == "deadline":
        field(app.text_input, "Deadline (Singapore time)").set_value("next week")
    elif invalid == "over_completed":
        field(app.number_input, "Completed study time (minutes)").set_value(90)
    field(app.button, "Save changes").click()
    checked(app)
    assert app.error
    assert workspace.tasks() == before


def test_external_progress_refreshes_editor_and_resets_review(workspace):
    task_id = save_task(workspace, estimated_minutes=90)
    app = checked(AppTest.from_string(APP_SOURCE))
    old_key = field(app.number_input, "Completed study time (minutes)").key
    field(app.text_input, "Task name").set_value("Stale edit")
    field(app.checkbox, "I have reviewed these task details.").check()
    workspace.update_task(task_id, {"completed_minutes": 30})
    checked(app)

    assert field(app.number_input, "Completed study time (minutes)").value == 30
    assert field(app.number_input, "Completed study time (minutes)").key != old_key
    assert field(app.text_input, "Task name").value == "Prepare report"
    assert field(app.checkbox, "I have reviewed these task details.").value is False
    assert any("latest details and progress" in message.value for message in app.info)
    assert workspace.tasks()[0]["completed_minutes"] == 30


def test_empty_category_does_not_offer_editor_for_other_category(workspace):
    save_task(workspace, estimated_minutes=30)
    app = checked(AppTest.from_string(APP_SOURCE))
    app.radio(key="tasks_category").set_value("Completed")
    checked(app)
    assert any("No completed tasks" in message.value for message in app.info)
    assert not app.text_input and not app.number_input


def test_task_export_keeps_progress_and_escapes_untrusted_spreadsheet_formulas():
    exported = tasks_csv([{
        "title": "=HYPERLINK('example')", "mail_subject": " +Source", "status": "confirmed",
        "estimated_minutes": 75, "completed_minutes": 30, "due_at": "2026-10-16T17:00:00+08:00",
    }])
    row, = list(csv.DictReader(io.StringIO(exported)))
    assert row["Task"].startswith("'=") and row["Source message"].startswith("' +")
    assert row["Completed (min)"] == "30" and row["Remaining (min)"] == "45"
    assert row["Progress"] == "Unfinished"
