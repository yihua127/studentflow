"""Overview reads saved facts and navigates without changing tasks or calendars."""

from datetime import date, datetime

from streamlit.testing.v1 import AppTest

from studentflow.overview_ui import (
    ZONE,
    _event_html,
    calendar_items,
    items_for_day,
    open_tasks,
    shift_month,
)
from studentflow.storage import Store


def task(identifier, **changes):
    return {"id": identifier, "title": identifier, "estimated_minutes": 60,
            "completed_minutes": 0, "due_at": None, "status": "confirmed", **changes}


def test_calendar_uses_local_dates_and_midnight_exclusive_overlap():
    items = calendar_items([], None, [{
        "title": "Overnight workshop", "start_at": "2030-01-05T15:00:00Z",
        "end_at": "2030-01-06T16:00:00Z",
    }], [])
    assert len(items_for_day(items, date(2030, 1, 5))) == 1
    assert len(items_for_day(items, date(2030, 1, 6))) == 1
    assert items_for_day(items, date(2030, 1, 7)) == []
    assert items[0]["start"].hour == 23
    assert "Continues–24:00" in _event_html(items[0], date(2030, 1, 6))


def test_calendar_excludes_unknown_deadlines_and_preserves_saved_kinds():
    tasks = [task("due", due_at="2030-01-05T10:00:00+08:00"),
             task("done", completed_minutes=60, due_at="2030-01-05T11:00:00+08:00"),
             task("unknown"), task("naive", due_at="2030-01-05T12:00:00")]
    plan = {"result": {"blocks": [{"title": "Study", "start": "2030-01-05T08:00:00+08:00",
                                    "end": "2030-01-05T09:00:00+08:00"}]}}
    events = [{"title": "Bad time", "start_at": "bad", "end_at": "2030-01-05T12:00:00+08:00"}]
    commitments = [{"title": "Team check-in", "start": "2030-01-05T15:00:00+08:00",
                    "end": "2030-01-05T16:00:00+08:00"}]
    items = calendar_items(tasks, plan, events, commitments)
    assert [item["kind"] for item in items] == ["Saved study", "Deadline", "Commitment"]
    assert [item["title"] for item in items] == ["Study", "due", "Team check-in"]


def test_todos_exclude_completed_keep_unknown_and_sort_deadlines():
    tasks = [task("unknown", estimated_minutes=None), task("finished", completed_minutes=60),
             task("later", due_at="2030-01-06T10:00:00+08:00"),
             task("earlier", due_at="2030-01-05T10:00:00+08:00")]
    assert [item["id"] for item in open_tasks(tasks)] == ["earlier", "later", "unknown"]


def test_month_navigation_crosses_year_and_leap_months():
    assert shift_month(date(2030, 12, 1), 1) == date(2031, 1, 1)
    assert shift_month(date(2030, 1, 1), -1) == date(2029, 12, 1)
    assert shift_month(date(2032, 3, 1), -1) == date(2032, 2, 1)


def test_calendar_title_is_text_not_markup():
    value = _event_html({"title": '<img src=x onerror="alert(1)"> & plan',
                         "kind": "Saved study", "start": datetime(2030, 1, 5, 9, tzinfo=ZONE),
                         "end": datetime(2030, 1, 5, 10, tzinfo=ZONE)}, date(2030, 1, 5))
    assert "<img" not in value
    assert "&lt;img" in value and "&amp; plan" in value


APP = """
import os
from pathlib import Path
import streamlit as st
from studentflow.overview_ui import render_overview_schedule
from studentflow.storage import Store
path = Path(os.environ['STUDENTFLOW_OVERVIEW_TEST'])
render_overview_schedule(Store(path / 'tasks.sqlite3'), path)
"""


def test_task_shortcut_clears_old_filters_and_does_not_mutate_data(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDENTFLOW_OVERVIEW_TEST", str(tmp_path))
    store = Store(tmp_path / "tasks.sqlite3")
    identifier = store.save_task({"title": "Prepare report", "due_at": "2030-01-05T18:00:00+08:00",
                                  "estimated_minutes": 60, "status": "confirmed", "mail_id": "mail"})
    before = store.tasks()
    app = AppTest.from_string(APP).run(timeout=30)
    assert not app.exception
    app.session_state.sf_task_search = "unrelated"
    app.session_state.sf_task_status = "Needs details"
    app.button(key="sf_overview_open_" + identifier).click().run(timeout=30)
    assert not app.exception
    assert app.session_state.sf_app_pending_tab == "Tasks"
    assert app.session_state.tasks_next_selection == identifier
    assert app.session_state.tasks_next_category == "Unfinished"
    assert app.session_state.sf_task_search == ""
    assert app.session_state.sf_task_status == "All tasks"
    assert store.tasks() == before and store.latest_plan() is None


def test_calendar_date_selection_and_month_navigation_are_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDENTFLOW_OVERVIEW_TEST", str(tmp_path))
    app = AppTest.from_string(APP)
    app.session_state.sf_cal_month = "2032-02-01"
    app.session_state.sf_cal_selected = "2032-02-01"
    app.run(timeout=30)
    assert not app.exception
    app.button(key="sf_cal_day_2032-02-29").click().run(timeout=30)
    assert app.session_state.sf_cal_selected == "2032-02-29"
    app.button(key="sf_cal_next").click().run(timeout=30)
    assert not app.exception
    assert app.session_state.sf_cal_month == "2032-03-01"
    assert app.session_state.sf_cal_selected == "2032-03-01"
    assert Store(tmp_path / "tasks.sqlite3").tasks() == []
    assert not (tmp_path / "planning_preferences.json").exists()
