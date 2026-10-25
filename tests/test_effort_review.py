"""Human estimate confirmation stays distinct from source facts and model advice."""

from copy import deepcopy
from datetime import datetime

import pytest

from studentflow.effort_review import build_effort_review
from studentflow.storage import Store


def advice(**changes):
    return {"suggested_minutes": 90, "estimate_reason": "Reading and a first draft.",
            "assumptions": ["A short report with familiar material."], "model": "test-model",
            **changes}


def task(**changes):
    source = {"mail_id": "mail-a", "gmail_account": "learner@example.invalid",
              "source_quote": "Prepare the report.", "source_item_id": "a" * 64,
              "source_key": "b" * 64}
    return {"mail_id": "mail-a", "title": "Prepare report", "sources": [source],
            "due_at": "2030-01-04T17:00:00+08:00", "estimated_minutes": 90,
            "status": "confirmed", **changes}


def reviewed_task(**changes):
    review = build_effort_review(advice(estimated_minutes=120), 90, model="test-model")
    return task(effort_review=review, **changes)


def test_build_separates_source_advice_and_human_decision_without_mutation():
    proposal = advice(estimated_minutes=120, confirmed_minutes=999, decision="accepted",
                      reviewed_at="untrusted timestamp", unrelated="discard")
    before = deepcopy(proposal)
    review = build_effort_review(proposal, 75, model="chosen-model")
    assert review["stated_minutes"] == 120
    assert review["suggested_minutes"] == 90
    assert review["confirmed_minutes"] == 75
    assert review["decision"] == "adjusted"
    assert review["model"] == "chosen-model"
    assert "unrelated" not in review
    assert datetime.fromisoformat(review["reviewed_at"]).utcoffset() is not None
    review["assumptions"].append("Local change")
    assert proposal == before


@pytest.mark.parametrize("final, expected", [(90, "accepted"), (120, "adjusted"),
                                            (None, "not_used"), (0, "not_used")])
def test_decision_is_derived_from_explicit_human_value(final, expected):
    review = build_effort_review(advice(), final)
    assert review["decision"] == expected
    assert review["confirmed_minutes"] == (final or None)


def test_unknown_ai_estimate_does_not_turn_manual_input_into_an_ai_suggestion():
    review = build_effort_review({"estimated_minutes": 120}, 75)
    assert review["stated_minutes"] == 120
    assert review["suggested_minutes"] is None
    assert review["confirmed_minutes"] == 75
    assert review["decision"] == "not_used"


@pytest.mark.parametrize("value", [True, 1.5, "60", -1, 10001])
def test_invalid_confirmed_values_are_rejected(value):
    with pytest.raises(ValueError):
        build_effort_review(advice(), value)


@pytest.mark.parametrize("changes", [{"estimate_reason": " "}, {"suggested_minutes": True},
                                      {"assumptions": "Unknown workload"},
                                      {"assumptions": [""]}])
def test_model_advice_has_valid_minutes_reason_and_assumptions(changes):
    with pytest.raises(ValueError):
        build_effort_review(advice(**changes), 90)


