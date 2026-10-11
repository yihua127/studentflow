"""Repeat extraction is idempotent without overwriting reviewed task details."""

import hashlib
import json
import sqlite3

import pytest

from studentflow.storage import Store

CONTENT = b"A synthetic course syllabus with an outline deadline."
FINGERPRINT = hashlib.sha256(CONTENT).hexdigest()


def proposal(mail_id="mail-one", **changes):
    source = {
        "mail_id": mail_id, "gmail_account": "student@example.invalid",
        "mail_subject": "Course syllabus", "source_attachment_id": "attachment-one",
        "source_filename": "syllabus.pdf", "source_quote": "Submit research outline.",
        "attachment_sha256": FINGERPRINT,
    }
    return {
        **source, "title": "Submit research outline", "due_at": "2030-01-04T17:00:00+08:00",
        "estimated_minutes": 60, "status": "confirmed", "sources": [source], **changes,
    }


def legacy(record):
    return {key: value for key, value in record.items() if key not in {"sources", "attachment_sha256"}}


def test_forwarded_attachment_collapses_and_unions_sources_across_restart(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    first = store.save_task(proposal())
    reopened = Store(store.path)
    repeated = reopened.save_task(proposal("forwarded-mail"))
    assert repeated == first
    [saved] = reopened.tasks()
    assert {source["mail_id"] for source in saved["sources"]} == {"mail-one", "forwarded-mail"}
    assert saved["mail_id"] == "mail-one"


def test_repeat_preserves_manual_edits_completion_and_primary_evidence(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    identifier = store.save_task(proposal())
    store.update_task(identifier, {
        "title": "Outline reviewed with tutor", "due_at": "2030-01-03T15:00:00+08:00",
        "estimated_minutes": 75, "completed_minutes": 75,
    })
    before = store.tasks()[0]
    assert store.save_task(proposal("forwarded-mail")) == identifier
    [after] = store.tasks()
    for field in ("id", "title", "due_at", "estimated_minutes", "completed_minutes", "is_completed",
                  "remaining_minutes", "status", "mail_id", "source_quote", "source_attachment_id"):
        assert after[field] == before[field]
    assert len(after["sources"]) == 2


def test_same_batch_proven_repeats_return_same_id(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    identifiers = store.save_tasks([proposal(), proposal("second-mail"), proposal("third-mail")])
    assert len(set(identifiers)) == 1
    assert len(store.tasks()) == 1
    assert len(store.tasks()[0]["sources"]) == 3


def test_same_source_and_title_different_deadline_keeps_two_tasks(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    one = store.save_task(proposal())
    two = store.save_task(proposal(due_at="2030-01-05T17:00:00+08:00"))
    assert one != two
    assert len(store.tasks()) == 2


def test_body_and_attachment_evidence_in_same_mail_collapse(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    body = proposal()
    for key in ("source_attachment_id", "source_filename", "attachment_sha256"):
        body.pop(key)
        body["sources"][0].pop(key)
    identifier = store.save_task(body)
    assert store.save_task(proposal()) == identifier
    [saved] = store.tasks()
    assert len(saved["sources"]) == 2
    assert saved.get("source_attachment_id") is None


def test_conflicting_estimates_and_different_accounts_are_kept_separate(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    first = store.save_task(proposal())
    second = store.save_task(proposal(estimated_minutes=90))
    other_account = proposal()
    other_account["gmail_account"] = "other@example.invalid"
    other_account["sources"][0]["gmail_account"] = "other@example.invalid"
    third = store.save_task(other_account)
    assert len({first, second, third}) == 3


def test_same_filename_and_attachment_identifier_are_not_content_proof(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    one = proposal()
    two = proposal("other-mail")
    one["sources"][0].pop("attachment_sha256")
    two["sources"][0].pop("attachment_sha256")
    one.pop("attachment_sha256")
    two.pop("attachment_sha256")
    assert store.save_task(one) != store.save_task(two)
    assert len(store.tasks()) == 2


def test_legacy_attachment_record_uses_saved_bytes_as_proof(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    old = legacy(proposal())
    identifier = store.save_task(old)
    store.update_task(identifier, {"completed_minutes": 60})
    with store.connect() as db:
        db.execute("CREATE TABLE attachment_documents (library_id TEXT PRIMARY KEY, payload TEXT, content BLOB)")
        db.execute("INSERT INTO attachment_documents VALUES (?, ?, ?)", (
            "file-one", json.dumps({"id": "attachment-one", "mail_id": "mail-one",
                                    "gmail_account": "student@example.invalid"}), CONTENT,
        ))
    assert store.save_task(proposal("forwarded-mail")) == identifier
    [saved] = store.tasks()
    assert saved["completed_minutes"] == 60
    assert len(saved["sources"]) == 2


def test_realistic_legacy_attachment_missing_account_recovers_unique_library_scope(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    old = legacy(proposal())
    old.pop("gmail_account")
    old["source_name"] = "Gmail"
    identifier = store.save_task(old)
    with store.connect() as db:
        db.execute("CREATE TABLE attachment_documents (library_id TEXT PRIMARY KEY, payload TEXT, content BLOB)")
        db.execute("INSERT INTO attachment_documents VALUES (?, ?, ?)", (
            "file-one", json.dumps({"id": "attachment-one", "mail_id": "mail-one",
                                    "gmail_account": "student@example.invalid"}), CONTENT,
        ))
    assert store.save_task(proposal("forwarded-mail")) == identifier
    [saved] = store.tasks()
    assert {source["gmail_account"] for source in saved["sources"]} == {"student@example.invalid"}
    assert "gmail_account" not in saved  # Primary legacy metadata stays intact.


@pytest.mark.parametrize("other_account,other_content", [
    ("other@example.invalid", CONTENT),
    ("student@example.invalid", b"Different content"),
    (None, CONTENT),
    ("student@example.invalid", b""),
])
def test_missing_legacy_account_rejects_ambiguous_library(tmp_path, other_account, other_content):
    store = Store(tmp_path / "state.sqlite3")
    old = legacy(proposal())
    old.pop("gmail_account")
    old["source_name"] = "Gmail"
    identifier = store.save_task(old)
    with store.connect() as db:
        db.execute("CREATE TABLE attachment_documents (library_id TEXT PRIMARY KEY, payload TEXT, content BLOB)")
        for index, account, content in [(1, "student@example.invalid", CONTENT),
                                        (2, other_account, other_content)]:
            db.execute("INSERT INTO attachment_documents VALUES (?, ?, ?)", (
                str(index), json.dumps({"id": "attachment-one", "mail_id": "mail-one",
                                        "gmail_account": account}), content,
            ))
    assert store.save_task(proposal("forwarded-mail")) != identifier


def body_proposal():
    record = proposal(hashlib.sha256(b"Synthetic raw Gmail message").hexdigest())
    record["source_name"] = "Gmail"
    for key in ("source_attachment_id", "source_filename", "attachment_sha256"):
        record.pop(key)
        record["sources"][0].pop(key)
    return record


def test_realistic_legacy_gmail_body_repeat_recovers_account_from_identical_raw_message(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    current = body_proposal()
    old = legacy(current)
    old.pop("gmail_account")
    identifier = store.save_task(old)
    store.update_task(identifier, {"completed_minutes": 60})
    assert store.save_task(current) == identifier
    [saved] = store.tasks()
    assert saved["completed_minutes"] == 60
    assert len(saved["sources"]) == 1
    assert saved["sources"][0]["gmail_account"] == "student@example.invalid"
    assert store.save_task(current) == identifier


@pytest.mark.parametrize("change", ["account", "short_id", "quote", "source_kind", "not_gmail"])
def test_legacy_body_account_binding_requires_exact_proof(tmp_path, change):
    store = Store(tmp_path / "state.sqlite3")
    current = body_proposal()
    old = legacy(current)
    old.pop("gmail_account")
    if change == "account":
        old["gmail_account"] = "other@example.invalid"
    elif change == "short_id":
        old["mail_id"] = current["mail_id"] = current["sources"][0]["mail_id"] = "mail-one"
    elif change == "quote":
        current["sources"][0]["source_quote"] = "Another instruction."
    elif change == "source_kind":
        current["sources"][0]["source_attachment_id"] = "attachment-one"
    elif change == "not_gmail":
        old["source_name"] = "local.eml"
    identifier = store.save_task(old)
    assert store.save_task(current) != identifier


@pytest.mark.parametrize("content,account", [
    (b"Different syllabus bytes", "student@example.invalid"),
    (CONTENT, "different@example.invalid"),
    (b"", "student@example.invalid"),
])
def test_legacy_lookup_rejects_unrelated_bytes_account_or_empty_file(tmp_path, content, account):
    store = Store(tmp_path / "state.sqlite3")
    old_id = store.save_task(legacy(proposal()))
    with store.connect() as db:
        db.execute("CREATE TABLE attachment_documents (library_id TEXT PRIMARY KEY, payload TEXT, content BLOB)")
        db.execute("INSERT INTO attachment_documents VALUES (?, ?, ?)", (
            "file-one", json.dumps({"id": "attachment-one", "mail_id": "mail-one",
                                    "gmail_account": account}), content,
        ))
    assert store.save_task(proposal("forwarded-mail")) != old_id
    assert len(store.tasks()) == 2


def test_existing_duplicate_records_are_not_deleted_or_automigrated(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    first = store.save_task(legacy(proposal()))
    second = store.save_task(legacy(proposal("forwarded-mail")))
    original = store.tasks()
    assert Store(store.path).tasks() == original
    assert store.save_task(proposal()) == first
    assert {record["id"] for record in store.tasks()} == {first, second}
    assert store.tasks()[1] == original[1]


def test_deleted_reviewed_task_gets_new_identity_and_fresh_progress(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    deleted_id = store.save_task(proposal(completed_minutes=60))
    store.delete_task(deleted_id)
    new_id = store.save_task(proposal())
    assert new_id != deleted_id
    assert store.tasks()[0]["completed_minutes"] == 0


def test_idempotent_save_does_not_change_version_or_metadata(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    identifier = store.save_task(proposal())
    before = store.tasks()
    assert store.save_task(proposal()) == identifier
    assert store.tasks() == before


def test_source_union_is_atomic_if_later_save_fails(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.save_task(proposal())
    before = store.tasks()
    with store.connect() as db:
        db.execute("CREATE TRIGGER reject_record BEFORE INSERT ON tasks "
                   "WHEN json_extract(NEW.payload, '$.title') = 'Rejected' "
                   "BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.save_tasks([proposal("forwarded-mail"), proposal("unrelated", title="Rejected")])
    assert store.tasks() == before


def test_sources_are_sanitized_before_persistence(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    record = proposal()
    record["sources"][0].update(raw_message="Unrelated body", content=b"Unrelated bytes", api_key="not-a-key")
    store.save_task(record)
    [source] = store.tasks()[0]["sources"]
    assert not {"raw_message", "content", "api_key"} & source.keys()
