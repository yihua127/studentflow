"""Personal planning preferences and explicit draft confirmation."""

import json
from datetime import datetime, time, timedelta

import pytest
from streamlit.testing.v1 import AppTest

from studentflow.planning_ui import (
    ZONE,
    default_preferences,
    planning_signature,
    read_preferences,
    schedule_csv,
    window,
    write_preferences,
)
from studentflow.storage import Store


def test_preferences_start_empty_and_persist_only_supplied_windows(tmp_path):
    path = tmp_path / "planning_preferences.json"
    preferences = read_preferences(path)
    assert not path.exists()
    assert preferences["available_slots"] == []
    assert preferences["busy_events"] == []
    day = datetime.now(ZONE).date() + timedelta(days=1)
    preferences["available_slots"].append(window(day, time(9), time(11)))
    write_preferences(path, preferences)
    assert read_preferences(path) == preferences
    assert list(tmp_path.glob("planning-*.tmp")) == []
    assert default_preferences()["available_slots"] == []


def test_invalid_window_and_invalid_saved_preferences_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="End time"):
        window(datetime.now(ZONE).date(), time(11), time(9))
    path = tmp_path / "planning_preferences.json"
    preferences = default_preferences()
    preferences["settings"]["daily_limit_minutes"] = 500
    path.write_text(json.dumps(preferences), encoding="utf-8")
    with pytest.raises(ValueError, match="could not be loaded"):
        read_preferences(path)


def test_task_changes_and_time_changes_invalidate_signature():
    preferences = default_preferences()
    tasks = [{"title": "Essay", "estimated_minutes": 60}]
    initial = planning_signature(tasks, preferences, "2030-01-01T09:00:00+08:00")
    tasks[0]["estimated_minutes"] = 90
    assert initial != planning_signature(tasks, preferences, "2030-01-01T09:00:00+08:00")
    tasks[0]["estimated_minutes"] = 60
    assert initial != planning_signature(tasks, preferences, "2030-01-01T09:01:00+08:00")


def test_export_contains_readable_columns_and_neutralizes_formula_titles():
    result = schedule_csv(
        [
            {
                "title": '=HYPERLINK("example")',
                "start": "2030-01-01T09:00:00+08:00",
                "end": "2030-01-01T10:00:00+08:00",
                "minutes": 60,
                "task_id": "internal",
            }
        ]
    )
    assert "Task,Date,Start,End,Duration (min)" in result
    assert "'=HYPERLINK" in result
    assert "internal" not in result


def test_plan_requires_personal_availability_and_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDENTFLOW_TEST_PLAN_DATA", str(tmp_path))
    day = datetime.now(ZONE).date() + timedelta(days=1)
    store = Store(tmp_path / "tasks.sqlite3")
    store.save_task(
        {
            "title": "Prepare report",
            "status": "confirmed",
            "estimated_minutes": 60,
            "due_at": datetime.combine(day, time(21), ZONE).isoformat(),
        }
    )
    app = AppTest.from_string("""
import os
from pathlib import Path
from studentflow.planning_ui import render_planner
from studentflow.storage import Store
path = Path(os.environ['STUDENTFLOW_TEST_PLAN_DATA'])
store = Store(path / 'tasks.sqlite3')
render_planner(store.tasks(), store, path)
""")

    def run():
        app.run(timeout=30)
        assert not app.exception

    def button(label):
        return next(item for item in app.button if item.label == label)

    run()
    assert button("Generate study plan").disabled
    assert not (tmp_path / "planning_preferences.json").exists()
    app.date_input(key="sf_plan_slot_day").set_value(day)
    button("Add available time").click()
    run()
    assert not button("Generate study plan").disabled
    button("Generate study plan").click()
    run()
    assert button("Save plan").disabled
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
    next(item for item in app.checkbox if item.label.startswith("I have reviewed")).check()
    run()
    button("Save plan").click()
    run()
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 1
    button("Remove selected time").click()
    run()
    assert button("Generate study plan").disabled
    assert all(item.label != "Save plan" for item in app.button)
