"""Source-bound batch duplicate regression tests, using synthetic mail only."""

import io
from copy import deepcopy
from hashlib import sha256

import pytest
from docx import Document

from studentflow import batch_ui
from studentflow.storage import Store

DUE = "2026-10-16T17:00:00+08:00"
QUOTE = "Submit the research outline by 16 October at 17:00."
ACCOUNT = "student@example.com"


def document_bytes(text=QUOTE):
    document = Document()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def mail(identifier, *, content=None, filename="instructions.docx", body="See attached.", account=ACCOUNT):
    return {
        "id": identifier, "gmail_id": "gmail-" + identifier,
        "provider": "gmail", "gmail_account": account,
        "subject": "Course instructions" if identifier == "first" else "Attachment forwarded again",
        "sent_at": "2026-10-09T09:00:00+08:00", "body": body,
        "attachments": ([] if content is None else [{
            "id": "attachment-" + identifier, "filename": filename,
            "content": content, "warnings": [],
        }]),
    }


def task(message, *, title="Submit research outline", quote=QUOTE, due=DUE,
         minutes=60, attachment=True):
    source = message["attachments"][0] if attachment and message["attachments"] else None
    return {
        "title": title, "source_quote": quote, "due_at": due,
        "estimated_minutes": minutes, "confidence": 0.9, "needs_clarification": [],
        "source_attachment_id": source["id"] if source else None,
        "source_filename": source["filename"] if source else None,
    }


def review(*pairs):
    results, mails = [], {}
    for message, proposals in pairs:
        mails[message["id"]] = message
        results.append({
            "mail_id": message["id"], "gmail_id": message["gmail_id"],
            "cache_key": "cached-" + message["id"], "cached": True,
            "result": {"tasks": proposals, "warnings": []},
        })
    snapshot = deepcopy(results)
    prepared = batch_ui.review_rows(results, mails)
    assert results == snapshot
    return prepared, results, mails


def selected_records(rows, sources):
    edited = deepcopy(rows)
    for row in edited:
        row["Include"] = True
    return batch_ui.validate_selected_rows(edited, sources, True)


def test_forwarded_attachment_collapses_four_repeats_and_keeps_fifth_task():
    titles = ["Submit outline", "Attend quiz", "Submit essay", "Submit slides", "Attend presentation"]
    quotes = [f"{title} by 16 October at 17:00." for title in titles]
    content = document_bytes("\n".join(quotes))
    first, second = mail("first", content=content), mail("second", content=content)
    left = [task(first, title=title, quote=quote) for title, quote in zip(titles[:4], quotes[:4])]
    right = [task(second, title=title, quote=quote) for title, quote in zip(titles, quotes)]
    (rows, sources), results, messages = review((first, left), (second, right))
    snapshot = deepcopy(results)
    assert len(rows) == 5
    assert [row["Task"] for row in rows] == titles
    assert rows[0]["_row_id"] == "cached-first:0:gmail-first"
    assert "Duplicate mentions combined (2)." in rows[0]["Review notes"]
    assert "Course instructions" in rows[0]["Source message"]
    assert "Attachment forwarded again" in rows[0]["Source message"]
    assert rows[0]["Source file"] == "instructions.docx"
    records = selected_records(rows, sources)
    for record in records[:4]:
        assert {item["mail_id"] for item in record["sources"]} == {"first", "second"}
        assert {item["attachment_sha256"] for item in record["sources"]} == {sha256(content).hexdigest()}
    assert len(records[-1]["sources"]) == 1
    # The cache remains the source of the original model responses, not a UI edit buffer.
    batch_ui.review_rows(results, messages)
    assert results == snapshot


def test_same_message_body_and_attachment_keep_both_verified_sources():
    body_quote = "Please submit the research outline by Friday at 17:00."
    message = mail("first", content=document_bytes(), body=body_quote)
    body_task = task(message, quote=body_quote, attachment=False)
    file_task = task(message)
    (rows, sources), _, _ = review((message, [body_task, file_task]))
    assert len(rows) == 1
    assert "Message text" in rows[0]["Source file"]
    assert "instructions.docx" in rows[0]["Source file"]
    record = selected_records(rows, sources)[0]
    assert {item["source_quote"] for item in record["sources"]} == {body_quote, QUOTE}
    assert {item["source_attachment_id"] for item in record["sources"]} == {None, "attachment-first"}


