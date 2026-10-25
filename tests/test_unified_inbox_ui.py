"""One explicit Inbox analysis classifies tasks and appointments for separate review."""

from copy import deepcopy
from email.message import EmailMessage
from sqlite3 import OperationalError

import pytest
from streamlit.testing.v1 import AppTest

from studentflow import batch_ui
from studentflow.extraction import REVIEW_NOTICE
from studentflow.fixed_events import prepare_sources, validate_fixed_events
from studentflow.storage import Store

ACCOUNT = "student@example.com"
PROFILE = {"email_address": ACCOUNT}
EXAM = "The mock exam is on 26 October 2026 from 10:00 to 11:00 Singapore time, online."
PRESENTATION = "The mock presentation is on 28 October 2026 from 10:00 to 10:20 Singapore time, online."
PREPARATION = "Prepare for the mock exam by 25 October 2026 at 18:00. Allow 45 minutes."
SLIDES = "Prepare the mock presentation slides by 27 October 2026 at 18:00. Allow 30 minutes."
SUBJECTS = {
    "mixed": "StudentFlow TEST 03 - Fixed events and preparation",
    "event-only": "Mock exam appointment",
    "other": "A different message",
}
BODIES = {
    "mixed": f"{EXAM}\n{PRESENTATION}\n{PREPARATION}\n{SLIDES}",
    "event-only": EXAM,
    "other": "No assignments or appointments in this message.",
}

APP_SOURCE = '''
from pathlib import Path
import os
import streamlit as st
from studentflow.batch_ui import render_batch_extraction
from studentflow.storage import Store
st.session_state.setdefault('mails', {})
st.session_state.setdefault('candidates', {})
st.session_state.setdefault('gmail_profile', {'email_address': 'student@example.com'})
model = st.selectbox('Test model', ['model-test', 'model-other'], key='test_model')
headers = [
    {'id': 'mixed', 'subject': 'StudentFlow TEST 03 - Fixed events and preparation', 'sender': 'teacher@example.com'},
    {'id': 'event-only', 'subject': 'Mock exam appointment', 'sender': 'teacher@example.com'},
    {'id': 'other', 'subject': 'A different message', 'sender': 'teacher@example.com'},
]
render_batch_extraction(headers, Path('fake-client'), Path('fake-token'),
    Store(Path(os.environ['UNIFIED_INBOX_TEST_DB'])), 'test-key', model, True)
'''


def _message(identifier):
    mail = EmailMessage()
    mail["Subject"] = SUBJECTS[identifier]
    mail["From"] = "teacher@example.com"
    mail["Date"] = "Sat, 24 Oct 2026 09:00:00 +0800"
    mail.set_content(BODIES[identifier])
    return mail.as_bytes()


def _event_result(mail):
    proposals = []
    for quote, title, kind, date, end in [
        (EXAM, "Mock exam", "exam", "2026-10-26", "11:00"),
        (PRESENTATION, "Mock presentation", "presentation", "2026-10-28", "10:20"),
    ]:
        if quote not in mail["body"]:
            continue
        proposals.append({
            "title": title, "event_key": title, "kind": kind, "course": "Mock course",
            "start_at": f"{date}T10:00:00+08:00", "end_at": f"{date}T{end}:00+08:00",
            "start_evidence": "10:00", "end_evidence": end, "in_person": False,
            "duration_minutes": None, "sources": [{"source_id": "body", "source_quote": quote}],
        })
    sources, _ = prepare_sources(mail, ACCOUNT)
    return validate_fixed_events({"events": proposals}, sources)


