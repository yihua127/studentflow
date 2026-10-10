"""Durable progress, stable task editing, and idempotent daily review transactions."""

import json
from datetime import datetime

import pytest

from studentflow.storage import Store


def task(store, title="Report", estimate=120):
    return store.save_task({"mail_id": "mail-a", "title": title, "estimated_minutes": estimate,
                            "due_at": "2030-01-03T20:00:00+08:00", "status": "confirmed",
                            "source_quote": "Please prepare the report."})


def plan(store, identifier, minutes=60):
    return store.save_plan({
        "inputs": {"settings": {"study_end": "20:00", "timezone": "Asia/Singapore",
                                  "now": "2030-01-01T08:00:00+08:00"}},
        "result": {"blocks": [{"task_id": identifier, "title": "Report", "minutes": minutes,
                                "start": "2030-01-01T18:00:00+08:00",
                                "end": f"2030-01-01T{18 + minutes // 60:02d}:{minutes % 60:02d}:00+08:00"}],
                   "unscheduled": [], "warnings": []},
    })


REVIEW_TIME = datetime.fromisoformat("2030-01-01T20:00:00+08:00")


def test_legacy_records_get_progress_without_rewriting_payload(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    old = {"id": "old", "title": "Report", "estimated_minutes": 90, "status": "confirmed"}
    with store.connect() as db:
        db.execute("INSERT INTO tasks VALUES (?, ?)", ("old", json.dumps(old)))
    result = store.tasks()[0]
    assert result["completed_minutes"] == 0 and result["remaining_minutes"] == 90
    assert result["is_completed"] is False
    with store.connect() as db:
        assert json.loads(db.execute("SELECT payload FROM tasks").fetchone()[0]) == old


def test_edit_rename_reextract_and_restart_preserve_progress_and_evidence(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    identifier = task(store)
    edited = store.update_task(identifier, {"title": "Revised report", "completed_minutes": 45})
    assert edited["id"] == identifier and edited["remaining_minutes"] == 75
    assert edited["source_quote"] == "Please prepare the report."
    assert task(store, "Revised report") == identifier
    restarted = Store(store.path)
    assert restarted.tasks()[0]["completed_minutes"] == 45
    assert task(store, "Report") != identifier
    assert store.tasks()[0]["title"] == "Revised report"


def test_edits_reject_stale_forms_collisions_and_progress_loss(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    identifier = task(store)
    old = store.tasks()[0]
    store.update_task(identifier, {"completed_minutes": 30})
    with pytest.raises(ValueError, match="changed"):
        store.update_task(identifier, {"completed_minutes": 0}, expected_updated_at=old["updated_at"])
    with pytest.raises(ValueError, match="cannot exceed"):
        store.update_task(identifier, {"estimated_minutes": 15})
    with pytest.raises(ValueError, match="cannot be less"):
        task(store, estimate=15)
    task(store, "Other")
    with pytest.raises(ValueError, match="Another task"):
        store.update_task(identifier, {"title": "Other"})
    assert store.tasks()[0]["completed_minutes"] == 30


def test_daily_review_is_atomic_persistent_and_idempotent(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    identifier = task(store)
    plan_id = plan(store, identifier)
    args = ("2030-01-01", plan_id, {identifier: 60})
    result = store.record_daily_review(*args, now=REVIEW_TIME)
    assert result["minutes"] == {identifier: 60}
    assert Store(store.path).record_daily_review(*args, now=REVIEW_TIME) == result
    assert store.tasks()[0]["remaining_minutes"] == 60
    assert store.progress_by_day() == {"2030-01-01": 60}
    with pytest.raises(ValueError, match="already saved"):
        store.record_daily_review("2030-01-01", plan_id, {identifier: 30}, now=REVIEW_TIME)
    assert store.tasks()[0]["remaining_minutes"] == 60


def test_all_done_clamps_to_remaining_after_another_view_logs_progress(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    identifier = task(store)
    plan_id = plan(store, identifier, minutes=90)
    store.update_task(identifier, {"completed_minutes": 100})
    result = store.record_daily_review("2030-01-01", plan_id, {identifier: 90}, now=REVIEW_TIME)
    assert result["minutes"][identifier] == 20
    assert store.tasks()[0]["remaining_minutes"] == 0
    assert store.tasks()[0]["is_completed"] is True


def test_daily_review_rejects_early_unknown_and_negative_progress(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    identifier = task(store)
    plan_id = plan(store, identifier)
    with pytest.raises(ValueError, match="end time"):
        store.record_daily_review("2030-01-01", plan_id, {identifier: 60},
                                  now=datetime.fromisoformat("2030-01-01T19:00:00+08:00"))
    with pytest.raises(ValueError, match="whole minutes"):
        store.record_daily_review("2030-01-01", plan_id, {identifier: -1}, now=REVIEW_TIME)
    with pytest.raises(ValueError, match="tasks in"):
        store.record_daily_review("2030-01-01", plan_id, {"other": 1}, now=REVIEW_TIME)
    assert not store.daily_reviews()
    assert store.tasks()[0]["completed_minutes"] == 0


def test_saving_an_earlier_draft_again_activates_it_and_repeated_click_is_idempotent(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    first = {"signature": "first", "result": {"blocks": []}}
    second = {"signature": "second", "result": {"blocks": []}}
    original_id = store.save_plan(first)
    store.save_plan(second)
    active_id = store.save_plan(first)
    assert active_id != original_id
    assert store.latest_plan()["id"] == active_id
    assert store.save_plan(first) == active_id
    assert len(store.plans()) == 3


def test_plan_save_rechecks_the_task_snapshot_inside_its_transaction(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    identifier = task(store)
    draft = {"inputs": {"tasks": store.tasks()}, "result": {"blocks": []}}
    store.update_task(identifier, {"completed_minutes": 30})
    with pytest.raises(ValueError, match="fresh plan"):
        store.save_plan(draft)
    assert store.plans() == []
