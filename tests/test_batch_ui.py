"""Batch extraction journeys with isolated email and AI doubles."""

from email.message import EmailMessage
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from studentflow import batch_ui
from studentflow.gmail import GmailAuthError, GmailError
from studentflow.storage import Store

PROFILE = {"email_address": "student@example.com"}


def message(identifier):
    mail = EmailMessage()
    mail["Subject"] = f"Assignment {identifier}"
    mail["From"] = "teacher@example.com"
    mail["Date"] = "Fri, 9 Oct 2026 09:00:00 +0800"
    mail.set_content(f"Submit report {identifier} by 16 October at 17:00. Allow 60 minutes.")
    return mail.as_bytes()


def fake_extraction(body, subject, sent_at, api_key, model):
    return {"tasks": [{
        "title": "Complete " + subject, "due_at": "2026-10-16T17:00:00+08:00",
        "estimated_minutes": 60, "source_quote": body, "confidence": 0.9,
        "needs_clarification": [],
    }], "warnings": []}


def setup_fakes(monkeypatch, *, fetch_failure=None, model_failure=None, profile=None):
    calls = []

    class Mailbox:
        def profile(self):
            calls.append(("profile",))
            return profile or PROFILE

        def fetch_eml(self, identifier):
            calls.append(("fetch", identifier))
            if fetch_failure and identifier == fetch_failure[0]:
                raise fetch_failure[1]
            return message(identifier)

        def close(self):
            calls.append(("close",))

    def extract(body, subject, sent_at, api_key, model):
        calls.append(("extract", subject, model))
        if subject == model_failure:
            raise RuntimeError("sensitive remote request details")
        return fake_extraction(body, subject, sent_at, api_key, model)

    monkeypatch.setattr(batch_ui, "connect", lambda *_args, **_kwargs: Mailbox())
    monkeypatch.setattr(batch_ui, "extract_tasks", extract)
    return calls


def run(ids, mails=None, cache=None, candidates=None, **kwargs):
    return batch_ui.run_batch(
        ids, Path("fake-client.json"), Path("fake-token.json"), PROFILE,
        mails if mails is not None else {}, cache if cache is not None else {},
        candidates if candidates is not None else {}, "test-key", kwargs.pop("model", "model-test"),
        **kwargs,
    )


def test_only_selected_messages_are_fetched_and_successes_survive_one_failure(monkeypatch):
    calls = setup_fakes(monkeypatch, model_failure="Assignment b")
    mails, cache, candidates = {}, {}, {}
    results = run(["a", "b", "c", "a"], mails, cache, candidates)
    assert [entry["gmail_id"] for entry in results] == ["a", "b", "c"]
    assert [item for item in calls if item[0] == "fetch"] == [("fetch", "a"), ("fetch", "b"), ("fetch", "c")]
    assert "result" in results[0] and "result" in results[2]
    assert "error" in results[1] and "sensitive" not in results[1]["error"]
    assert len(cache) == len(candidates) == 2
    assert calls.index(("close",)) < next(index for index, call in enumerate(calls) if call[0] == "extract")


def test_retry_reuses_completed_results_and_only_calls_failed_message(monkeypatch):
    calls = setup_fakes(monkeypatch, model_failure="Assignment b")
    mails, cache, candidates = {}, {}, {}
    run(["a", "b"], mails, cache, candidates)
    calls.clear()
    results = run(["a", "b"], mails, cache, candidates)
    assert not any(call[0] == "fetch" for call in calls)
    assert [call for call in calls if call[0] == "extract"] == [("extract", "Assignment b", "model-test")]
    assert results[0]["cached"] is True


def test_reanalyze_and_new_model_require_new_calls(monkeypatch):
    calls = setup_fakes(monkeypatch)
    mails, cache = {}, {}
    run(["a"], mails, cache)
    calls.clear()
    run(["a"], mails, cache, reanalyze=True)
    run(["a"], mails, cache, model="different-model")
    assert len([call for call in calls if call[0] == "extract"]) == 2
    assert len(cache) == 2


@pytest.mark.parametrize("changed", [True, False])
def test_account_change_or_mid_fetch_auth_failure_stops_all_model_calls(monkeypatch, changed):
    calls = setup_fakes(
        monkeypatch,
        profile={"email_address": "other@example.com"} if changed else None,
        fetch_failure=("b", GmailAuthError("Reconnect")),
    )
    with pytest.raises(GmailAuthError):
        run(["a", "b", "c"])
    assert not any(call[0] == "extract" for call in calls)
    if changed:
        assert not any(call[0] == "fetch" for call in calls)
    else:
        assert ("fetch", "c") not in calls


def test_single_fetch_failure_does_not_discard_other_results(monkeypatch):
    setup_fakes(monkeypatch, fetch_failure=("b", GmailError("private detail")))
    results = run(["a", "b", "c"])
    assert ["result" in result for result in results] == [True, False, True]
    assert "private" not in results[1]["error"]


