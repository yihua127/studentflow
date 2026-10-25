"""Minor extraction-title variants must not duplicate a sourced assignment."""

import hashlib
import io
import json
from copy import deepcopy
from itertools import permutations

import pytest
from docx import Document
from streamlit.testing.v1 import AppTest

from studentflow import batch_ui
from studentflow.batch_ui import review_rows, validate_selected_rows
from studentflow.storage import Store

DUE = "2026-10-16T17:00:00+08:00"
ACCOUNT = "student@example.invalid"
OUTLINE = "DSX1002 research outline"
QUOTE = "Research outline: submit the outline by 16 October, 17:00."
SECOND_QUOTE = "The DSX1002 research outline must be uploaded by 16 October, 17:00."


def file_bytes(text):
    document = Document()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def message(identifier, content):
    return {
        "id": identifier, "gmail_id": "gmail-" + identifier, "gmail_account": ACCOUNT,
        "provider": "gmail", "subject": "Course syllabus" if identifier == "a" else "Forwarded syllabus",
        "sent_at": "2026-10-09T09:00:00+08:00", "body": "See the attached syllabus.",
        "attachments": [{"id": "file-" + identifier, "filename": "syllabus.docx", "content": content}],
    }


def proposal(mail, title=OUTLINE, quote=QUOTE, **changes):
    return {
        "title": title, "due_at": DUE, "estimated_minutes": 60,
        "source_quote": quote, "source_attachment_id": mail["attachments"][0]["id"],
        "source_filename": "syllabus.docx", "confidence": 0.9, "needs_clarification": [],
        **changes,
    }


def prepare(*pairs):
    mails, results = {}, []
    for mail, tasks in pairs:
        mails[mail["id"]] = mail
        results.append({
            "mail_id": mail["id"], "gmail_id": mail["gmail_id"], "cache_key": "cache-" + mail["id"],
            "result": {"tasks": tasks, "warnings": []}, "cached": True,
        })
    snapshot = deepcopy(results)
    rows, sources = review_rows(results, mails)
    assert results == snapshot
    return rows, sources


def records(rows, sources):
    return validate_selected_rows([dict(row, Include=True) for row in rows], sources, True)


@pytest.mark.parametrize("reverse", [False, True])
def test_screenshot_ten_title_variants_become_five_with_both_quotes(reverse):
    tasks = [
        ("DSX1002 research outline", "Submit DSX1002 research outline", "2026-10-12T17:00:00+08:00"),
        ("DSX1002 source evaluation quiz", "Attend DSX1002 source evaluation quiz", "2026-10-16T10:00:00+08:00"),
        ("DSX1002 individual research essay", "Submit DSX1002 individual research essay", "2026-10-23T23:59:00+08:00"),
        ("DSX1002 presentation slides", "Submit DSX1002 presentation slides", "2026-10-28T15:00:00+08:00"),
        ("DSX1002 research presentation", "Attend DSX1002 research presentation", "2026-10-29T14:00:00+08:00"),
    ]
    bare_quotes = [f"{bare}: deadline {due}." for bare, _action, due in tasks]
    action_quotes = [f"Students must {action.lower()} at {due}." for _bare, action, due in tasks]
    content = file_bytes("\n".join(bare_quotes + action_quotes))
    a, b = message("a", content), message("b", content)
    pairs = [
        (a, [proposal(a, bare, quote, due_at=due, estimated_minutes=None)
             for (bare, _action, due), quote in zip(tasks, bare_quotes)]),
        (b, [proposal(b, action, quote, due_at=due, estimated_minutes=None)
             for (_bare, action, due), quote in zip(tasks, action_quotes)]),
    ]
    if reverse:
        pairs.reverse()
    rows, sources = prepare(*pairs)
    assert len(rows) == 5
    saved = records(rows, sources)
    for item, bare_quote, action_quote in zip(saved, bare_quotes, action_quotes):
        assert {evidence["source_quote"] for evidence in item["sources"]} == {bare_quote, action_quote}
        assert {evidence["mail_id"] for evidence in item["sources"]} == {"a", "b"}
    assert all("Duplicate mentions combined (2)." in row["Review notes"] for row in rows)


