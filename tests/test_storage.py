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
