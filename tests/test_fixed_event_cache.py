"""Successful event extraction is cached without crossing sources or saving events."""

import json
import sqlite3
from copy import deepcopy
from unittest.mock import Mock

import pytest

from studentflow import fixed_events
from studentflow.extraction_cache import ExtractionCache
from studentflow.fixed_event_cache import (
    FixedEventCache,
    analyze_fixed_event_messages,
    fixed_event_cache_key,
)
from studentflow.storage import Store

ACCOUNT = "student@example.invalid"
QUOTE = "DSS5105 final exam is on 28 October 2026, 14:00-16:00 Singapore time."


def message(**changes):
    return {"id": "mail-a", "gmail_account": ACCOUNT, "subject": "Exam arrangements",
            "body": QUOTE, "sent_at": "2026-10-24T09:00:00+08:00", "attachments": [], **changes}


def result(mail=None):
    mail = mail or message()
    sources, _ = fixed_events.prepare_sources(mail, mail["gmail_account"])
    return fixed_events.validate_fixed_events({"events": [{
        "title": "Attend DSS5105 final exam", "event_key": "DSS5105 final exam", "kind": "exam",
        "course": "DSS5105", "start_at": "2026-10-28T14:00:00+08:00",
        "end_at": "2026-10-28T16:00:00+08:00", "duration_minutes": None,
        "start_evidence": "28 October 2026, 14:00", "end_evidence": "16:00",
        "duration_evidence": None, "time_basis": "absolute", "in_person": True,
        "location": "", "needs_clarification": [],
        "sources": [{"source_id": "body", "source_quote": QUOTE}],
    }]}, sources, sent_at=mail["sent_at"])


def analyze(cache, *, mails=None, extractor=None, reanalyze=False):
    return analyze_fixed_event_messages(mails or [message()], ACCOUNT, "synthetic-key",
                                        "test-model", cache, reanalyze=reanalyze,
                                        extractor=extractor)


def test_key_normalizes_account_case_and_is_stable():
    assert fixed_event_cache_key(" Student@Example.Invalid ", message(), "test-model") == (
        fixed_event_cache_key(ACCOUNT, message(), "test-model"))


@pytest.mark.parametrize("changes", [
    {"id": "mail-b"}, {"body": QUOTE + " Revised announcement."},
    {"subject": "Updated exam arrangements"}, {"sent_at": "2026-10-25T09:00:00+08:00"},
    {"attachments": [{"id": "a", "filename": "exam.txt", "content": b"test"}]},
])
def test_key_changes_with_source_context(changes):
    assert fixed_event_cache_key(ACCOUNT, message(**changes), "test-model") != (
        fixed_event_cache_key(ACCOUNT, message(), "test-model"))


@pytest.mark.parametrize("field,value", [
    ("FIXED_EVENTS_VERSION", "new-version"), ("SYSTEM_PROMPT", "new extraction prompt"),
])
def test_key_changes_with_extraction_contract(monkeypatch, field, value):
    before = fixed_event_cache_key(ACCOUNT, message(), "test-model")
    monkeypatch.setattr(fixed_events, field, value)
    assert before != fixed_event_cache_key(ACCOUNT, message(), "test-model")


def test_key_changes_with_account_model_attachment_bytes_and_warnings():
    baseline = fixed_event_cache_key(ACCOUNT, message(), "test-model")
    assert baseline != fixed_event_cache_key("other@example.invalid", message(), "test-model")
    assert baseline != fixed_event_cache_key(ACCOUNT, message(), "other-model")
    attachment = {"id": "a", "filename": "exam.txt", "content": b"test", "warnings": []}
    original = fixed_event_cache_key(ACCOUNT, message(attachments=[attachment]), "test-model")
    for changes in ({"id": "b"}, {"filename": "different.txt"}, {"content": b"changed"},
                    {"warnings": ["Some text could not be read"]}):
        assert original != fixed_event_cache_key(
            ACCOUNT, message(attachments=[attachment | changes]), "test-model")


