"""Conditional workspace navigation preserves context without carrying approval."""

from datetime import datetime, time, timedelta

import pytest
from streamlit.testing.v1 import AppTest

from studentflow import planning_ui
from studentflow.planning_ui import ZONE, default_preferences, window, write_preferences
from studentflow.storage import Store

APP = """
import os
from pathlib import Path
import streamlit as st
from studentflow.storage import Store
from studentflow.tasks_ui import preserve_task_inputs, render_tasks
from studentflow.planning_ui import preserve_planning_inputs, render_planner
preserve_task_inputs()
preserve_planning_inputs()
path = Path(os.environ['STUDENTFLOW_TEST_NAVIGATION'])
store = Store(path / 'tasks.sqlite3')
page = st.radio('Workspace', ['Tasks', 'Study Plan', 'Other'], key='test_page')
if page == 'Tasks':
    render_tasks(store)
elif page == 'Study Plan':
    render_planner(store.tasks(), store, path)
else:
    st.write('Another workspace')
"""


def field(elements, label):
    return next(element for element in elements if element.label == label)


def checked(app):
    app.run(timeout=30)
    assert not app.exception
    return app


def navigate(app, page):
    app.radio(key="test_page").set_value(page)
    return checked(app)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDENTFLOW_TEST_NAVIGATION", str(tmp_path))
    return Store(tmp_path / "tasks.sqlite3")


def save_task(store, title, source, **changes):
    return store.save_task({
        "title": title, "mail_id": source, "mail_subject": source,
        "source_quote": "Evidence for " + title,
        "due_at": "2030-01-16T18:00:00+08:00", "estimated_minutes": 60,
        "status": "confirmed", **changes,
    })


def test_task_browser_returns_to_correct_card_without_retaining_approval(workspace):
    save_task(workspace, "Report outline", "Outline request")
    target = save_task(workspace, "Report revision", "Revision request")
    save_task(workspace, "Reading notes", "Reading request")
    before = workspace.tasks()
    app = checked(AppTest.from_string(APP))
    app.text_input(key="sf_task_search").set_value("Report")
    app.selectbox(key="sf_task_status").set_value("Ready to plan")
    checked(app)
    app.button(key=f"sf_task_open_{target}").click()
    checked(app)
    assert field(app.text_input, "Task name").value == "Report revision"
    assert "Evidence for Report revision" in {
        item.value for item in field(app.expander, "Original supporting text").text
    }

    # These values reached the server; unsent browser form drafts are not modeled.
    field(app.checkbox, "I have reviewed these task details.").check()
    field(app.checkbox, "I want to delete this task.").check()
    checked(app)
    navigate(app, "Other")
    navigate(app, "Tasks")

    assert app.text_input(key="sf_task_search").value == "Report"
    assert app.selectbox(key="sf_task_status").value == "Ready to plan"
    assert field(app.selectbox, "Edit task").value == target
    assert field(app.text_input, "Task name").value == "Report revision"
    assert field(app.checkbox, "I have reviewed these task details.").value is False
    assert field(app.checkbox, "I want to delete this task.").value is False
    assert field(app.button, "Delete task").disabled
    assert workspace.tasks() == before


def test_task_changed_while_away_uses_latest_revision_and_original_evidence(workspace):
    target = save_task(workspace, "Report revision", "Revision request")
    app = checked(AppTest.from_string(APP))
    field(app.text_input, "Task name").set_value("Stale received edit")
    field(app.checkbox, "I have reviewed these task details.").check()
    checked(app)
    navigate(app, "Other")
    workspace.update_task(target, {"completed_minutes": 30})
    navigate(app, "Tasks")

    assert field(app.text_input, "Task name").value == "Report revision"
    assert field(app.number_input, "Completed study time (minutes)").value == 30
    assert field(app.checkbox, "I have reviewed these task details.").value is False
    assert "Evidence for Report revision" in {
        item.value for item in field(app.expander, "Original supporting text").text
    }
    assert any("latest details and progress" in item.value for item in app.info)


def test_plan_draft_returns_without_approval_or_implicit_save(workspace, tmp_path, monkeypatch):
    instant = datetime(2030, 1, 14, 8, tzinfo=ZONE)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(planning_ui, "datetime", FrozenDatetime)
    save_task(workspace, "Report revision", "Revision request")
    preferences = default_preferences()
    preferences["available_slots"] = [
        window(instant.date() + timedelta(days=1), time(9), time(11)),
    ]
    write_preferences(tmp_path / "planning_preferences.json", preferences)
    app = checked(AppTest.from_string(APP))
    navigate(app, "Study Plan")
    app.selectbox(key="sf_plan_override_kind").set_value("Remove available time")
    checked(app)
    field(app.button, "Generate study plan").click()
    checked(app)
    original_draft = app.session_state["planner_draft"]
    assert original_draft["result"]["blocks"]
    review = "I have reviewed the schedule and any unscheduled work."
    field(app.checkbox, review).check()
    checked(app)
    assert not field(app.button, "Save plan").disabled

    navigate(app, "Other")
    navigate(app, "Study Plan")
    assert app.selectbox(key="sf_plan_override_kind").value == "Remove available time"
    assert app.session_state["planner_draft"] == original_draft
    assert field(app.checkbox, review).value is False
    assert field(app.button, "Save plan").disabled
    assert workspace.latest_plan() is None

    field(app.checkbox, review).check()
    checked(app)
    field(app.button, "Save plan").click()
    checked(app)
    assert workspace.latest_plan()["result"]["blocks"] == original_draft["result"]["blocks"]