def test_attachment_rename_and_equivalent_timezone_still_identify_repeat():
    content = document_bytes()
    first, second = mail("first", content=content), mail("second", content=content, filename="copy.docx")
    (rows, sources), _, _ = review(
        (first, [task(first)]),
        (second, [task(second, title="  submit   research outline.  ", due="2026-10-16T09:00:00Z")]),
    )
    assert len(rows) == 1
    record = selected_records(rows, sources)[0]
    assert {source["source_filename"] for source in record["sources"]} == {"instructions.docx", "copy.docx"}


@pytest.mark.parametrize("difference", [
    "same_filename_changed_bytes", "deadline", "effort", "account", "unrelated_bodies", "course",
])
def test_shared_task_words_do_not_hide_distinct_assignments(difference):
    text = QUOTE + "\nMATH101 report instructions.\nHIST201 report instructions."
    content = document_bytes(text)
    first, second = mail("first", content=content), mail("second", content=content)
    left, right = task(first), task(second)
    if difference == "same_filename_changed_bytes":
        second["attachments"][0]["content"] = document_bytes(text + "\nDifferent course material.")
        # Provider/attachment IDs alone must not prove equality.
        second["attachments"][0]["id"] = first["attachments"][0]["id"]
        right["source_attachment_id"] = first["attachments"][0]["id"]
    elif difference == "deadline":
        right["due_at"] = "2026-10-17T17:00:00+08:00"
    elif difference == "effort":
        right["estimated_minutes"] = 120
    elif difference == "account":
        second["gmail_account"] = "other@example.com"
    elif difference == "unrelated_bodies":
        for message, proposal in ((first, left), (second, right)):
            message["body"] = QUOTE
            message["attachments"] = []
            proposal["source_attachment_id"] = None
            proposal["source_filename"] = None
    elif difference == "course":
        left["source_quote"] = "MATH101 report instructions."
        right["source_quote"] = "HIST201 report instructions."
    (rows, sources), _, _ = review((first, [left]), (second, [right]))
    assert len(rows) == 2
    assert len(selected_records(rows, sources)) == 2


def test_missing_deadline_requires_matching_evidence_not_just_shared_file():
    second_quote = "Submit a second research outline when requested."
    content = document_bytes(QUOTE + "\n" + second_quote)
    first, second = mail("first", content=content), mail("second", content=content)
    (rows, _), _, _ = review(
        (first, [task(first, due=None)]), (second, [task(second, due=None, quote=second_quote)]),
    )
    assert len(rows) == 2
    (rows, _), _, _ = review((first, [task(first, due=None)]), (second, [task(second, due=None)]))
    assert len(rows) == 1


def test_forged_editor_provenance_does_not_replace_protected_evidence():
    content = document_bytes()
    first, second = mail("first", content=content), mail("second", content=content)
    (rows, sources), _, _ = review((first, [task(first)]), (second, [task(second)]))
    rows[0]["Source file"] = "forged.pdf"
    rows[0]["Source message"] = "forged subject"
    rows[0]["sources"] = [{"mail_id": "forged", "source_quote": "forged"}]
    record = selected_records(rows, sources)[0]
    assert record["source_filename"] == "instructions.docx"
    assert {item["mail_id"] for item in record["sources"]} == {"first", "second"}
    rows[0]["Supporting text"] = "Invented text."
    with pytest.raises(ValueError, match="must match"):
        selected_records(rows, sources)


def test_forwarded_repeat_saved_later_preserves_user_edits_and_progress(tmp_path):
    content = document_bytes()
    first, second = mail("first", content=content), mail("second", content=content)
    (rows, sources), _, _ = review((first, [task(first)]))
    store = Store(tmp_path / "synthetic.sqlite3")
    identifier = store.save_task(selected_records(rows, sources)[0])
    edited = store.update_task(identifier, {
        "title": "My revised outline", "due_at": "2026-10-18T19:00:00+08:00",
        "estimated_minutes": 120, "completed_minutes": 30,
    })
    (rows, sources), _, _ = review((second, [task(second)]))
    assert store.save_task(selected_records(rows, sources)[0]) == identifier
    stored = Store(store.path).tasks()
    assert len(stored) == 1
    for key in ("title", "due_at", "estimated_minutes", "completed_minutes", "remaining_minutes"):
        assert stored[0][key] == edited[key]
    assert {item["mail_id"] for item in stored[0]["sources"]} == {"first", "second"}