@pytest.mark.parametrize("first_title,second_title", [(OUTLINE, "Submit " + OUTLINE), ("Submit " + OUTLINE, OUTLINE)])
def test_title_variant_resave_keeps_manual_details_and_completed_work(tmp_path, first_title, second_title):
    content = file_bytes(QUOTE + "\n" + SECOND_QUOTE)
    a, b = message("a", content), message("b", content)
    store = Store(tmp_path / "synthetic.sqlite3")
    rows, sources = prepare((a, [proposal(a, first_title)]))
    identifier = store.save_task(records(rows, sources)[0])
    changed = store.update_task(identifier, {
        "title": "My reviewed outline", "due_at": "2026-10-15T18:00:00+08:00",
        "estimated_minutes": 90, "completed_minutes": 30,
    })
    rows, sources = prepare((b, [proposal(b, second_title, SECOND_QUOTE)]))
    assert store.save_task(records(rows, sources)[0]) == identifier
    [saved] = Store(store.path).tasks()
    for field in ("title", "due_at", "estimated_minutes", "completed_minutes", "remaining_minutes"):
        assert saved[field] == changed[field]
    assert {source["mail_id"] for source in saved["sources"]} == {"a", "b"}


@pytest.mark.parametrize("difference", [
    "prepare_submit", "attend_prepare", "attend_present", "submit_attend", "course",
    "assignment_number", "deadline", "account", "bytes",
])
def test_optional_action_matching_keeps_meaningful_differences_separate(difference):
    content = file_bytes(QUOTE + "\n" + SECOND_QUOTE)
    a, b = message("a", content), message("b", content)
    left, right = proposal(a), proposal(b, "Submit " + OUTLINE, SECOND_QUOTE)
    if difference == "prepare_submit":
        left["title"] = "Prepare " + OUTLINE
    elif difference == "attend_prepare":
        left["title"] = "Attend DSX1002 research presentation"
        right["title"] = "Prepare DSX1002 research presentation"
    elif difference == "attend_present":
        left["title"] = "Attend DSX1002 research presentation"
        right["title"] = "Present DSX1002 research presentation"
    elif difference == "submit_attend":
        left["title"] = "Submit DSX1002 research report"
        right["title"] = "Attend DSX1002 research report"
    elif difference == "course":
        right["title"] = "Submit DSX2003 research outline"
    elif difference == "assignment_number":
        left["title"] = "DSX1002 research outline 1"
        right["title"] = "Submit DSX1002 research outline 2"
    elif difference == "deadline":
        right["due_at"] = "2026-10-17T17:00:00+08:00"
    elif difference == "account":
        b["gmail_account"] = "other@example.invalid"
    elif difference == "bytes":
        b["attachments"][0]["content"] = file_bytes(QUOTE + "\n" + SECOND_QUOTE + "\nA different course document.")
    rows, sources = prepare((a, [left]), (b, [right]))
    assert len(rows) == 2
    assert len(records(rows, sources)) == 2


@pytest.mark.parametrize("order", list(permutations(("bare", "prepare", "submit"))))
def test_generic_title_cannot_bridge_preparation_and_submission_in_batch(order):
    content = file_bytes(QUOTE + "\n" + SECOND_QUOTE)
    titles = {"bare": OUTLINE, "prepare": "Prepare " + OUTLINE, "submit": "Submit " + OUTLINE}
    pairs = []
    for index, kind in enumerate(order):
        mail = message(str(index), content)
        pairs.append((mail, [proposal(mail, titles[kind])]))
    rows, sources = prepare(*pairs)
    saved = records(rows, sources)
    assert len(saved) == 2
    prepare_id, submit_id = str(order.index("prepare")), str(order.index("submit"))
    bare_id = str(order.index("bare"))
    for item in saved:
        ids = {source["mail_id"] for source in item["sources"]}
        assert not {prepare_id, submit_id}.issubset(ids)
        if bare_id in ids:
            assert submit_id in ids


@pytest.mark.parametrize("order", list(permutations(("bare", "prepare", "submit"))))
def test_generic_title_cannot_bridge_different_actions_across_separate_saves(tmp_path, order):
    content = file_bytes(QUOTE + "\n" + SECOND_QUOTE)
    titles = {"bare": OUTLINE, "prepare": "Prepare " + OUTLINE, "submit": "Submit " + OUTLINE}
    store = Store(tmp_path / "synthetic.sqlite3")
    saved_ids = {}
    for index, kind in enumerate(order):
        mail = message(str(index), content)
        rows, sources = prepare((mail, [proposal(mail, titles[kind])]))
        saved_ids[kind] = store.save_task(records(rows, sources)[0])
        store = Store(store.path)
    assert saved_ids["prepare"] != saved_ids["submit"]
    assert saved_ids["bare"] == saved_ids["submit"]
    assert len(store.tasks()) == 2


