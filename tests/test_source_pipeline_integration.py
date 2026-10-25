"""Whole-source reuse and semantic aliases through the actual review/save flow."""

import io
import json
from copy import deepcopy
from pathlib import Path

from docx import Document
from streamlit.testing.v1 import AppTest

from studentflow import batch_ui
from studentflow.extraction_cache import ExtractionCache
from studentflow.storage import Store
from studentflow.task_reconciliation import reconcile_tasks

ACCOUNT = "student@example.invalid"
QUOTES = [f"Deliverable {number}: submit on 1{number} October 2026 at 17:00."
          for number in range(5)]


def setup_sources(monkeypatch):
    doc = Document()
    doc.add_paragraph("\n".join(QUOTES))
    stream = io.BytesIO()
    doc.save(stream)
    mails = {
        name: {"id": name, "gmail_id": name, "provider": "gmail", "gmail_account": ACCOUNT,
               "subject": f"Forwarded material {name}", "body": "For your information.",
               "sent_at": "2026-10-09T09:00:00+08:00", "attachments": [{
                   "id": f"file-{name}", "filename": f"copy-{name}.docx",
                   "content": stream.getvalue(), "warnings": [],
               }]}
        for name in ("a", "b")
    }
    calls, judgments = [], []

    class Mailbox:
        def profile(self):
            return {"email_address": ACCOUNT}

        def fetch_eml(self, *_):
            raise AssertionError("The opened synthetic messages should be reused.")

        def close(self):
            pass

    def extractor(body, subject, sent_at, api_key, model, attachments=None):
        calls.append("attachment" if attachments else "body")
        if not attachments:
            return {"tasks": [], "warnings": []}
        generation = calls.count("attachment")
        return {"tasks": [{
            # Deliberately unrelated display wording on subsequent analyses.
            "title": f"Item {number}" if generation == 1 else f"Finish course obligation {number}",
            "due_at": f"2026-10-1{number}T17:00:00+08:00", "deadline_basis": "absolute",
            "estimated_minutes": 60, "source_quote": quote,
            "source_attachment_id": attachments[0]["id"], "confidence": 0.9,
            "needs_clarification": [],
        } for number, quote in enumerate(QUOTES)], "warnings": []}

    def judge(pairs):
        judgments.append(deepcopy(pairs))
        return {"decisions": [{"pair_id": pair["pair_id"], "verdict": "same_task",
                              "action_match": "same", "deliverable_match": "same",
                              "occurrence_match": "same", "conflicting_details": False,
                              "left_quote": pair["left"]["sources"][0]["quote"],
                              "right_quote": pair["right"]["sources"][0]["quote"],
                              "reason": "The repeated source identifies the same deliverable."}
                              for pair in pairs]}

    def reconciler(proposals, references, api_key, model):
        return reconcile_tasks(proposals, references, api_key, model, judge=judge)

    monkeypatch.setattr(batch_ui, "connect", lambda *_: Mailbox())
    monkeypatch.setattr(batch_ui, "extract_tasks", extractor)
    monkeypatch.setattr(batch_ui, "reconcile_tasks", reconciler)
    return mails, calls, judgments, reconciler


def run(mails, store, reconciler, *, message_ids=("a", "b"), message_cache=None, **kwargs):
    return batch_ui.run_batch(
        message_ids, Path("fake-client"), Path("fake-token"), {"email_address": ACCOUNT},
        mails, message_cache if message_cache is not None else {}, {}, "test-key", "model-test",
        source_cache=ExtractionCache(store.path),
        references=store.task_references(), reconciler=reconciler, **kwargs,
    )


def save(results, mails, store):
    rows, sources = batch_ui.review_rows(results, mails)
    assert len(rows) == 5
    assert all("Duplicate mentions combined (2)." in row["Review notes"] for row in rows)
    records = batch_ui.validate_selected_rows([dict(row, Include=True) for row in rows], sources, True)
    return store.save_tasks(records)


def test_duplicate_files_restart_and_reanalysis_preserve_task_identity(tmp_path, monkeypatch):
    mails, calls, judgments, reconciler = setup_sources(monkeypatch)
    store = Store(tmp_path / "tasks.sqlite3")
    results = run(mails, store, reconciler)
    assert calls.count("attachment") == 1
    assert calls.count("body") == 2
    assert judgments == []  # Sharing a source item requires no semantic API call.
    # Display wording cannot change identity or create another saved task.
    for item in results[1]["result"]["tasks"]:
        item["title"] = "Completely different display wording: " + item["title"]
    identifiers = save(results, mails, store)
    store.update_task(identifiers[0], {"title": "My own name", "estimated_minutes": 90,
                                       "completed_minutes": 30, "due_at": "2026-10-09T19:00:00+08:00"})
    before = len(calls)
    reopened = Store(store.path)
    assert save(run(mails, reopened, reconciler), mails, reopened) == identifiers
    assert len(calls) == before
    assert judgments == []
    # Force a new analysis with entirely new wording and generation IDs.
    assert save(run(mails, reopened, reconciler, reanalyze=True), mails, reopened) == identifiers
    assert calls.count("attachment") == 2
    assert len(judgments) == 1
    saved = reopened.tasks()
    assert len(saved) == 5
    assert saved[0]["title"] == "My own name"
    assert saved[0]["completed_minutes"] == 30
    assert saved[0]["remaining_minutes"] == 60
    assert saved[0]["due_at"] == "2026-10-09T19:00:00+08:00"