def test_cache_preserves_identity_and_evidence_across_restart_without_aliasing(tmp_path):
    path = tmp_path / "cache.sqlite3"
    cache = FixedEventCache(path)
    expected = result()
    before = deepcopy(expected)
    cache.put("key", expected)
    assert expected == before
    assert FixedEventCache(path).get("key") == expected
    recovered = cache.get("key")
    recovered["events"][0]["sources"][0]["source_quote"] = "Edited in the UI"
    assert cache.get("key") == expected
    assert cache.get("missing") is None


def test_successful_empty_result_is_reused_without_another_call(tmp_path):
    cache = FixedEventCache(tmp_path / "cache.sqlite3")
    extractor = Mock(return_value={"events": [], "warnings": []})
    first = analyze(cache, extractor=extractor)
    second = analyze(FixedEventCache(cache.path), extractor=extractor)
    assert first["complete"] and second["complete"]
    assert second["events"] == [] and second["results"][0]["cached"]
    extractor.assert_called_once()


@pytest.mark.parametrize("payload", [
    "not json", "null", "[]", '{"events": "wrong", "warnings": []}',
    '{"events": [], "warnings": "wrong"}', '{"events": [], "warnings": [1]}',
    '{"events": ["wrong"], "warnings": []}',
])
def test_malformed_cache_is_a_miss(tmp_path, payload):
    cache = FixedEventCache(tmp_path / "cache.sqlite3")
    with cache.connect() as db:
        db.execute("INSERT INTO fixed_event_extraction_cache VALUES (?, ?)", ("bad", payload))
    assert cache.get("bad") is None


@pytest.mark.parametrize("changes", [
    {"status": "confirmed"}, {"sources": []}, {"_extraction_identity": {}},
    {"start_at": "2026-10-28T14:00:00"}, {"duration_minutes": True},
    {"travel_minutes": 40}, {"needs_clarification": "wrong"}, {"in_person": "yes"},
])
def test_invalid_proposals_are_not_cached(tmp_path, changes):
    cache = FixedEventCache(tmp_path / "cache.sqlite3")
    invalid = result()
    invalid["events"][0].update(changes)
    with pytest.raises((TypeError, ValueError)):
        cache.put("key", invalid)
    assert cache.get("key") is None


def test_only_validated_proposal_fields_are_persisted(tmp_path):
    cache = FixedEventCache(tmp_path / "cache.sqlite3")
    expected = result()
    untrusted = deepcopy(expected)
    untrusted.update(api_key="secret", raw_mail="unrelated whole email")
    untrusted["events"][0].update(api_key="secret", raw_body="whole email", content=b"raw")
    untrusted["events"][0]["sources"][0]["raw_body"] = "whole email"
    untrusted["events"][0]["_extraction_identity"]["api_key"] = "secret"
    cache.put("key", untrusted)
    assert cache.get("key") == expected


def test_partial_failure_keeps_successes_and_retries_only_failed_mail(tmp_path):
    cache = FixedEventCache(tmp_path / "cache.sqlite3")
    mails = [message(), message(id="mail-b")]
    attempts = []

    def extractor(mail, api_key, model, *, account):
        attempts.append(mail["id"])
        if mail["id"] == "mail-b" and attempts.count("mail-b") == 1:
            raise RuntimeError("secret provider details " + api_key + mail["body"])
        return result(mail)

    first = analyze(cache, mails=mails, extractor=extractor)
    assert not first["complete"] and len(first["events"]) == 1
    assert first["results"][1]["error"]
    assert "secret" not in first["results"][1]["error"]
    assert QUOTE not in first["results"][1]["error"]
    assert cache.get(fixed_event_cache_key(ACCOUNT, mails[1], "test-model")) is None
    second = analyze(cache, mails=mails, extractor=extractor)
    assert second["complete"] and len(second["events"]) == 2
    assert [item["cached"] for item in second["results"]] == [True, False]
    assert attempts == ["mail-a", "mail-b", "mail-b"]


