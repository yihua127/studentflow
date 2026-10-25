"""Whole inbox cache invalidation, completion checks and credential boundaries."""

import json
import sqlite3
from copy import deepcopy

import pytest

from studentflow import effort_estimation, inbox_cache
from studentflow.inbox_cache import InboxAnalysisCache, analysis_cache_key

ACCOUNT = "student@example.invalid"


def mail(**changes):
    return {"id": "local-a", "gmail_id": "gmail-a", "body": "Prepare for the exam.",
            "subject": "Exam arrangements", "sent_at": "2026-10-24T10:00:00+08:00",
            "attachments": [], **changes}


def key(**changes):
    values = {"account": ACCOUNT, "mails": [mail()], "model": "test-model",
              "references": [{"id": 1, "title": "Saved task", "estimated_minutes": 30}],
              "suggest_effort": True}
    return analysis_cache_key(**(values | changes))


def completed(**changes):
    return {"status": "completed", "task_result": {
        "status": "completed", "results": [], "effort": {"estimates": {}, "warnings": []},
    }, "event_result": {"status": "completed", "complete": True, "events": [],
                        "results": [], "warnings": []},
        "events": [], "usage": {"model_calls": 7, "input_tokens": 20, "output_tokens": 10},
        "warnings": [], **changes}


def insert_raw(cache, payload):
    with sqlite3.connect(cache.path) as db:
        db.execute("INSERT OR REPLACE INTO inbox_analysis_cache VALUES (?, ?)", ("key", payload))


def test_key_is_stable_and_normalizes_account_identity():
    assert key() == key(account=" Student@Example.Invalid ")
    assert len(key()) == 64
    assert key(references=[{"estimated_minutes": 30, "title": "Saved task", "id": 1}]) == key()


@pytest.mark.parametrize("changes", [
    {"account": "other@example.invalid"}, {"model": "another-model"},
    {"references": []}, {"references": [{"id": 1, "title": "Changed task", "estimated_minutes": 30}]},
    {"references": [{"id": 1, "title": "Saved task", "estimated_minutes": 45}]},
    {"suggest_effort": False},
])
def test_key_invalidates_account_model_references_and_effort_mode(changes):
    assert key(**changes) != key()


@pytest.mark.parametrize("changes", [
    {"id": "local-b"}, {"gmail_id": "gmail-b"}, {"body": "Prepare a different assignment."},
    {"subject": "Updated arrangements"}, {"sent_at": "2026-10-25T10:00:00+08:00"},
    {"attachments": [{"id": "a", "filename": "exam.pdf", "content": b"synthetic bytes"}]},
])
def test_key_invalidates_source_identity_content_and_time(changes):
    assert key(mails=[mail(**changes)]) != key()


@pytest.mark.parametrize("changes", [
    {"id": "b"}, {"filename": "new.pdf"}, {"content": b"different bytes"},
    {"warnings": ["Only some pages were readable"]},
])
def test_key_binds_attachment_bytes_filename_identity_and_warnings(changes):
    attachment = {"id": "a", "filename": "exam.pdf", "content": b"original", "warnings": []}
    assert key(mails=[mail(attachments=[attachment | changes])]) != (
        key(mails=[mail(attachments=[attachment])]))


@pytest.mark.parametrize("field", [
    "UNIFIED_VERSION", "PIPELINE_VERSION", "RECONCILIATION_VERSION", "FIXED_EVENTS_VERSION",
    "EFFORT_VERSION", "SYSTEM_PROMPT", "FIXED_EVENTS_PROMPT",
])
def test_key_invalidates_changed_versions_and_prompts(monkeypatch, field):
    before = key()
    monkeypatch.setattr(inbox_cache, field, "changed contract")
    assert key() != before


def test_key_binds_effort_prompt_only_when_estimates_are_requested(monkeypatch):
    enabled_before, disabled_before = key(), key(suggest_effort=False)
    monkeypatch.setattr(effort_estimation, "_INSTRUCTIONS", "Changed effort instructions")
    assert key() != enabled_before
    assert key(suggest_effort=False) == disabled_before


def test_source_credentials_are_not_part_of_the_interpretation_key():
    assert key(mails=[mail(api_key="not-a-real-key", access_token="not-a-real-token")]) == key()


def test_complete_empty_analysis_survives_restart_and_does_not_alias(tmp_path):
    path = tmp_path / "cache.sqlite3"
    cache = InboxAnalysisCache(path)
    original = completed()
    before = deepcopy(original)
    cache.put("key", original)
    assert original == before
    assert InboxAnalysisCache(path).get("key") == original
    loaded = cache.get("key")
    loaded["event_result"]["events"].append({"title": "Changed in the UI"})
    assert cache.get("key") == original
    assert cache.get("absent") is None