def _setup(tmp_path, monkeypatch, *, fixed_failure=False):
    database = tmp_path / "records.sqlite3"
    monkeypatch.setenv("UNIFIED_INBOX_TEST_DB", str(database))
    calls = []

    class Mailbox:
        def profile(self):
            calls.append(("profile",))
            return PROFILE

        def fetch_eml(self, identifier):
            calls.append(("fetch", identifier))
            return _message(identifier)

        def close(self):
            calls.append(("close",))

    def extract_tasks(body, subject, sent_at, api_key, model):
        calls.append(("tasks", subject, model))
        tasks = []
        for quote, title, due, minutes in [
            (PREPARATION, "Prepare for mock exam", "2026-10-25T18:00:00+08:00", 45),
            (SLIDES, "Prepare mock presentation slides", "2026-10-27T18:00:00+08:00", 30),
        ]:
            if quote in body:
                tasks.append({
                    "title": title, "due_at": due, "estimated_minutes": minutes,
                    "source_quote": quote, "confidence": 0.9, "needs_clarification": [],
                })
        return {"tasks": tasks, "warnings": [REVIEW_NOTICE]}

    def analyze_events(mails, account, api_key, model, cache, *, reanalyze=False, extractor=None):
        calls.append(("events", tuple(mail["gmail_id"] for mail in mails), model))
        assert account == ACCOUNT
        if fixed_failure:
            return {
                "events": [], "warnings": [],
                "results": [{"mail_id": mail["id"], "error": "Fixed appointment analysis failed.",
                             "cached": False} for mail in mails], "complete": False,
            }
        outcomes = [{"mail_id": mail["id"], **_event_result(mail), "cached": False} for mail in mails]
        return {
            "events": [event for outcome in outcomes for event in outcome["events"]],
            "warnings": [], "results": outcomes, "complete": True,
        }

    def run_agents(task_action, event_action, api_key, model):
        calls.append(("coordinator", model))
        task_result = task_action()
        event_result = event_action()
        return {
            "status": "completed" if event_result.get("complete") and task_result.get("status") == "completed" else "partial",
            "task_result": task_result, "event_result": event_result,
            "events": [], "usage": {}, "warnings": [],
        }

    monkeypatch.setattr(batch_ui, "connect", lambda *_args, **_kwargs: Mailbox())
    monkeypatch.setattr(batch_ui, "extract_tasks", extract_tasks)
    monkeypatch.setattr(batch_ui, "analyze_fixed_event_messages", analyze_events)
    monkeypatch.setattr(batch_ui, "run_inbox_agents", run_agents)
    monkeypatch.setattr(batch_ui, "reconcile_tasks", lambda *_args, **_kwargs: {"links": {}, "warnings": []})
    return database, calls


def _button(app, label):
    return next(item for item in app.button if item.label == label)


def _tables(app, column):
    return [item.value for item in app.dataframe if column in item.value.columns]


def _analyze(app, identifier="mixed"):
    app.multiselect(key="batch_selected").set_value([identifier]).run(timeout=30)
    app.checkbox(key="batch_consent").check().run(timeout=30)
    app.button(key="batch_extract").click().run(timeout=30)
    assert not app.exception


def _model_calls(calls):
    return [call for call in calls if call[0] in {"tasks", "events", "coordinator"}]


def test_one_click_opens_mail_and_presents_both_kinds_without_saving(tmp_path, monkeypatch):
    database, calls = _setup(tmp_path, monkeypatch)
    app = AppTest.from_string(APP_SOURCE).run(timeout=30)
    assert not app.exception and calls == []
    assert app.button(key="batch_extract").label == "Analyze selected messages"
    assert app.button(key="batch_extract").disabled
    app.multiselect(key="batch_selected").set_value(["mixed"]).run()
    assert app.button(key="batch_extract").disabled and calls == []
    _analyze(app)
    assert [call for call in calls if call[0] == "fetch"] == [("fetch", "mixed")]
    assert len([call for call in calls if call[0] == "tasks"]) == 1
    assert len([call for call in calls if call[0] == "events"]) == 1
    assert len(_tables(app, "Task")[0]) == 2
    assert len(_tables(app, "Event")[0]) == 2
    assert not any(item.label == "Extract fixed events" for item in app.button)
    assert Store(database).tasks() == [] and Store(database).list_fixed_events() == []
    before = deepcopy(calls)
    app.run(timeout=30)
    assert not app.exception and calls == before


def test_event_only_message_still_renders_fixed_review(tmp_path, monkeypatch):
    database, calls = _setup(tmp_path, monkeypatch)
    app = AppTest.from_string(APP_SOURCE).run(timeout=30)
    _analyze(app, "event-only")
    assert not _tables(app, "Task")
    assert len(_tables(app, "Event")[0]) == 1
    assert _button(app, "Save selected fixed events").disabled
    assert not any(item.label == "Save selected tasks" for item in app.button)
    assert len([call for call in calls if call[0] == "events"]) == 1
    assert Store(database).list_fixed_events() == []