def test_deleted_duplicate_readded_has_new_identity_and_no_old_progress(tmp_path):
    message = mail("first", content=document_bytes())
    (rows, sources), _, _ = review((message, [task(message)]))
    record = selected_records(rows, sources)[0]
    store = Store(tmp_path / "synthetic.sqlite3")
    previous = store.save_task(record)
    store.update_task(previous, {"completed_minutes": 30})
    store.delete_task(previous)
    new = store.save_task(record)
    assert new != previous
    assert store.tasks()[0]["completed_minutes"] == 0


class Session(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key) from None

    def __setattr__(self, key, value):
        self[key] = value


def test_review_layout_change_invalidates_old_row_selections_and_consent(monkeypatch):
    state = Session(batch_review_version=1)
    monkeypatch.setattr(batch_ui.st, "session_state", state)
    first, second = mail("first", body=QUOTE), mail("second", body=QUOTE)
    (rows, _), _, _ = review((first, [task(first, attachment=False)]), (second, [task(second, attachment=False)]))
    initial = batch_ui._review_version(rows)
    state[f"batch_editor_{initial}"] = {"edited_rows": {1: {"Include": True}}, "added_rows": [], "deleted_rows": []}
    state[f"batch_reviewed_{initial}"] = True
    assert batch_ui._review_version(rows) == initial
    changed = batch_ui._review_version(rows[:1])
    assert changed != initial
    assert not state.get(f"batch_reviewed_{changed}", False)
    assert not state.get(f"batch_editor_{changed}", {}).get("edited_rows")


def test_existing_browser_session_loses_pre_fix_row_index_selections(monkeypatch):
    state = Session(
        batch_review_version=4,
        batch_editor_4={"edited_rows": {7: {"Include": True}}, "added_rows": [], "deleted_rows": []},
        batch_reviewed_4=True,
    )
    monkeypatch.setattr(batch_ui.st, "session_state", state)
    message = mail("first", body=QUOTE)
    (rows, _), _, _ = review((message, [task(message, attachment=False)]))
    version = batch_ui._review_version(rows)
    assert version != 4
    assert not state.get(f"batch_reviewed_{version}", False)
    assert not state.get(f"batch_editor_{version}", {}).get("edited_rows")
    assert "batch_editor_4" not in state
    assert "batch_reviewed_4" not in state


def test_same_row_count_different_order_cannot_reuse_row_index_edits(monkeypatch):
    state = Session(batch_review_version=1)
    monkeypatch.setattr(batch_ui.st, "session_state", state)
    first, second = mail("first", body=QUOTE), mail("second", body=QUOTE)
    (rows, _), _, _ = review((first, [task(first, attachment=False)]), (second, [task(second, attachment=False)]))
    initial = batch_ui._review_version(rows)
    state[f"batch_editor_{initial}"] = {"edited_rows": {0: {"Include": True}}, "added_rows": [], "deleted_rows": []}
    state[f"batch_reviewed_{initial}"] = True
    assert batch_ui._review_version(rows[::-1]) != initial
    assert f"batch_editor_{initial}" not in state


def test_generic_evidence_cannot_bridge_explicitly_different_courses():
    content = document_bytes("MATH101 report instructions.\n" + QUOTE + "\nHIST201 report instructions.")
    messages = [mail(identifier, content=content) for identifier in ("first", "second", "third")]
    quotes = ["MATH101 report instructions.", QUOTE, "HIST201 report instructions."]
    (rows, sources), _, _ = review(*[(message, [task(message, quote=quote)]) for message, quote in zip(messages, quotes)])
    assert len(rows) == 2
    for record in selected_records(rows, sources):
        evidence = " ".join(source["source_quote"] for source in record["sources"])
        assert not ("MATH101" in evidence and "HIST201" in evidence)