def test_failed_forced_reanalysis_does_not_overwrite_previous_success(tmp_path):
    cache = FixedEventCache(tmp_path / "cache.sqlite3")
    analyze(cache, extractor=Mock(return_value=result()))
    failed = analyze(cache, reanalyze=True, extractor=Mock(side_effect=RuntimeError("private")))
    assert not failed["complete"]
    extractor = Mock(side_effect=AssertionError("must reuse previous success"))
    resumed = analyze(cache, extractor=extractor)
    assert resumed["complete"] and resumed["results"][0]["cached"]
    assert resumed["events"] == result()["events"]
    extractor.assert_not_called()


def test_successful_reanalysis_replaces_cache(tmp_path):
    cache = FixedEventCache(tmp_path / "cache.sqlite3")
    analyze(cache, extractor=Mock(return_value=result()))
    extractor = Mock(return_value={"events": [], "warnings": []})
    refreshed = analyze(cache, reanalyze=True, extractor=extractor)
    assert refreshed["complete"] and not refreshed["results"][0]["cached"]
    assert cache.get(fixed_event_cache_key(ACCOUNT, message(), "test-model"))["events"] == []


@pytest.mark.parametrize("corruption", ["account", "mail", "quote", "attachment", "page"])
def test_cached_provenance_must_match_selected_source(tmp_path, corruption):
    cache = FixedEventCache(tmp_path / "cache.sqlite3")
    poisoned = result()
    event = poisoned["events"][0]
    ref = event["sources"][0]
    if corruption == "account":
        event["account"] = ref["account"] = "other@example.invalid"
    elif corruption == "mail":
        ref["mail_id"] = "other-message"
    elif corruption == "quote":
        ref["source_quote"] = "A fabricated supporting statement"
    elif corruption == "attachment":
        ref.update(source_attachment_id="fake", source_filename="fake.pdf", attachment_sha256="fake")
    else:
        ref["source_page"] = 42
    cache.put(fixed_event_cache_key(ACCOUNT, message(), "test-model"), poisoned)
    extractor = Mock(return_value=result())
    recovered = analyze(cache, extractor=extractor)
    assert recovered["complete"] and not recovered["results"][0]["cached"]
    assert recovered["events"] == result()["events"]
    extractor.assert_called_once()


def test_fresh_wrong_source_result_is_never_cached(tmp_path):
    cache = FixedEventCache(tmp_path / "cache.sqlite3")
    invalid = result(message(id="other-mail"))
    extracted = analyze(cache, extractor=Mock(return_value=invalid))
    assert not extracted["complete"] and not extracted["events"]
    assert cache.get(fixed_event_cache_key(ACCOUNT, message(), "test-model")) is None


def test_wrong_mail_account_cannot_use_even_empty_cache(tmp_path):
    cache = FixedEventCache(tmp_path / "cache.sqlite3")
    mail = message(gmail_account="other@example.invalid")
    cache.put(fixed_event_cache_key(ACCOUNT, mail, "test-model"), {"events": [], "warnings": []})
    extractor = Mock()
    extracted = analyze(cache, mails=[mail], extractor=extractor)
    assert not extracted["complete"] and not extracted["events"]
    extractor.assert_not_called()


def test_default_extractor_is_resolved_at_call_time(tmp_path, monkeypatch):
    extractor = Mock(return_value=result())
    monkeypatch.setattr(fixed_events, "extract_fixed_events", extractor)
    extracted = analyze(FixedEventCache(tmp_path / "cache.sqlite3"))
    assert extracted["complete"]
    extractor.assert_called_once()


def test_duplicate_mentions_and_warnings_are_combined(tmp_path):
    cache = FixedEventCache(tmp_path / "cache.sqlite3")
    response = result()
    response["events"].append(deepcopy(response["events"][0]))
    response["warnings"] = ["Check times", "Check times"]
    extracted = analyze(cache, extractor=Mock(return_value=response))
    assert extracted["complete"] and len(extracted["events"]) == 1
    assert extracted["warnings"] == ["Check times"]