def test_fixed_branch_failure_preserves_task_review_for_retry(tmp_path, monkeypatch):
    database, _ = _setup(tmp_path, monkeypatch, fixed_failure=True)
    app = AppTest.from_string(APP_SOURCE).run(timeout=30)
    _analyze(app)
    assert len(_tables(app, "Task")[0]) == 2
    assert not _tables(app, "Event")
    notices = [item.value for kind in (app.warning, app.error, app.caption, app.info) for item in kind]
    assert any("fixed" in notice.casefold() and ("fail" in notice.casefold() or "retry" in notice.casefold())
               for notice in notices)
    assert any("Fixed appointment analysis failed." in item.value for item in app.error)
    assert any("fixed-event analysis is incomplete" in notice.casefold() for notice in notices)
    assert not any("No fixed appointments found" in notice for notice in notices)
    assert Store(database).tasks() == []


def test_task_branch_failure_is_not_reported_as_no_tasks_found(tmp_path, monkeypatch):
    database, _ = _setup(tmp_path, monkeypatch)

    def fail_tasks(*_args, **_kwargs):
        raise RuntimeError("private model failure details")

    monkeypatch.setattr(batch_ui, "extract_tasks", fail_tasks)
    app = AppTest.from_string(APP_SOURCE).run(timeout=30)
    _analyze(app)
    assert not _tables(app, "Task")
    assert len(_tables(app, "Event")[0]) == 2
    notices = [item.value for kind in (app.warning, app.error, app.caption, app.info) for item in kind]
    assert any("study-task analysis is incomplete" in notice.casefold() for notice in notices)
    assert not any("No flexible study tasks found" in notice for notice in notices)
    assert not any("private model failure details" in notice for notice in notices)
    assert Store(database).tasks() == [] and Store(database).list_fixed_events() == []


@pytest.mark.parametrize("stage", ["init", "read", "write"])
def test_full_cache_failure_keeps_successful_proposals_for_review(tmp_path, monkeypatch, stage):
    database, _ = _setup(tmp_path, monkeypatch)
    original_cache = batch_ui.InboxAnalysisCache

    class UnavailableCache(original_cache):
        def __init__(self, path):
            if stage == "init":
                raise OperationalError("private cache path initialization failure")
            super().__init__(path)

        def get(self, key):
            if stage == "read":
                raise OperationalError("private cache path read failure")
            return super().get(key)

        def put(self, key, result):
            if stage == "write":
                raise OperationalError("private cache path write failure")
            return super().put(key, result)

    monkeypatch.setattr(batch_ui, "InboxAnalysisCache", UnavailableCache)
    app = AppTest.from_string(APP_SOURCE).run(timeout=30)
    _analyze(app)
    assert len(_tables(app, "Task")[0]) == 2
    assert len(_tables(app, "Event")[0]) == 2
    assert app.session_state["batch_analysis_run"]["status"] == "completed"
    assert not app.error
    warnings = [item.value for item in app.warning]
    assert any("cache" in warning.casefold() or "Previous complete analysis could not be loaded" in warning
               for warning in warnings)
    assert not any("private cache path" in warning for warning in warnings)
    assert not any("Some analysis could not finish" in warning for warning in warnings)
    assert Store(database).tasks() == [] and Store(database).list_fixed_events() == []


