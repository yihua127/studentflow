import sqlite3

import pytest

from studentflow.storage import Store


def test_confirmed_task_survives_restart_and_same_source_updates(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    first = {"mail_id": "source-a", "title": "Submit", "due_at": None, "status": "pending"}
    identifier = store.save_task(first)
    store.save_task({**first, "due_at": "2026-10-15T10:00:00+08:00", "status": "confirmed"})
    restarted = Store(path)
    assert len(restarted.tasks()) == 1
    assert restarted.tasks()[0]["id"] == identifier
    assert restarted.tasks()[0]["status"] == "confirmed"
    assert restarted.tasks()[0]["due_at"] == "2026-10-15T10:00:00+08:00"
    store.save_task({**first, "mail_id": "source-b"})
    assert len(store.tasks()) == 2


def test_batch_save_rolls_back_all_changes_on_database_failure(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    original = {"mail_id": "a", "title": "Submit", "status": "pending"}
    store.save_task(original)
    with store.connect() as db:
        db.execute("""
            CREATE TRIGGER reject_second BEFORE INSERT ON tasks
            WHEN json_extract(NEW.payload, '$.title') = 'Rejected'
            BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END
        """)
    with pytest.raises(sqlite3.IntegrityError):
        store.save_tasks([
            {**original, "status": "confirmed"},
            {"mail_id": "b", "title": "Rejected"},
        ])
    assert len(store.tasks()) == 1
    assert store.tasks()[0]["status"] == "pending"


def test_batch_reconfirmation_updates_matching_tasks_and_preserves_sources(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    records = [{"mail_id": source, "title": "Submit", "status": "pending"} for source in ("a", "b")]
    identifiers = store.save_tasks(records)
    assert len(set(identifiers)) == 2
    assert store.save_tasks([{**record, "status": "confirmed"} for record in records]) == identifiers
    assert all(task["status"] == "confirmed" for task in Store(store.path).tasks())
