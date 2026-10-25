"""Batch extraction journeys with isolated email and AI doubles."""

import io
from email.message import EmailMessage
from pathlib import Path

import pytest
from docx import Document
from streamlit.testing.v1 import AppTest

from studentflow import batch_ui
from studentflow.attachment_library import AttachmentLibrary
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
    # Exercise the real source/review path without contacting orchestration models.
    def coordinate(tasks, events, *_args, **_kwargs):
        task_result, event_result = tasks(), events()
        return {"status": "completed", "task_result": task_result,
                "event_result": event_result, "events": [], "usage": {}, "warnings": []}

    monkeypatch.setattr(batch_ui, "run_inbox_agents", coordinate)
    monkeypatch.setattr(batch_ui, "analyze_fixed_event_messages", lambda *_args, **_kwargs: {
        "events": [], "results": [], "warnings": [], "complete": True,
    })
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


@pytest.mark.parametrize("outdated_stage", ["extraction", "reconciliation"])
def test_obsolete_browser_results_require_explicit_refresh_without_model_calls(tmp_path, monkeypatch, outdated_stage):
    monkeypatch.setenv("BATCH_TEST_RECORDS", str(tmp_path))
    calls = setup_fakes(monkeypatch)
    app = AppTest.from_string(APP_SOURCE)
    app.session_state["batch_results"] = [{"result": {"tasks": [{"title": "Old suggestion"}]}}]
    app.session_state["batch_results_version"] = (
        "source-pipeline-v1" if outdated_stage == "extraction" else batch_ui.PIPELINE_VERSION)
    app.session_state["batch_reconciliation_version"] = "semantic-review-v1"
    app.session_state["batch_selected"] = ["a"]
    app.run(timeout=30)
    assert not app.exception
    assert calls == []
    assert len(app.dataframe) == 0
    expected = "Task extraction has been updated" if outdated_stage == "extraction" else "Duplicate checking has been updated"
    assert any(expected in item.value for item in app.info)
    assert app.multiselect(key="batch_selected").value == ["a"]
    assert Store(tmp_path / "tasks.sqlite3").tasks() == []
    # A new user-authorized extraction replaces obsolete data and restores review.
    app.checkbox(key="batch_consent").check().run()
    app.button(key="batch_extract").click().run(timeout=30)
    assert not app.exception
    assert len(app.dataframe[0].value) == 1
    assert app.session_state["batch_results_version"] == batch_ui.PIPELINE_VERSION
    assert app.session_state["batch_reconciliation_version"] == batch_ui.RECONCILIATION_VERSION


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
    assert button("Analyze selected messages").disabled
    button("Select all loaded").click()
    checked()
    assert calls == []
    assert app.multiselect(key="batch_selected").value == ["a", "b"]
    app.checkbox(key="batch_consent").check()
    checked()
    assert not button("Analyze selected messages").disabled
    button("Analyze selected messages").click()
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


def attachment_mail():
    document = Document()
    document.add_paragraph("Write the research report. Allow 60 minutes.")
    buffer = io.BytesIO()
    document.save(buffer)
    email = EmailMessage()
    email["Subject"] = "Attachment instructions"
    email["Date"] = "Fri, 9 Oct 2026 09:00:00 +0800"
    email.set_content("For your information.")
    email.add_attachment(buffer.getvalue(), maintype="application", subtype="vnd.openxmlformats-officedocument.wordprocessingml.document", filename="instructions.docx")
    return batch_ui.parse_eml_bytes(email.as_bytes(), "Gmail")


def test_attachment_changes_invalidate_model_cache_even_when_ids_are_reused():
    mail = attachment_mail()
    original = batch_ui.extraction_key("student", mail, "model")
    changed = {**mail, "attachments": [{**mail["attachments"][0], "content": b"changed"}]}
    assert original != batch_ui.extraction_key("student", changed, "model")


def test_batch_passes_attachment_and_saves_locked_correct_provenance(monkeypatch):
    setup_fakes(monkeypatch)
    mail = attachment_mail()
    mail.update(provider="gmail", gmail_id="a", gmail_account=PROFILE["email_address"])
    mails = {mail["id"]: mail}
    seen = []

    def extract(body, subject, sent_at, api_key, model, attachments=None):
        if not attachments:
            return {"tasks": [], "warnings": []}
        seen.extend(attachments or [])
        task = fake_extraction("Write the research report. Allow 60 minutes.", subject, sent_at, api_key, model)["tasks"][0]
        task.update(source_attachment_id=attachments[0]["id"], source_filename="instructions.docx")
        return {"tasks": [task], "warnings": []}

    monkeypatch.setattr(batch_ui, "extract_tasks", extract)
    results = run(["a"], mails)
    assert len(seen) == 1
    assert seen[0]["content"] == mail["attachments"][0]["content"]
    assert seen[0]["id"].startswith("document-")
    rows, sources = batch_ui.review_rows(results, mails)
    rows[0]["Include"] = True
    assert rows[0]["Source file"] == "instructions.docx"
    rows[0]["Source file"] = "forged.docx"
    record = batch_ui.validate_selected_rows(rows, sources, True)[0]
    assert record["source_filename"] == "instructions.docx"
    assert record["source_attachment_id"] == mail["attachments"][0]["id"]
    rows[0]["Supporting text"] = "For your information."
    with pytest.raises(ValueError, match="must match"):
        batch_ui.validate_selected_rows(rows, sources, True)