def test_cache_binds_account_model_and_message_content():
    mail = {"body": "Submit report", "subject": "Report", "sent_at": None}
    initial = batch_ui.extraction_key("student@example.com", mail, "model")
    assert initial != batch_ui.extraction_key("other@example.com", mail, "model")
    assert initial != batch_ui.extraction_key("student@example.com", mail, "model2")
    assert initial != batch_ui.extraction_key("student@example.com", dict(mail, body="Changed"), "model")


def prepared_rows(monkeypatch):
    setup_fakes(monkeypatch)
    mails = {}
    results = run(["a", "b"], mails)
    rows, sources = batch_ui.review_rows(results, mails)
    for row in rows:
        row["Include"] = True
    return rows, sources


def test_review_gate_and_missing_details(monkeypatch):
    rows, sources = prepared_rows(monkeypatch)
    with pytest.raises(ValueError, match="reviewed"):
        batch_ui.validate_selected_rows(rows, sources, False)
    rows[0]["Deadline"] = ""
    rows[0]["Study time (min)"] = 0
    records = batch_ui.validate_selected_rows(rows, sources, True)
    assert records[0]["status"] == "pending"
    assert records[0]["due_at"] is None and records[0]["estimated_minutes"] is None
    assert records[1]["status"] == "confirmed"
    assert records[0]["mail_id"] != records[1]["mail_id"]


@pytest.mark.parametrize("field,value,error", [
    ("Supporting text", "Fabricated quotation", "must match"),
    ("Deadline", "tomorrow", "YYYY-MM-DD"),
    ("Study time (min)", -1, "whole number"),
    ("Study time (min)", 1.5, "whole number"),
    ("Task", "", "name"),
    ("_row_id", "untrusted source", "source changed"),
])
def test_every_selected_row_is_validated_before_any_save(tmp_path, monkeypatch, field, value, error):
    rows, sources = prepared_rows(monkeypatch)
    rows[1][field] = value
    store = Store(tmp_path / "tasks.sqlite3")
    with pytest.raises(ValueError, match=error):
        records = batch_ui.validate_selected_rows(rows, sources, True)
        store.save_tasks(records)
    assert store.tasks() == []


def test_source_quote_cannot_be_borrowed_from_another_message(monkeypatch):
    rows, sources = prepared_rows(monkeypatch)
    rows[0]["Supporting text"] = rows[1]["Supporting text"]
    with pytest.raises(ValueError, match="must match"):
        batch_ui.validate_selected_rows(rows, sources, True)


def test_same_source_duplicate_names_are_rejected_even_with_different_dates(monkeypatch):
    rows, sources = prepared_rows(monkeypatch)
    rows[1]["Task"] = "  " + rows[0]["Task"] + "  "
    rows[1]["Deadline"] = "2026-10-17 17:00"
    rows[1]["Supporting text"] = rows[0]["Supporting text"]
    sources[rows[1]["_row_id"]] = sources[rows[0]["_row_id"]]
    with pytest.raises(ValueError, match="distinct names"):
        batch_ui.validate_selected_rows(rows, sources, True)


def test_different_source_tasks_can_share_the_same_name(monkeypatch):
    rows, sources = prepared_rows(monkeypatch)
    rows[1]["Task"] = rows[0]["Task"]
    records = batch_ui.validate_selected_rows(rows, sources, True)
    assert len(records) == 2
    assert records[0]["title"] == records[1]["title"]
    assert records[0]["mail_id"] != records[1]["mail_id"]


APP_SOURCE = '''
from pathlib import Path
import os
import streamlit as st
from studentflow.batch_ui import render_batch_extraction
from studentflow.storage import Store
st.session_state.setdefault('mails', {})
st.session_state.setdefault('candidates', {})
st.session_state.setdefault('gmail_profile', {'email_address': 'student@example.com'})
headers = [{'id': value, 'subject': 'Assignment ' + value, 'sender': 'teacher@example.com'} for value in ['a', 'b']]
render_batch_extraction(headers, Path('fake-client'), Path('fake-token'),
    Store(Path(os.environ['BATCH_TEST_RECORDS']) / 'tasks.sqlite3'), 'test-key', 'model-test', True)
'''