def test_first_save_persists_review_and_rejects_mismatched_official_estimate(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    value = reviewed_task()
    identifier = store.save_task(value)
    saved = Store(store.path).tasks()[0]
    assert saved["id"] == identifier
    assert saved["effort_review"] == value["effort_review"]
    with pytest.raises(ValueError, match="must match"):
        store.save_task(reviewed_task(title="Another task", estimated_minutes=120))
    assert store.tasks() == [saved]


def test_invalid_review_rolls_back_the_entire_save_batch(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    invalid = reviewed_task(title="Second task")
    invalid["effort_review"]["decision"] = "adjusted"
    with pytest.raises(ValueError, match="decision"):
        store.save_tasks([reviewed_task(), invalid])
    assert store.tasks() == []


def test_repeat_extraction_cannot_replace_manually_reviewed_time_or_progress(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    original = reviewed_task()
    identifier = store.save_task(original)
    before = store.update_task(identifier, {"estimated_minutes": 150, "completed_minutes": 30})
    repeated = task(estimated_minutes=120,
                    effort_review=build_effort_review(advice(suggested_minutes=120), 120))
    assert store.save_task(repeated) == identifier
    after = store.tasks()[0]
    for key in ("estimated_minutes", "completed_minutes", "remaining_minutes", "effort_review"):
        assert after[key] == before[key]
    assert after["effort_review"]["stated_minutes"] == 120
    assert len(store.tasks()) == 1


def test_repeat_legacy_identity_keeps_review_while_legacy_without_review_stays_compatible(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    first = reviewed_task(sources=[])
    identifier = store.save_task(first)
    before = store.update_task(identifier, {"estimated_minutes": 150})
    assert store.save_task(task(sources=[], estimated_minutes=75)) == identifier
    assert store.tasks()[0]["estimated_minutes"] == 150
    assert store.tasks()[0]["effort_review"] == before["effort_review"]
    legacy = task(sources=[], title="Legacy task", estimated_minutes=60)
    legacy_id = store.save_task(legacy)
    assert store.save_task(legacy | {"estimated_minutes": 120}) == legacy_id
    assert store.tasks()[1]["estimated_minutes"] == 120
    assert "effort_review" not in store.tasks()[1]


def test_tasks_edit_updates_confirmation_but_keeps_the_original_advice(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    identifier = store.save_task(reviewed_task())
    before = store.tasks()[0]
    after = store.update_task(identifier, {"estimated_minutes": 135},
                              expected_updated_at=before["updated_at"])
    review = after["effort_review"]
    assert review["confirmed_minutes"] == 135 and review["decision"] == "adjusted"
    for key in ("stated_minutes", "suggested_minutes", "estimate_reason", "assumptions", "model"):
        assert review[key] == before["effort_review"][key]
    accepted = store.update_task(identifier, {"estimated_minutes": 90})
    assert accepted["effort_review"]["decision"] == "accepted"
    cleared = store.update_task(identifier, {"estimated_minutes": 0})
    assert cleared["effort_review"]["confirmed_minutes"] is None
    assert cleared["effort_review"]["decision"] == "not_used"
    assert cleared["status"] == "pending"


def test_progress_and_title_edit_do_not_rewrite_the_effort_review(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    identifier = store.save_task(reviewed_task())
    before = store.tasks()[0]
    after = store.update_task(identifier, {"completed_minutes": 30, "title": "Updated report"})
    assert after["effort_review"] == before["effort_review"]


def test_review_existing_task_is_atomic_and_preserves_evidence_and_progress(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    identifier = store.save_task(task(completed_minutes=30))
    before = store.tasks()[0]
    suggestion = advice()
    original_advice = deepcopy(suggestion)
    after = store.review_task_effort(identifier, 120, suggestion,
                                     expected_updated_at=before["updated_at"])
    assert after["estimated_minutes"] == 120
    assert after["remaining_minutes"] == 90 and after["completed_minutes"] == 30
    assert after["status"] == "confirmed"
    assert after["sources"] == before["sources"]
    assert after["_extraction_identity"] == before["_extraction_identity"]
    assert after["due_at"] == before["due_at"]
    assert after["effort_review"]["decision"] == "adjusted"
    assert after["effort_review"]["model"] == "test-model"
    assert suggestion == original_advice
    assert Store(store.path).tasks()[0] == after
    with pytest.raises(ValueError, match="changed"):
        store.review_task_effort(identifier, 180, suggestion,
                                 expected_updated_at=before["updated_at"])
    assert store.tasks()[0] == after


@pytest.mark.parametrize("final", [None, 0, 15])
def test_review_cannot_reduce_total_below_completed_work(tmp_path, final):
    store = Store(tmp_path / "state.sqlite3")
    identifier = store.save_task(task(completed_minutes=30))
    before = store.tasks()[0]
    with pytest.raises(ValueError, match="already completed"):
        store.review_task_effort(identifier, final, advice(),
                                 expected_updated_at=before["updated_at"])
    assert store.tasks() == [before]


def test_review_retains_source_stated_value_and_requires_current_version(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    identifier = store.save_task(reviewed_task(due_at=None, status="pending"))
    before = store.tasks()[0]
    with pytest.raises(ValueError, match="Refresh"):
        store.review_task_effort(identifier, 90, advice(), expected_updated_at=None)
    after = store.review_task_effort(identifier, 90, advice(estimated_minutes=999),
                                     expected_updated_at=before["updated_at"])
    assert after["effort_review"]["stated_minutes"] == 120
    assert after["status"] == "pending"
    with pytest.raises(ValueError, match="no longer available"):
        store.review_task_effort("missing", 90, advice(), expected_updated_at=after["updated_at"])