def test_batch_ui_keeps_original_attachments_when_candidates_are_extracted(tmp_path, monkeypatch):
    monkeypatch.setenv("BATCH_TEST_RECORDS", str(tmp_path))
    setup_fakes(monkeypatch)
    mail = attachment_mail()
    mail.update(provider="gmail", gmail_id="a", gmail_account=PROFILE["email_address"])

    def extract(body, subject, sent_at, api_key, model, attachments=None):
        return fake_extraction(body, subject, sent_at, api_key, model)

    monkeypatch.setattr(batch_ui, "extract_tasks", extract)
    app = AppTest.from_string(APP_SOURCE).run(timeout=30)
    app.session_state["mails"] = {mail["id"]: mail}
    app.multiselect(key="batch_selected").set_value(["a"])
    app.run(timeout=30)
    app.checkbox(key="batch_consent").check()
    app.run(timeout=30)
    app.button(key="batch_extract").click()
    app.run(timeout=30)
    assert not app.exception
    library = AttachmentLibrary(tmp_path / "tasks.sqlite3")
    assert len(library.documents()) == 1
    assert library.content(library.documents()[0]["library_id"]) == mail["attachments"][0]["content"]
    assert Store(tmp_path / "tasks.sqlite3").tasks() == []


def test_ui_estimates_unknown_time_and_saves_only_human_reviewed_value(tmp_path, monkeypatch):
    monkeypatch.setenv("BATCH_TEST_RECORDS", str(tmp_path))
    setup_fakes(monkeypatch)
    calls = []

    def extract(body, subject, sent_at, api_key, model):
        result = fake_extraction(body, subject, sent_at, api_key, model)
        result["tasks"][0]["estimated_minutes"] = None
        return result

    def estimate(tasks, api_key, model):
        calls.append([task["id"] for task in tasks])
        return {"estimates": {task["id"]: {"suggested_minutes": 60,
                "estimate_reason": "Draft and check the report.",
                "assumptions": ["Research is complete."]} for task in tasks}, "warnings": []}

    monkeypatch.setattr(batch_ui, "extract_tasks", extract)
    monkeypatch.setattr(batch_ui, "estimate_task_efforts", estimate)
    app = AppTest.from_string(APP_SOURCE).run(timeout=30)
    assert not app.exception and calls == []
    app.multiselect(key="batch_selected").set_value(["a"]).run()
    app.checkbox(key="batch_consent").check().run()
    app.button(key="batch_extract").click().run(timeout=30)
    assert not app.exception and len(calls) == 1
    frame = app.dataframe[0].value
    assert frame.iloc[0]["AI suggestion (min)"] == 60
    assert frame.iloc[0]["Study time (min)"] == 60
    assert "Research is complete" in frame.iloc[0]["Estimate basis"]
    assert Store(tmp_path / "tasks.sqlite3").tasks() == []
    version = app.session_state["batch_review_version"]
    next(item for item in app.button if item.label == "Save selected tasks").click().run()
    assert Store(tmp_path / "tasks.sqlite3").tasks() == []
    assert any("reviewed" in item.value for item in app.error)
    app.session_state[f"batch_editor_{version}"] = {
        "edited_rows": {0: {"Include": True, "Study time (min)": 105}},
        "added_rows": [], "deleted_rows": [],
    }
    app.checkbox(key=f"batch_reviewed_{version}").check()
    next(item for item in app.button if item.label == "Save selected tasks").click().run()
    assert not app.exception
    assert not app.error, [item.value for item in app.error]
    task, = Store(tmp_path / "tasks.sqlite3").tasks()
    assert task["estimated_minutes"] == 105
    assert task["effort_review"]["suggested_minutes"] == 60
    assert task["effort_review"]["confirmed_minutes"] == 105
    assert task["effort_review"]["decision"] == "adjusted"
    app.run()
    assert len(calls) == 1  # Rerenders and saving never trigger a paid estimation call.


def test_turning_off_effort_suggestions_requires_new_consent_and_skips_estimator(tmp_path, monkeypatch):
    monkeypatch.setenv("BATCH_TEST_RECORDS", str(tmp_path))
    setup_fakes(monkeypatch)

    def extract(body, subject, sent_at, api_key, model):
        result = fake_extraction(body, subject, sent_at, api_key, model)
        result["tasks"][0]["estimated_minutes"] = None
        return result

    def forbidden(*args):
        raise AssertionError("Estimation was disabled.")

    monkeypatch.setattr(batch_ui, "extract_tasks", extract)
    monkeypatch.setattr(batch_ui, "estimate_task_efforts", forbidden)
    app = AppTest.from_string(APP_SOURCE).run(timeout=30)
    app.multiselect(key="batch_selected").set_value(["a"]).run()
    app.checkbox(key="batch_consent").check().run()
    app.checkbox(key="batch_suggest_effort").uncheck().run()
    assert not app.checkbox(key="batch_consent").value
    app.checkbox(key="batch_consent").check().run()
    app.button(key="batch_extract").click().run(timeout=30)
    assert not app.exception
    assert app.dataframe[0].value.iloc[0]["Study time (min)"] == 0
    assert not app.session_state["batch_effort_warnings"]

