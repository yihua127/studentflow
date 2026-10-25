"""Source extraction -> suggested effort -> human review -> stored schedule inputs."""

from copy import deepcopy

import pytest

from studentflow.batch_ui import review_rows, validate_selected_rows
from studentflow.effort_batch import apply_effort_suggestions, estimate_review_efforts
from studentflow.planner import propose_schedule
from studentflow.storage import Store


def extracted(stated=None):
    mail = {"id": "mail-one", "gmail_account": "student@example.invalid", "subject": "Report",
            "body": "Write a report explaining the experiment.", "attachments": []}
    task = {"title": "Write report", "estimated_minutes": stated,
            "due_at": "2035-10-16T17:00:00+08:00", "source_quote": mail["body"]}
    results = [{"mail_id": mail["id"], "gmail_id": "gmail-one", "cache_key": "test-cache",
                "result": {"tasks": [task], "warnings": []}}]
    return review_rows(results, {mail["id"]: mail})


def estimate(tasks, api_key, model):
    return {"estimates": {task["id"]: {
        "suggested_minutes": 60, "estimate_reason": "Draft and check a short report.",
        "assumptions": ["The experiment is already complete."],
    } for task in tasks}, "warnings": []}


def test_confirmed_value_not_ai_suggestion_drives_schedule_and_preserves_progress(tmp_path):
    rows, sources = extracted()
    result = estimate_review_efforts(rows, sources, "offline", "test-model", estimator=estimate)
    prepared, bound = apply_effort_suggestions(rows, sources, result["estimates"])
    assert rows[0]["Study time (min)"] == 0  # Cached extraction facts remain unchanged.
    assert prepared[0]["Study time (min)"] == 60
    prepared[0].update({"Include": True, "Study time (min)": 180})
    store = Store(tmp_path / "tasks.sqlite3")
    with pytest.raises(ValueError, match="reviewed"):
        validate_selected_rows(prepared, bound, False)
    assert store.tasks() == []
    records = validate_selected_rows(prepared, bound, True)
    identifier = store.save_tasks(records)[0]
    saved = store.tasks()[0]
    assert saved["estimated_minutes"] == 180 and saved["status"] == "confirmed"
    assert saved["effort_review"]["suggested_minutes"] == 60
    assert saved["effort_review"]["confirmed_minutes"] == 180
    assert saved["effort_review"]["decision"] == "adjusted"
    assert saved["effort_review"]["model"] == "test-model"
    store.update_task(identifier, {"completed_minutes": 30}, expected_updated_at=saved["updated_at"])
    slots = [{"start": "2035-10-15T09:00:00+08:00", "end": "2035-10-15T17:00:00+08:00"}]
    plan = propose_schedule(store.tasks(), slots, [], {"meals": []})
    assert sum(block["minutes"] for block in plan["blocks"]) == 150
    store.save_tasks(records)  # Repeated extraction cannot reset either progress or reviewed effort.
    assert len(store.tasks()) == 1
    assert store.tasks()[0]["completed_minutes"] == 30
    assert store.tasks()[0]["estimated_minutes"] == 180


def test_source_stated_time_is_not_replaced_by_ai_and_forged_table_values_are_ignored():
    rows, sources = extracted(90)

    def forbidden(*args):
        raise AssertionError("Source-stated effort needs no model call.")

    assert estimate_review_efforts(rows, sources, "offline", "model", estimator=forbidden)["estimates"] == {}
    prepared, bound = apply_effort_suggestions(rows, sources, {
        rows[0]["_row_id"]: {"suggested_minutes": 10, "estimate_reason": "Untrusted."},
    })
    assert prepared[0]["AI suggestion (min)"] is None
    assert prepared[0]["Study time (min)"] == 90
    prepared[0].update({"Include": True, "AI suggestion (min)": 999,
                        "Source-stated time (min)": 999, "Estimate basis": "Forged"})
    record = validate_selected_rows(prepared, bound, True)[0]
    assert record["effort_review"]["suggested_minutes"] is None
    assert record["effort_review"]["stated_minutes"] == 90
    assert record["effort_review"]["confirmed_minutes"] == 90


def test_declining_suggestion_does_not_confirm_task():
    rows, sources = extracted()
    suggestions = estimate_review_efforts(rows, sources, "offline", "model", estimator=estimate)
    prepared, bound = apply_effort_suggestions(rows, sources, suggestions["estimates"])
    prepared[0].update({"Include": True, "Study time (min)": 0})
    record = validate_selected_rows(prepared, bound, True)[0]
    assert record["estimated_minutes"] is None and record["status"] == "pending"
    assert record["effort_review"]["decision"] == "not_used"


def test_cache_binds_model_account_and_source_content():
    rows, sources = extracted()
    calls, cache = [], {}

    def counted(tasks, *args):
        calls.append(deepcopy(tasks))
        return estimate(tasks, *args)

    first = estimate_review_efforts(rows, sources, "offline", "model", estimator=counted, cache=cache)
    assert estimate_review_efforts(rows, sources, "offline", "model", estimator=counted, cache=cache) == first
    assert len(calls) == 1
    estimate_review_efforts(rows, sources, "offline", "another-model", estimator=counted, cache=cache)
    sid = rows[0]["_row_id"]
    sources[sid]["gmail_account"] = "other@example.invalid"
    estimate_review_efforts(rows, sources, "offline", "model", estimator=counted, cache=cache)
    sources[sid]["body"] += " Now include a second experiment."
    estimate_review_efforts(rows, sources, "offline", "model", estimator=counted, cache=cache)
    assert len(calls) == 4
    assert all(len(call[0]["source_text"]) <= 4000 for call in calls)


def test_provider_failure_keeps_tasks_and_never_caches_exception():
    rows, sources = extracted()
    cache = {}

    def fail(*args):
        raise RuntimeError("secret-key PRIVATE-MESSAGE")

    result = estimate_review_efforts(rows, sources, "offline", "model", estimator=fail, cache=cache)
    assert result["estimates"] == {} and result["warnings"]
    assert "secret-key" not in str(result) and "PRIVATE-MESSAGE" not in str(result)
    assert cache == {}
    prepared, _ = apply_effort_suggestions(rows, sources, result["estimates"])
    assert prepared[0]["Task"] == "Write report" and prepared[0]["Study time (min)"] == 0


def test_unreliable_partial_batch_is_not_used_or_cached():
    rows, sources = extracted()
    second = deepcopy(rows[0])
    second["_row_id"] = "second"
    rows.append(second)
    sources["second"] = deepcopy(sources[rows[0]["_row_id"]])

    def bad(tasks, *args):
        result = estimate(tasks, *args)
        result["estimates"]["second"]["suggested_minutes"] = True
        return result

    cache = {}
    result = estimate_review_efforts(rows, sources, "offline", "model", estimator=bad, cache=cache)
    assert result["estimates"] == {} and cache == {} and result["warnings"]


def test_many_tasks_are_bounded_and_each_is_estimated_once():
    sample_rows, sample_sources = extracted()
    source = next(iter(sample_sources.values()))
    rows = [dict(sample_rows[0], _row_id=str(index)) for index in range(61)]
    sources = {row["_row_id"]: source for row in rows}
    batches = []

    def counted(tasks, *args):
        batches.append(len(tasks))
        return estimate(tasks, *args)

    result = estimate_review_efforts(rows, sources, "offline", "model", estimator=counted)
    assert batches == [30, 30] and len(result["estimates"]) == 60
    assert "60 tasks" in result["warnings"][0]