def test_attachment_quote_page_digest_and_identity_survive_cached_reuse(tmp_path, monkeypatch):
    original = result()["events"][0]
    mail = message(body="Please read the attached exam notice.", attachments=[{
        "id": "attachment-a", "filename": "exam.pdf", "content": b"synthetic PDF fixture",
    }])
    monkeypatch.setattr(fixed_events, "message_documents", lambda _: [{
        "id": "attachment-a", "filename": "exam.pdf", "text": QUOTE,
        "pages": [{"page": 2, "text": QUOTE}], "warnings": [],
    }])
    sources, _ = fixed_events.prepare_sources(mail, ACCOUNT)
    proposal = original | {
        "start_evidence": "28 October 2026, 14:00", "end_evidence": "16:00",
        "time_basis": "absolute", "sources": [{"source_id": "attachment-0", "source_quote": QUOTE}],
    }
    expected = fixed_events.validate_fixed_events({"events": [proposal]}, sources)
    extractor = Mock(return_value=expected)
    cache = FixedEventCache(tmp_path / "cache.sqlite3")
    first = analyze(cache, mails=[mail], extractor=extractor)
    second = analyze(FixedEventCache(cache.path), mails=[mail], extractor=extractor)
    assert first["complete"] and second["complete"] and second["results"][0]["cached"]
    assert second["events"] == expected["events"]
    evidence = second["events"][0]["sources"][0]
    assert evidence["source_page"] == 2 and evidence["source_filename"] == "exam.pdf"
    assert len(evidence["attachment_sha256"]) == 64
    extractor.assert_called_once()


def test_cache_coexists_with_saved_tasks_events_and_task_cache(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    store.save_task({"title": "Existing task", "mail_id": "synthetic", "status": "pending"})
    saved = result()["events"]
    store.save_fixed_events(saved)
    before = (store.tasks(), store.list_fixed_events())
    task_cache = ExtractionCache(path)
    task_cache.put("tasks", {"tasks": [], "warnings": []})
    cache = FixedEventCache(path)
    analyze(cache, extractor=Mock(return_value=result()))
    assert (Store(path).tasks(), Store(path).list_fixed_events()) == before
    assert task_cache.get("tasks") == {"tasks": [], "warnings": []}


def test_failed_atomic_write_preserves_previous_cache(tmp_path):
    cache = FixedEventCache(tmp_path / "cache.sqlite3")
    cache.put("key", result())
    before = cache.get("key")
    with cache.connect() as db:
        db.execute("CREATE TRIGGER reject_cache BEFORE UPDATE ON fixed_event_extraction_cache "
                   "BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        cache.put("key", {"events": [], "warnings": []})
    assert cache.get("key") == before


def test_corrupt_proposal_identity_in_database_is_a_cache_miss(tmp_path):
    cache = FixedEventCache(tmp_path / "cache.sqlite3")
    response = result()
    response["events"][0]["_extraction_identity"]["title"] = "Unrelated edited identity"
    with cache.connect() as db:
        db.execute("INSERT INTO fixed_event_extraction_cache VALUES (?, ?)", ("bad", json.dumps(response)))
    assert cache.get("bad") is None


@pytest.mark.parametrize("operation", ["get", "put"])
def test_cache_io_failure_preserves_validated_proposals(tmp_path, monkeypatch, operation):
    cache = FixedEventCache(tmp_path / "cache.sqlite3")
    monkeypatch.setattr(cache, operation, Mock(side_effect=OSError("private path and secret")))
    extracted = analyze(cache, extractor=Mock(return_value=result()))
    assert extracted["complete"] and extracted["events"] == result()["events"]
    assert "error" not in extracted["results"][0]
    assert any("cache was unavailable" in note for note in extracted["warnings"])
    assert "private" not in str(extracted["warnings"])


def test_missing_cache_after_initialization_failure_still_allows_review():
    extracted = analyze(None, extractor=Mock(return_value=result()))
    assert extracted["complete"] and extracted["events"] == result()["events"]
    assert any("cache was unavailable" in note for note in extracted["warnings"])


def test_cache_closes_database_handles_after_create_write_and_read(tmp_path, monkeypatch):
    connect = sqlite3.connect
    connections = []

    def tracked_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    cache = FixedEventCache(tmp_path / "cache.sqlite3")
    cache.put("key", result())
    assert cache.get("key") == result()
    assert cache.get("missing") is None
    assert len(connections) == 4
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