def test_ui_requires_explicit_selection_consent_and_bulk_review(tmp_path, monkeypatch):
    monkeypatch.setenv("BATCH_TEST_RECORDS", str(tmp_path))
    calls = setup_fakes(monkeypatch)
    extract = batch_ui.extract_tasks

    def extract_with_reminder(*args, **kwargs):
        result = extract(*args, **kwargs)
        result["warnings"] = ["Review the suggestions before saving."]
        return result

    monkeypatch.setattr(batch_ui, "extract_tasks", extract_with_reminder)
    app = AppTest.from_string(APP_SOURCE)

    def checked():
        app.run(timeout=30)
        assert not app.exception

    def button(label):
        return next(item for item in app.button if item.label == label)

    checked()
    assert calls == []
    assert app.multiselect(key="batch_selected").value == []
    assert button("Extract tasks").disabled
    button("Select all loaded").click()
    checked()
    assert calls == []
    assert app.multiselect(key="batch_selected").value == ["a", "b"]
    app.checkbox(key="batch_consent").check()
    checked()
    assert not button("Extract tasks").disabled
    button("Extract tasks").click()
    checked()
    assert len([call for call in calls if call[0] == "extract"]) == 2
    assert [caption.value for caption in app.caption].count("Review the suggestions before saving.") == 1
    assert Store(tmp_path / "tasks.sqlite3").tasks() == []
    button("Save selected tasks").click()
    checked()
    assert any("reviewed" in error.value for error in app.error)
    assert Store(tmp_path / "tasks.sqlite3").tasks() == []
    before = list(calls)
    checked()
    assert calls == before
    app.session_state["batch_editor_1"] = {
        "edited_rows": {0: {"Include": True}, 1: {"Include": True}},
        "added_rows": [], "deleted_rows": [],
    }
    app.checkbox(key="batch_reviewed_1").check()
    button("Save selected tasks").click()
    checked()
    records = Store(tmp_path / "tasks.sqlite3").tasks()
    assert len(records) == 2
    assert {record["mail_subject"] for record in records} == {"Assignment a", "Assignment b"}
    button("Save selected tasks").click()
    checked()
    assert len(Store(tmp_path / "tasks.sqlite3").tasks()) == 2
    app.multiselect(key="batch_selected").set_value(["a"])
    checked()
    assert app.checkbox(key="batch_consent").value is False


def test_view_selected_messages_without_api_or_consent_reuses_opened_mail(tmp_path, monkeypatch):
    monkeypatch.setenv("BATCH_TEST_RECORDS", str(tmp_path))
    calls = setup_fakes(monkeypatch)
    app = AppTest.from_string(APP_SOURCE.replace("'test-key', 'model-test'", "'', ''"))
    app.run(timeout=30)
    assert not app.exception and not app.expander
    assert calls == []
    assert app.button(key="batch_view").disabled
    app.multiselect(key="batch_selected").set_value(["b", "a"])
    app.run(timeout=30)
    assert not app.exception and calls == []
    assert not app.button(key="batch_view").disabled
    assert app.button(key="batch_extract").disabled
    assert app.checkbox(key="batch_consent").value is False
    app.session_state["review_message"] = "old-message"
    app.button(key="batch_view").click()
    app.run(timeout=30)
    assert not app.exception
    assert [call for call in calls if call[0] == "fetch"] == [("fetch", "b"), ("fetch", "a")]
    assert not any(call[0] == "extract" for call in calls)
    assert app.session_state["mails"][app.session_state["active_mail"]]["gmail_id"] == "b"
    assert "review_message" not in app.session_state
    assert app.session_state["candidates"] == {}
    assert Store(tmp_path / "tasks.sqlite3").tasks() == []
    calls.clear()
    app.button(key="batch_view").click()
    app.run(timeout=30)
    assert not app.exception
    assert calls == [("profile",), ("close",)]


def test_view_keeps_successful_messages_and_sanitizes_partial_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("BATCH_TEST_RECORDS", str(tmp_path))
    calls = setup_fakes(monkeypatch, fetch_failure=("a", GmailError("private request detail")))
    app = AppTest.from_string(APP_SOURCE)
    app.run(timeout=30)
    app.multiselect(key="batch_selected").set_value(["a", "b"])
    app.run(timeout=30)
    app.button(key="batch_view").click()
    app.run(timeout=30)
    assert not app.exception
    assert len(app.session_state["mails"]) == 1
    assert app.session_state["mails"][app.session_state["active_mail"]]["gmail_id"] == "b"
    assert any("could not be opened" in error.value for error in app.error)
    assert not any("private request detail" in error.value for error in app.error)
    assert not any(call[0] == "extract" for call in calls)


def test_view_account_mismatch_clears_old_messages_and_requires_reconnect(tmp_path, monkeypatch):
    monkeypatch.setenv("BATCH_TEST_RECORDS", str(tmp_path))
    calls = setup_fakes(monkeypatch, profile={"email_address": "different@example.com"})
    app = AppTest.from_string(APP_SOURCE)
    app.run(timeout=30)
    app.session_state["mails"] = {"previous": {"gmail_id": "a", "gmail_account": PROFILE["email_address"], "provider": "gmail"}}
    app.multiselect(key="batch_selected").set_value(["a"])
    app.run(timeout=30)
    app.button(key="batch_view").click()
    app.run(timeout=30)
    assert not app.exception
    assert app.session_state["mails"] == {}
    assert app.session_state["gmail_auth_required"] is True
    assert "Reconnect Gmail" in app.session_state["gmail_connection_error"]
    assert not any(call[0] in {"fetch", "extract"} for call in calls)


def test_empty_inbox_guides_refresh_without_network_or_ai(tmp_path, monkeypatch):
    monkeypatch.setenv("BATCH_TEST_RECORDS", str(tmp_path))
    calls = setup_fakes(monkeypatch)
    app = AppTest.from_string(APP_SOURCE.replace("for value in ['a', 'b']", "for value in []"))
    app.run(timeout=30)
    assert not app.exception
    assert any(info.value == "Refresh the inbox to choose messages." for info in app.info)
    assert not app.multiselect and calls == []