def test_human_save_gates_are_separate_and_repeated_save_is_idempotent(tmp_path, monkeypatch):
    database, calls = _setup(tmp_path, monkeypatch)
    app = AppTest.from_string(APP_SOURCE).run(timeout=30)
    _analyze(app)
    before = _model_calls(calls)
    store = Store(database)
    _button(app, "Save selected tasks").click().run(timeout=30)
    assert not app.exception and store.tasks() == []
    assert any("review" in item.value.casefold() for item in app.error)
    assert _button(app, "Save selected fixed events").disabled

    task_review = next(item for item in app.checkbox if item.key.startswith("batch_reviewed_"))
    task_version = task_review.key.removeprefix("batch_reviewed_")
    app.session_state[f"batch_editor_{task_version}"] = {
        "edited_rows": {0: {"Include": True}, 1: {"Include": True}},
        "added_rows": [], "deleted_rows": [],
    }
    task_review.check()
    _button(app, "Save selected tasks").click().run(timeout=30)
    assert not app.exception and len(store.tasks()) == 2
    assert store.list_fixed_events() == []
    _button(app, "Save selected tasks").click().run(timeout=30)
    assert len(store.tasks()) == 2

    fixed_review = next(item for item in app.checkbox if "checked the event dates" in item.label)
    editor_key = fixed_review.key.removesuffix("_reviewed") + "_rows"
    fixed_review.check().run(timeout=30)
    app.session_state[editor_key] = {
        "edited_rows": {0: {"Include": True}, 1: {"Include": True}},
        "added_rows": [], "deleted_rows": [],
    }
    _button(app, "Save selected fixed events").click().run(timeout=30)
    assert not app.exception and len(store.list_fixed_events(ACCOUNT)) == 2
    _button(app, "Save selected fixed events").click().run(timeout=30)
    assert len(store.list_fixed_events(ACCOUNT)) == 2 and len(store.tasks()) == 2
    assert _model_calls(calls) == before


@pytest.mark.parametrize("change", ["selection", "model"])
def test_new_selection_or_model_hides_both_old_reviews_without_analysis(tmp_path, monkeypatch, change):
    database, calls = _setup(tmp_path, monkeypatch)
    app = AppTest.from_string(APP_SOURCE).run(timeout=30)
    _analyze(app)
    before = deepcopy(calls)
    if change == "selection":
        app.multiselect(key="batch_selected").set_value(["other"]).run(timeout=30)
    else:
        app.selectbox(key="test_model").select("model-other").run(timeout=30)
    assert not app.exception and calls == before
    assert app.checkbox(key="batch_consent").value is False
    assert not _tables(app, "Task") and not _tables(app, "Event")
    assert not any(item.label in {"Save selected tasks", "Save selected fixed events"} for item in app.button)
    assert Store(database).tasks() == [] and Store(database).list_fixed_events() == []


def test_new_browser_session_reuses_whole_successful_analysis(tmp_path, monkeypatch):
    database, calls = _setup(tmp_path, monkeypatch)
    first = AppTest.from_string(APP_SOURCE).run(timeout=30)
    _analyze(first)
    assert REVIEW_NOTICE in [item.value for item in first.caption]
    before = _model_calls(calls)
    second = AppTest.from_string(APP_SOURCE).run(timeout=30)
    _analyze(second)
    assert len(_tables(second, "Task")[0]) == 2
    assert len(_tables(second, "Event")[0]) == 2
    assert _model_calls(calls) == before
    assert REVIEW_NOTICE in [item.value for item in second.caption]
    assert Store(database).tasks() == [] and Store(database).list_fixed_events() == []


def test_actionable_warning_prevents_full_analysis_reuse(tmp_path, monkeypatch):
    database, calls = _setup(tmp_path, monkeypatch)
    original_extractor = batch_ui.extract_tasks
    warning = "Some source text was not analyzed; review the missing material."

    def extract_with_warning(*args, **kwargs):
        result = original_extractor(*args, **kwargs)
        result["warnings"].append(warning)
        return result

    monkeypatch.setattr(batch_ui, "extract_tasks", extract_with_warning)
    first = AppTest.from_string(APP_SOURCE).run(timeout=30)
    _analyze(first)
    second = AppTest.from_string(APP_SOURCE).run(timeout=30)
    _analyze(second)
    assert len(_tables(second, "Task")[0]) == 2
    assert len(_tables(second, "Event")[0]) == 2
    assert len([call for call in calls if call[0] == "coordinator"]) == 2
    assert second.session_state["batch_analysis_run"]["cached"] is False
    assert warning in [item.value for item in second.caption]
    assert REVIEW_NOTICE in [item.value for item in second.caption]
    assert Store(database).tasks() == [] and Store(database).list_fixed_events() == []