def test_reanalyzing_one_copy_refreshes_all_copies_in_next_batch(tmp_path, monkeypatch):
    mails, calls, judgments, reconciler = setup_sources(monkeypatch)
    store = Store(tmp_path / "tasks.sqlite3")
    session_cache = {}
    first = run(mails, store, reconciler, message_cache=session_cache)
    old_ids = {item["_source_item_id"] for item in first[0]["result"]["tasks"]}
    fresh = run(mails, store, reconciler, message_ids=["a"], message_cache=session_cache,
                reanalyze=True)
    new_ids = {item["_source_item_id"] for item in fresh[0]["result"]["tasks"]}
    assert old_ids.isdisjoint(new_ids)
    before = len(calls)
    together = run(mails, store, reconciler, message_cache=session_cache)
    assert len(calls) == before and judgments == []
    assert all({item["_source_item_id"] for item in entry["result"]["tasks"]} == new_ids
               for entry in together)
    assert len(batch_ui.review_rows(together, mails)[0]) == 5


def test_real_review_screen_uses_one_document_analysis_and_saves_five_tasks(tmp_path, monkeypatch):
    mails, calls, judgments, _reconciler = setup_sources(monkeypatch)
    monkeypatch.setenv("SOURCE_PIPELINE_TEST_DB", str(tmp_path / "tasks.sqlite3"))

    # This test owns the real task/source/dedup/review path. SDK delegation and
    # fixed-event classification have separate tests; neither may call a provider here.
    def analyze_events(selected_mails, account, api_key, model, cache, *, reanalyze=False):
        assert account == ACCOUNT
        return {"events": [], "warnings": [], "complete": True, "results": [
            {"mail_id": mail["id"], "events": [], "warnings": [], "cached": False}
            for mail in selected_mails
        ]}

    def run_agents(task_action, event_action, api_key, model):
        return {"status": "completed", "task_result": task_action(),
                "event_result": event_action(), "events": [], "usage": {}, "warnings": []}

    monkeypatch.setattr(batch_ui, "analyze_fixed_event_messages", analyze_events)
    monkeypatch.setattr(batch_ui, "run_inbox_agents", run_agents)
    app = AppTest.from_string('''
import os
from pathlib import Path
import streamlit as st
from studentflow.batch_ui import render_batch_extraction
from studentflow.storage import Store
st.session_state.setdefault('candidates', {})
st.session_state.setdefault('gmail_profile', {'email_address': 'student@example.invalid'})
headers = [{'id': key, 'subject': mail['subject'], 'sender': 'teacher@example.invalid'}
           for key, mail in st.session_state.mails.items()]
render_batch_extraction(headers, Path('fake-client'), Path('fake-token'),
    Store(Path(os.environ['SOURCE_PIPELINE_TEST_DB'])), 'test-key', 'model-test', True)
''')
    app.session_state["mails"] = mails
    app.run(timeout=30)
    assert not app.exception
    app.button(key="batch_all").click().run()
    app.checkbox(key="batch_consent").check().run()
    app.button(key="batch_extract").click().run(timeout=30)
    assert not app.exception
    assert len(app.dataframe[0].value) == 5
    assert calls.count("attachment") == 1 and judgments == []
    version = app.session_state["batch_review_version"]
    app.session_state[f"batch_editor_{version}"] = {
        "edited_rows": {index: {"Include": True} for index in range(5)},
        "added_rows": [], "deleted_rows": [],
    }
    app.checkbox(key=f"batch_reviewed_{version}").check()
    next(button for button in app.button if button.label == "Save selected tasks").click().run()
    assert not app.exception
    store = Store(tmp_path / "tasks.sqlite3")
    assert len(store.tasks()) == 5
    calls_before = list(calls)
    app.button(key="batch_extract").click().run(timeout=30)
    assert not app.exception
    assert len(app.dataframe[0].value) == 5
    assert calls == calls_before and judgments == []


def test_preexisting_legacy_duplicates_do_not_create_another_saved_copy(tmp_path, monkeypatch):
    mails, _calls, judgments, reconciler = setup_sources(monkeypatch)
    store = Store(tmp_path / "tasks.sqlite3")
    identifiers = save(run(mails, store, reconciler), mails, store)
    # Recreate two older saved copies without canonical source IDs. This changes
    # only the synthetic database, as if both had been saved before this update.
    first = store.tasks()[0]
    for source in first["sources"]:
        source.pop("source_key", None)
        source.pop("source_item_id", None)
    second = deepcopy(first)
    second.update(id="older-duplicate", title="Earlier duplicate name")
    with store.connect() as db:
        db.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(first), first["id"]))
        db.execute("INSERT INTO tasks VALUES (?, ?)", (second["id"], json.dumps(second)))
    assert len(store.tasks()) == 6
    results = run(mails, store, reconciler)
    returned = save(results, mails, store)
    assert judgments  # Legacy copies require evidence-based reconciliation.
    assert len(store.tasks()) == 6  # Existing copies remain; no new one is added.
    assert returned[0] in {identifiers[0], "older-duplicate"}
