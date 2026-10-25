from copy import deepcopy

from streamlit.testing.v1 import AppTest

from studentflow import fixed_events_ui
from studentflow.fixed_events import validate_fixed_events
from studentflow.storage import Store

ACCOUNT = "student@example.com"
QUOTE = "DSS5105 exam on 28 October 2026 from 14:00 to 16:00 Singapore time."


def event_result():
    return validate_fixed_events({"events": [{
        "title": "DSS5105 exam", "event_key": "DSS5105 exam", "course": "DSS5105", "kind": "exam",
        "start_at": "2026-10-28T14:00:00+08:00", "end_at": "2026-10-28T16:00:00+08:00",
        "start_evidence": "14:00", "end_evidence": "16:00", "duration_minutes": None,
        "sources": [{"source_id": "body", "source_quote": QUOTE}],
    }]}, [{"id": "body", "text": QUOTE, "account": ACCOUNT, "mail_id": "mail-a"}])


APP = '''
from pathlib import Path
import os
import streamlit as st
from studentflow.fixed_events_ui import render_fixed_event_extraction
from studentflow.storage import Store
account = st.selectbox('Account', ['student@example.com', 'another@example.com'])
mail = {'id': 'mail-a', 'gmail_account': account, 'body': 'Exam source', 'subject': 'Exam'}
render_fixed_event_extraction([mail], Store(Path(os.environ['FIXED_EVENT_TEST_DB'])),
    Path('.'), 'fake-key', 'fake-model', account=account)
'''


def button(app, label):
    return next(item for item in app.button if item.label == label)


def test_explicit_extraction_human_review_and_idempotent_save(tmp_path, monkeypatch):
    database = tmp_path / "events.sqlite3"
    monkeypatch.setenv("FIXED_EVENT_TEST_DB", str(database))
    calls = []

    def extract(*args, **kwargs):
        calls.append(kwargs["account"])
        return deepcopy(event_result())

    monkeypatch.setattr(fixed_events_ui, "extract_fixed_events", extract)
    app = AppTest.from_string(APP).run(timeout=30)
    assert not app.exception
    assert calls == [] and button(app, "Extract fixed events").disabled
    app.checkbox[0].check().run()
    button(app, "Extract fixed events").click().run(timeout=30)
    assert not app.exception
    assert calls == [ACCOUNT]
    assert Store(database).list_fixed_events() == []
    assert button(app, "Save selected fixed events").disabled
    review_checkbox = next(item for item in app.checkbox if "checked the event dates" in item.label)
    editor_key = review_checkbox.key.removesuffix("_reviewed") + "_rows"
    review_checkbox.check().run()
    app.session_state[editor_key] = {"edited_rows": {0: {"Include": True}}, "added_rows": [], "deleted_rows": []}
    button(app, "Save selected fixed events").click().run()
    assert not app.exception
    assert len(Store(database).list_fixed_events(ACCOUNT)) == 1, (
        [item.value for item in app.error], [item.value for item in app.warning])
    button(app, "Save selected fixed events").click().run()
    assert len(Store(database).list_fixed_events(ACCOUNT)) == 1
    assert calls == [ACCOUNT]
    app.selectbox[0].select("another@example.com").run()
    assert not app.exception
    assert app.checkbox[0].value is False
    assert not any(item.label == "Save selected fixed events" for item in app.button)
    assert calls == [ACCOUNT]


def test_saved_event_editor_corrects_time_and_removes_only_local_fixture(tmp_path, monkeypatch):
    database = tmp_path / "events.sqlite3"
    monkeypatch.setenv("FIXED_EVENT_TEST_DB", str(database))
    store = Store(database)
    identifier = store.save_fixed_events(event_result()["events"])[0]
    source = '''
from pathlib import Path
import os
from studentflow.fixed_events_ui import render_fixed_events
from studentflow.storage import Store
render_fixed_events(Store(Path(os.environ['FIXED_EVENT_TEST_DB'])), account='student@example.com')
'''
    app = AppTest.from_string(source).run(timeout=30)
    assert not app.exception
    next(item for item in app.text_input if item.label == "Start (Singapore)").set_value("2026-10-28 15:00")
    next(item for item in app.text_input if item.label == "End (Singapore)").set_value("2026-10-28 17:00")
    next(item for item in app.checkbox if item.label == "I have reviewed these event details.").check()
    button(app, "Save event changes").click().run()
    assert not app.exception
    assert store.list_fixed_events(ACCOUNT)[0]["id"] == identifier
    assert store.list_fixed_events(ACCOUNT)[0]["start_at"] == "2026-10-28T15:00:00+08:00"
    next(item for item in app.checkbox if item.label == "Remove this fixed event from StudentFlow.").check().run()
    button(app, "Remove fixed event").click().run()
    assert not app.exception
    assert store.list_fixed_events(ACCOUNT) == []


def test_old_fixed_appointment_task_preview_is_blocked_without_changing_saved_tasks(tmp_path, monkeypatch):
    database = tmp_path / "events.sqlite3"
    monkeypatch.setenv("FIXED_EVENT_TEST_DB", str(database))
    store = Store(database)
    store.save_task({"title": "Previously reviewed task", "due_at": "2026-10-28T14:00:00+08:00",
                     "estimated_minutes": 30, "status": "confirmed"})
    before = store.tasks()
    source = '''
from pathlib import Path
import os
import streamlit as st
from studentflow.batch_ui import render_batch_extraction
from studentflow.storage import Store
st.session_state.setdefault('mails', {})
st.session_state.setdefault('candidates', {})
st.session_state.setdefault('gmail_profile', {'email_address': 'student@example.com'})
render_batch_extraction([{'id': 'a', 'subject': 'Exam', 'sender': 'teacher@example.com'}], Path('fake-client'), Path('fake-token'),
    Store(Path(os.environ['FIXED_EVENT_TEST_DB'])), 'fake-key', 'fake-model', True)
'''
    app = AppTest.from_string(source)
    app.session_state["batch_results"] = [{"result": {"tasks": [{"title": "Attend old exam"}]}}]
    app.session_state["batch_results_version"] = "source-pipeline-v2"
    app.run(timeout=30)
    assert not app.exception
    assert any("Task extraction has been updated" in item.value for item in app.info)
    assert not any(item.label == "Save selected tasks" for item in app.button)
    assert store.tasks() == before