def test_cached_title_variants_render_five_rows_without_mail_or_model_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("TITLE_VARIANT_RECORDS", str(tmp_path))
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Cached review must not fetch mail or call the model.")

    monkeypatch.setattr(batch_ui, "connect", forbidden)
    monkeypatch.setattr(batch_ui, "extract_tasks", forbidden)
    monkeypatch.setattr(batch_ui, "run_inbox_agents", forbidden)
    monkeypatch.setattr(batch_ui, "analyze_fixed_event_messages", forbidden)
    objects = ["research outline", "source evaluation quiz", "individual research essay", "presentation slides", "research presentation"]
    actions = ["Submit", "Attend", "Submit", "Submit", "Attend"]
    quotes = [f"DSX1002 {item} is due on the specified date." for item in objects]
    content = file_bytes("\n".join(quotes))
    mails = {identifier: message(identifier, content) for identifier in ("a", "b")}
    results = []
    for identifier, mail in mails.items():
        tasks = [proposal(
            mail, ((action + " ") if identifier == "b" else "") + "DSX1002 " + item, quote,
            due_at=f"2026-10-{12 + index:02d}T17:00:00+08:00",
        ) for index, (item, action, quote) in enumerate(zip(objects, actions, quotes))]
        results.append({
            "mail_id": identifier, "gmail_id": mail["gmail_id"], "cache_key": "cache-" + identifier,
            "result": {"tasks": tasks, "warnings": []}, "cached": True,
        })
    source = '''
import os
from pathlib import Path
import streamlit as st
from studentflow.batch_ui import render_batch_extraction
from studentflow.storage import Store
headers = [{'id': mail['gmail_id'], 'subject': mail['subject'], 'sender': 'teacher@example.invalid'}
           for mail in st.session_state.mails.values()]
render_batch_extraction(headers, Path('fake-client'), Path('fake-token'),
    Store(Path(os.environ['TITLE_VARIANT_RECORDS']) / 'tasks.sqlite3'), 'fake-key', 'model-test', True)
'''
    app = AppTest.from_string(source)
    app.session_state["mails"] = mails
    app.session_state["candidates"] = {}
    app.session_state["gmail_profile"] = {"email_address": ACCOUNT}
    selected = [mail["gmail_id"] for mail in mails.values()]
    app.session_state["batch_selected"] = selected
    app.session_state["batch_results"] = results
    fixed_result = {"events": [], "warnings": [], "results": [], "complete": True}
    app.session_state["batch_fixed_result"] = fixed_result
    app.session_state["batch_analysis_run"] = {
        "status": "completed", "task_result": {"results": results, "effort": {}, "status": "completed"},
        "event_result": fixed_result, "events": [], "usage": {}, "warnings": [], "cached": True,
    }
    app.session_state["batch_analysis_signature"] = hashlib.sha256(json.dumps([
        batch_ui.UNIFIED_VERSION, batch_ui.PIPELINE_VERSION, batch_ui.RECONCILIATION_VERSION,
        ACCOUNT, selected, "model-test", True,
    ]).encode()).hexdigest()
    app.session_state["batch_results_version"] = batch_ui.PIPELINE_VERSION
    app.session_state["batch_reconciliation_version"] = batch_ui.RECONCILIATION_VERSION
    app.session_state["batch_review_version"] = 4
    app.session_state["batch_editor_4"] = {"edited_rows": {9: {"Include": True}}, "added_rows": [], "deleted_rows": []}
    app.session_state["batch_reviewed_4"] = True
    app.run(timeout=30)
    assert not app.exception
    assert len(app.dataframe[0].value) == 5
    assert any("5 duplicate mention(s) combined. 5 distinct task suggestion(s)" in item.value for item in app.caption)
    version = app.session_state["batch_review_version"]
    assert version != 4
    assert "batch_editor_4" not in app.session_state
    assert not app.checkbox(key=f"batch_reviewed_{version}").value
    assert calls == []
    app.session_state[f"batch_editor_{version}"] = {
        "edited_rows": {0: {"Include": True, "Study time (min)": 75}}, "added_rows": [], "deleted_rows": [],
    }
    app.checkbox(key=f"batch_reviewed_{version}").check()
    next(button for button in app.button if button.label == "Save selected tasks").click()
    app.run(timeout=30)
    assert not app.exception
    assert len(app.dataframe[0].value) == 5
    [saved] = Store(tmp_path / "tasks.sqlite3").tasks()
    assert saved["title"] == "DSX1002 research outline"
    assert saved["estimated_minutes"] == 75
    assert len(saved["sources"]) == 2
    assert calls == []
