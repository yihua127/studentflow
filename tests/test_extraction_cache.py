"""Persistent source caching preserves item identity and safely scopes results."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from studentflow.extraction_cache import ExtractionCache, attach_item_ids, source_cache_key
from studentflow.storage import Store


def result():
    return {"tasks": [{
        "title": "Submit outline", "due_at": "2030-01-04T17:00:00+08:00",
        "estimated_minutes": None, "source_quote": "Submit the outline.",
        "source_attachment_id": None, "source_filename": None, "source_page": None,
        "confidence": 0.9, "needs_clarification": ["Enter a study estimate."],
        "status": "needs_review",
    }], "warnings": ["Review the proposed task."]}


def cache_key(**changes):
    values = {"kind": "body", "account": "student@example.invalid", "content_hash": "abc123",
              "model": "example-model", "context": {"sent_at": "2030-01-01T08:00:00+08:00"}}
    return source_cache_key(**(values | changes))


def test_key_is_stable_for_account_case_and_context_key_order():
    left = cache_key(account=" Student@Example.Invalid ", context={"a": 1, "b": 2})
    right = cache_key(context={"b": 2, "a": 1})
    assert left == right
    assert len(left) == 64


@pytest.mark.parametrize("changes", [
    {"kind": "attachment"}, {"account": "other@example.invalid"}, {"content_hash": "different"},
    {"model": "another-model"}, {"context": {"sent_at": "2030-01-02T08:00:00+08:00"}},
    {"schema_version": "v-next"},
])
def test_key_separates_every_interpretation_input(changes):
    assert cache_key(**changes) != cache_key()


def test_cache_survives_restart_and_returned_values_do_not_mutate_it(tmp_path):
    path = tmp_path / "state.sqlite3"
    cache = ExtractionCache(path)
    key = cache_key()
    original = result()
    before = deepcopy(original)
    cache.put(key, original)
    assert original == before
    cached = cache.get(key)
    assert cached["tasks"][0]["_source_key"] == key
    assert len(cached["tasks"][0]["_source_item_id"]) == 64
    assert ExtractionCache(path).get(key) == cached
    cached["tasks"][0]["title"] = "Changed in a view"
    assert cache.get(key)["tasks"][0]["title"] == "Submit outline"
    assert cache.get("missing") is None


def test_reanalysis_replaces_generation_and_attach_helper_can_preserve_ids(tmp_path):
    cache = ExtractionCache(tmp_path / "state.sqlite3")
    key = cache_key()
    cache.put(key, result())
    first_id = cache.get(key)["tasks"][0]["_source_item_id"]
    cache.put(key, result())
    assert cache.get(key)["tasks"][0]["_source_item_id"] != first_id
    attached = attach_item_ids(key, result(), generation="controlled-generation")
    before = deepcopy(attached)
    cache.put(key, attached)
    assert cache.get(key) == attached == before
    assert attach_item_ids(key, result(), generation="controlled-generation") == attached


def test_identical_task_titles_have_distinct_item_ids_in_same_generation():
    original = result()
    original["tasks"].append(deepcopy(original["tasks"][0]))
    before = deepcopy(original)
    attached = attach_item_ids(cache_key(), original)
    assert original == before
    assert attached["tasks"][0]["_source_item_id"] != attached["tasks"][1]["_source_item_id"]


def test_empty_result_is_cached(tmp_path):
    cache = ExtractionCache(tmp_path / "state.sqlite3")
    cache.put("empty", {"tasks": [], "warnings": []})
    assert ExtractionCache(cache.path).get("empty") == {"tasks": [], "warnings": []}


@pytest.mark.parametrize("payload", [
    "{broken", "null", "[]", '{"tasks": "wrong"}',
    '{"tasks": ["wrong"], "warnings": []}',
    '{"tasks": [{"title": "Missing IDs"}], "warnings": []}',
])
def test_invalid_cache_entries_are_misses(tmp_path, payload):
    cache = ExtractionCache(tmp_path / "state.sqlite3")
    with cache.connect() as db:
        db.execute("INSERT INTO source_extraction_cache VALUES (?, ?)", ("bad", payload))
    assert cache.get("bad") is None


def test_mismatched_and_duplicate_item_ids_are_misses(tmp_path):
    cache = ExtractionCache(tmp_path / "state.sqlite3")
    attached = attach_item_ids("another-key", result())
    with cache.connect() as db:
        db.execute("INSERT INTO source_extraction_cache VALUES (?, ?)", ("key", json.dumps(attached)))
    assert cache.get("key") is None
    attached = attach_item_ids("key", result())
    attached["tasks"].append(deepcopy(attached["tasks"][0]))
    with cache.connect() as db:
        db.execute("UPDATE source_extraction_cache SET payload=? WHERE cache_key='key'", (json.dumps(attached),))
    assert cache.get("key") is None


def test_cache_retains_only_extraction_fields(tmp_path):
    cache = ExtractionCache(tmp_path / "state.sqlite3")
    original = result()
    original.update(api_key="not-a-real-key", raw_mail="entire unrelated message")
    original["tasks"][0].update(api_key="not-a-real-key", body="unrelated body", content=b"raw bytes")
    cache.put("safe-key", original)
    cached = cache.get("safe-key")
    serialized = json.dumps(cached)
    assert "not-a-real-key" not in serialized and "unrelated" not in serialized and "raw bytes" not in serialized
    assert cached["tasks"][0]["source_quote"] == "Submit the outline."


def test_cache_coexists_with_task_database_without_changing_tasks(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    store.save_task({"title": "Existing task", "mail_id": "synthetic", "status": "pending"})
    before = store.tasks()
    cache = ExtractionCache(path)
    cache.put(cache_key(), result())
    assert Store(path).tasks() == before
    assert cache.get(cache_key()) is not None


def test_atomic_failed_upsert_retains_previous_result(tmp_path):
    cache = ExtractionCache(tmp_path / "state.sqlite3")
    cache.put("one-key", result())
    before = cache.get("one-key")
    with cache.connect() as db:
        db.execute("CREATE TRIGGER reject_cache BEFORE UPDATE ON source_extraction_cache "
                   "BEGIN SELECT RAISE(ABORT, 'simulated cache failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        cache.put("one-key", result())
    assert cache.get("one-key") == before


def test_threaded_writes_use_independent_connections(tmp_path):
    cache = ExtractionCache(tmp_path / "state.sqlite3")
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda number: cache.put(str(number), result()), range(12)))
    assert all(cache.get(str(number)) is not None for number in range(12))


@pytest.mark.parametrize("basis", ["absolute", "message_relative", "unknown"])
def test_deadline_basis_and_source_hash_survive_cache(tmp_path, basis):
    cache = ExtractionCache(tmp_path / "state.sqlite3")
    original = result()
    original["tasks"][0].update(deadline_basis=basis, _source_hash="a" * 64)
    cache.put(cache_key(), original)
    task = cache.get(cache_key())["tasks"][0]
    assert task["deadline_basis"] == basis and task["_source_hash"] == "a" * 64


def test_invalid_deadline_basis_and_nonhash_source_hash_are_not_retained(tmp_path):
    cache = ExtractionCache(tmp_path / "state.sqlite3")
    original = result()
    original["tasks"][0].update(deadline_basis=["not an enum"], _source_hash="not a hash")
    cache.put(cache_key(), original)
    task = cache.get(cache_key())["tasks"][0]
    assert "deadline_basis" not in task and "_source_hash" not in task