def test_successful_message_with_no_tasks_or_events_is_cached(tmp_path):
    cache = InboxAnalysisCache(tmp_path / "cache.sqlite3")
    response = completed()
    response["task_result"]["results"] = [{"mail_id": "local-a", "result": {"tasks": [], "warnings": []}}]
    response["event_result"]["results"] = [{"mail_id": "local-a", "events": [], "warnings": [], "cached": False}]
    cache.put("empty-message", response)
    assert cache.get("empty-message") == response


@pytest.mark.parametrize("path,value", [
    (("status",), "partial"), (("status",), "failed"),
    (("task_result", "status"), "partial"), (("task_result", "status"), None),
    (("event_result", "complete"), False), (("event_result", "complete"), 1),
    (("event_result", "status"), "partial"),
    (("task_result", "results"), [{"error": "Failed to open"}]),
    (("task_result", "results"), [{"partial": True, "result": {"tasks": []}}]),
    (("event_result", "results"), [{"error": "Extraction failed"}]),
])
def test_partial_results_are_never_cached_as_complete(tmp_path, path, value):
    cache = InboxAnalysisCache(tmp_path / "cache.sqlite3")
    response = completed()
    target = response
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        cache.put("key", response)
    assert cache.get("key") is None
    insert_raw(cache, json.dumps(response))
    assert cache.get("key") is None


@pytest.mark.parametrize("payload", [
    "broken json", "null", "[]", '"not a result"', "{}", '{"status": "completed"}',
])
def test_malformed_persisted_payloads_are_cache_misses(tmp_path, payload):
    cache = InboxAnalysisCache(tmp_path / "cache.sqlite3")
    insert_raw(cache, payload)
    assert cache.get("key") is None


@pytest.mark.parametrize("path,value", [
    (("task_result",), None), (("task_result", "results"), "wrong"),
    (("task_result", "results"), [None]), (("task_result", "results"), [{"result": {}}]),
    (("task_result", "effort"), []), (("event_result",), None),
    (("event_result", "events"), "wrong"), (("event_result", "results"), "wrong"),
    (("events",), None), (("usage",), None), (("warnings",), None),
])
def test_corrupted_nested_shape_is_ignored(tmp_path, path, value):
    cache = InboxAnalysisCache(tmp_path / "cache.sqlite3")
    response = completed()
    target = response
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = value
    insert_raw(cache, json.dumps(response))
    assert cache.get("key") is None


def test_known_credential_fields_are_removed_without_changing_source_text(tmp_path):
    cache = InboxAnalysisCache(tmp_path / "cache.sqlite3")
    response = completed(api_key="top-secret", callback=lambda: None)
    response["task_result"]["api_key"] = "nested-secret"
    response["task_result"]["results"] = [{"mail_id": "local-a", "result": {"tasks": [{
        "title": "Explain token authentication", "source_quote": "The token field matters; api_key is a field name.",
        "source_token": "academic example", "access_token": "nested-token-secret",
    }], "warnings": []}}]
    response["event_result"]["credentials"] = {"refresh_token": "refresh-secret"}
    response["event_result"]["Authorization"] = "Bearer authorization-secret"
    before = deepcopy(response)
    cache.put("key", response)
    assert response == before
    loaded = cache.get("key")
    serialized = json.dumps(loaded)
    assert "secret" not in serialized
    assert "callback" not in loaded and "api_key" not in loaded
    evidence = loaded["task_result"]["results"][0]["result"]["tasks"][0]
    assert evidence["source_quote"] == "The token field matters; api_key is a field name."
    assert evidence["source_token"] == "academic example"
    assert loaded["usage"]["input_tokens"] == 20 and loaded["usage"]["output_tokens"] == 10
    insert_raw(cache, json.dumps({key: value for key, value in before.items() if key != "callback"}))
    assert cache.get("key") == loaded


def test_invalid_replacement_keeps_previous_success(tmp_path):
    cache = InboxAnalysisCache(tmp_path / "cache.sqlite3")
    cache.put("key", completed())
    with pytest.raises(ValueError):
        cache.put("key", completed(status="partial"))
    assert cache.get("key") == completed()


def test_cache_closes_database_handles_after_create_write_and_read(tmp_path, monkeypatch):
    connect = sqlite3.connect
    connections = []

    def tracked_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    cache = InboxAnalysisCache(tmp_path / "cache.sqlite3")
    cache.put("key", completed())
    assert cache.get("key") == completed()
    assert cache.get("missing") is None
    assert len(connections) == 4
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
