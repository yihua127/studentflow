"""Exercise recurring and exceptional availability through the user interface."""

from datetime import datetime, time, timedelta

from streamlit.testing.v1 import AppTest

from studentflow.availability import (
    WEEKDAYS,
    ZONE,
    dated_override,
    resolve_availability,
    weekly_rule,
)
from studentflow.planning_ui import (
    default_preferences,
    make_draft,
    read_preferences,
    window,
    write_preferences,
)
from studentflow.storage import Store


def app_for(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDENTFLOW_AVAILABILITY_TEST_DATA", str(tmp_path))
    return AppTest.from_string("""
import os
from pathlib import Path
from studentflow.planning_ui import render_planner
from studentflow.storage import Store
path = Path(os.environ['STUDENTFLOW_AVAILABILITY_TEST_DATA'])
store = Store(path / 'tasks.sqlite3')
render_planner(store.tasks(), store, path)
""").run(timeout=30)


def click(app, label):
    next(button for button in app.button if button.label == label).click().run(timeout=30)
    assert not app.exception


def test_weekly_rule_add_edit_batch_remove_and_horizon(tmp_path, monkeypatch):
    app = app_for(tmp_path, monkeypatch)
    assert not app.exception
    assert not (tmp_path / "planning_preferences.json").exists()
    app.multiselect(key="sf_plan_weekdays").set_value(["Monday", "Wednesday"])
    click(app, "Add weekly rule")
    preferences = read_preferences(tmp_path / "planning_preferences.json")
    rule = preferences["weekly_availability"][0]
    assert rule["weekdays"] == [0, 2]
    assert preferences["available_slots"] == []
    app.multiselect(key="sf_plan_edit_days_" + rule["id"]).set_value(["Tuesday", "Thursday"])
    app.time_input(key="sf_plan_edit_end_" + rule["id"]).set_value(time(17))
    click(app, "Save weekly rule")
    changed = read_preferences(tmp_path / "planning_preferences.json")["weekly_availability"][0]
    assert changed["weekdays"] == [1, 3]
    assert changed["end"] == "17:00"
    app.number_input(key="sf_plan_horizon_days").set_value(14)
    click(app, "Save planning horizon")
    assert (
        read_preferences(tmp_path / "planning_preferences.json")["availability_horizon_days"] == 14
    )
    app.multiselect(key="sf_plan_remove_weekly").set_value([rule["id"]]).run()
    click(app, "Remove selected weekly rules")
    assert read_preferences(tmp_path / "planning_preferences.json")["weekly_availability"] == []


def test_day_exclusions_and_added_time_can_be_reviewed_and_removed_together(tmp_path, monkeypatch):
    tomorrow = datetime.now(ZONE).date() + timedelta(days=1)
    preferences = default_preferences()
    preferences["weekly_availability"] = [weekly_rule([tomorrow.weekday()], time(9), time(18))]
    write_preferences(tmp_path / "planning_preferences.json", preferences)
    app = app_for(tmp_path, monkeypatch)
    app.selectbox(key="sf_plan_override_kind").set_value("Remove available time").run()
    app.date_input(key="sf_plan_slot_day").set_value(tomorrow)
    app.time_input(key="sf_plan_slot_start").set_value(time(10))
    app.time_input(key="sf_plan_slot_end").set_value(time(11))
    click(app, "Remove available time")
    app.selectbox(key="sf_plan_override_kind").set_value("Whole day unavailable").run()
    click(app, "Whole day unavailable")
    app.selectbox(key="sf_plan_override_kind").set_value("Add available time").run()
    app.time_input(key="sf_plan_slot_start").set_value(time(18))
    app.time_input(key="sf_plan_slot_end").set_value(time(20))
    click(app, "Add available time")
    saved = read_preferences(tmp_path / "planning_preferences.json")
    assert [entry["mode"] for entry in saved["availability_overrides"]] == ["remove", "off", "add"]
    intervals = resolve_availability(saved, datetime.combine(tomorrow, time(8), ZONE))
    assert all(item["start"][:10] != tomorrow.isoformat() for item in intervals)
    chosen = ["change:" + entry["id"] for entry in saved["availability_overrides"]]
    app.multiselect(key="sf_plan_remove_slots").set_value(chosen).run()
    click(app, "Remove selected time")
    saved = read_preferences(tmp_path / "planning_preferences.json")
    assert saved["availability_overrides"] == []
    assert resolve_availability(saved, datetime.combine(tomorrow, time(8), ZONE))[0][
        "start"
    ].endswith("T09:00:00+08:00")


def test_render_prunes_expired_dates_but_preserves_saved_plan_and_future_windows(
    tmp_path, monkeypatch
):
    today = datetime.now(ZONE).date()
    yesterday, tomorrow = today - timedelta(days=1), today + timedelta(days=1)
    preferences = default_preferences()
    preferences["available_slots"] = [
        window(yesterday, time(9), time(12)),
        window(tomorrow, time(9), time(12)),
    ]
    preferences["availability_overrides"] = [dated_override(yesterday, "off")]
    preferences["weekly_availability"] = [weekly_rule([tomorrow.weekday()], time(14), time(16))]
    write_preferences(tmp_path / "planning_preferences.json", preferences)
    store = Store(tmp_path / "tasks.sqlite3")
    task = {
        "title": "Synthetic study task",
        "status": "confirmed",
        "estimated_minutes": 30,
        "due_at": datetime.combine(tomorrow, time(20), ZONE).isoformat(),
    }
    store.save_task(task)
    draft = make_draft(
        store.tasks(), preferences, datetime.combine(today, time(8), ZONE).isoformat(), {}
    )
    store.save_plan(draft)
    historic = store.latest_plan()
    app = app_for(tmp_path, monkeypatch)
    assert not app.exception
    after = read_preferences(tmp_path / "planning_preferences.json")
    assert after["available_slots"] == [preferences["available_slots"][1]]
    assert after["availability_overrides"] == []
    assert after["weekly_availability"] == preferences["weekly_availability"]
    assert store.latest_plan() == historic


def test_weekly_only_availability_enables_generation_and_changed_rule_invalidates_draft(
    tmp_path, monkeypatch
):
    today = datetime.now(ZONE).date()
    tomorrow = today + timedelta(days=1)
    store = Store(tmp_path / "tasks.sqlite3")
    store.save_task(
        {
            "title": "Synthetic study task",
            "status": "confirmed",
            "estimated_minutes": 30,
            "due_at": datetime.combine(tomorrow, time(20), ZONE).isoformat(),
        }
    )
    app = app_for(tmp_path, monkeypatch)
    app.multiselect(key="sf_plan_weekdays").set_value([WEEKDAYS[tomorrow.weekday()]])
    click(app, "Add weekly rule")
    assert not next(
        button for button in app.button if button.label == "Generate study plan"
    ).disabled
    click(app, "Generate study plan")
    assert any(button.label == "Save plan" for button in app.button)
    rule = read_preferences(tmp_path / "planning_preferences.json")["weekly_availability"][0]
    app.time_input(key="sf_plan_edit_start_" + rule["id"]).set_value(time(10))
    click(app, "Save weekly rule")
    assert any("Generate a fresh plan" in item.value for item in app.info)
    assert all(button.label != "Save plan" for button in app.button)
