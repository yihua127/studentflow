"""Saved daily plans, restart recovery, explicit progress and superseding plans."""

import json
from datetime import datetime

import pytest
from streamlit.testing.v1 import AppTest

from studentflow.daily_review import due_daily_reviews, planned_days
from studentflow.storage import Store


def stamp(clock, day="2026-10-01"):
    return f"{day}T{clock}:00+08:00"


def plan(identifier="p1", saved="08:00", day="2026-10-01", blocks=None, study_end="20:00"):
    return {
        "id": identifier,
        "saved_at": stamp(saved, day),
        "inputs": {"settings": {"timezone": "Asia/Singapore", "study_end": study_end}},
        "result": {"blocks": blocks if blocks is not None else [
            {"task_id": "t1", "title": "Essay", "start": stamp("09:00", day), "end": stamp("10:00", day), "minutes": 60}
        ]},
    }


def test_checkin_waits_for_configured_end_not_last_block_end_and_survives_reopen():
    plans = [plan()]
    assert due_daily_reviews(plans, [], datetime.fromisoformat(stamp("19:59"))) == []
    assert len(due_daily_reviews(plans, [], datetime.fromisoformat(stamp("20:00")))) == 1
    assert len(due_daily_reviews(plans, [], datetime.fromisoformat(stamp("08:00", "2026-10-05")))) == 1
    assert due_daily_reviews(plans, [{"date": "2026-10-01"}], datetime.fromisoformat(stamp("21:00"))) == []


def test_replacement_retains_elapsed_work_and_supersedes_future_blocks():
    first = plan(blocks=[
        {"task_id": "t1", "title": "Essay", "start": stamp("09:00"), "end": stamp("10:00"), "minutes": 60},
        {"task_id": "old", "title": "Old plan", "start": stamp("15:00"), "end": stamp("16:00"), "minutes": 60},
    ])
    replacement = plan("p2", saved="09:30", blocks=[
        {"task_id": "urgent", "title": "Urgent", "start": stamp("10:30"), "end": stamp("11:30"), "minutes": 60},
    ], study_end="21:00")
    days = planned_days([first, replacement])
    assert days["2026-10-01"]["minutes"] == {"t1": 30, "urgent": 60}
    assert days["2026-10-01"]["plan_id"] == "p2"
    assert days["2026-10-01"]["due_at"] == stamp("21:00")


def test_new_day_plan_does_not_hide_prior_days_unfinished_checkin():
    days = due_daily_reviews([plan(), plan("p2", day="2026-10-02")], [], datetime.fromisoformat(stamp("21:00", "2026-10-02")))
    assert [day["date"] for day in days] == ["2026-10-01", "2026-10-02"]


def test_multiple_replans_saved_in_one_minute_only_credit_latest_future_sessions():
    first = plan()
    first["saved_at"] = "2026-10-01T08:00:10+08:00"
    second = plan("p2", blocks=[
        {"task_id": "t2", "title": "Replacement", "start": stamp("10:00"), "end": stamp("11:00"), "minutes": 60},
    ])
    second["saved_at"] = "2026-10-01T08:00:50+08:00"
    daily = planned_days([first, second])["2026-10-01"]
    assert daily["minutes"] == {"t2": 60}
    assert daily["plan_id"] == "p2"


def setup_store(tmp_path, estimate=120):
    store = Store(tmp_path / "tasks.sqlite3")
    task_id = store.save_task({"title": "Essay", "estimated_minutes": estimate, "due_at": stamp("22:00", "2026-10-20"), "status": "confirmed"})
    saved = plan()
    saved["result"]["blocks"][0]["task_id"] = task_id
    with store.connect() as db:
        db.execute("INSERT INTO plans VALUES (?, ?)", ("p1", json.dumps(saved)))
    return store, task_id


def test_progress_clamps_remaining_is_atomic_idempotent_and_survives_restart(tmp_path):
    store, task_id = setup_store(tmp_path, estimate=40)
    saved = store.record_daily_review("2026-10-01", "p1", {task_id: 60}, now=datetime.fromisoformat(stamp("21:00")))
    assert saved["minutes"][task_id] == 40
    assert store.tasks()[0]["is_completed"]
    reopened = Store(tmp_path / "tasks.sqlite3")
    assert reopened.record_daily_review("2026-10-01", "p1", {task_id: 60}, now=datetime.fromisoformat(stamp("21:00"))) == saved
    assert reopened.tasks()[0]["completed_minutes"] == 40
    assert reopened.progress_by_day() == {"2026-10-01": 40}
    with pytest.raises(ValueError, match="already saved"):
        reopened.record_daily_review("2026-10-01", "p1", {task_id: 30})


def test_partial_progress_allows_extra_work_and_rejects_premature_or_unknown(tmp_path):
    store, task_id = setup_store(tmp_path)
    with pytest.raises(ValueError, match="end time"):
        store.record_daily_review("2026-10-01", "p1", {task_id: 30}, now=datetime.fromisoformat(stamp("19:00")))
    with pytest.raises(ValueError, match="tasks in"):
        store.record_daily_review("2026-10-01", "p1", {"missing": 30}, now=datetime.fromisoformat(stamp("21:00")))
    assert store.tasks()[0]["completed_minutes"] == 0
    saved = store.record_daily_review("2026-10-01", "p1", {task_id: 90}, now=datetime.fromisoformat(stamp("21:00")))
    assert saved["minutes"][task_id] == 90
    assert store.tasks()[0]["remaining_minutes"] == 30


def test_daily_review_ui_requires_answer_and_records_partial_minutes(tmp_path, monkeypatch):
    store, task_id = setup_store(tmp_path)
    monkeypatch.setenv("STUDENTFLOW_CHECKIN_TEST", str(tmp_path))
    script = """
import os
from pathlib import Path
from studentflow.daily_review import render_daily_review
from studentflow.storage import Store
path = Path(os.environ['STUDENTFLOW_CHECKIN_TEST'])
render_daily_review(Store(path / 'tasks.sqlite3'), path)
"""
    app = AppTest.from_string(script).run(timeout=30)
    assert not app.exception
    assert app.button(key="sf_checkin_save_2026-10-01").disabled
    app.radio(key="sf_checkin_answer_2026-10-01").set_value("No, enter completed minutes").run()
    app.number_input(key=f"sf_checkin_minutes_2026-10-01_{task_id}").set_value(25)
    app.button(key="sf_checkin_save_2026-10-01").click().run()
    assert not app.exception
    assert store.tasks()[0]["completed_minutes"] == 25
    assert app.button(key="sf_checkin_replan")
    reopened = AppTest.from_string(script).run()
    assert not reopened.exception
    assert len(reopened.radio) == 0
