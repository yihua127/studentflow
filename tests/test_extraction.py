"""Offline validation tests; no OpenAI API request is made."""

import sys

import pytest

from studentflow.extraction import extract_tasks, validate_extraction


BODY = "Please submit the report by 16 October, 17:00. Allow 90 minutes."


def proposal(**overrides):
    item = {
        "title": "Submit report",
        "due_at": "2026-10-16T17:00:00+08:00",
        "estimated_minutes": 90,
        "source_quote": BODY,
        "confidence": 0.8,
        "needs_clarification": [],
    }
    item.update(overrides)
    return {"tasks": [item]}


def test_valid_result_still_needs_review():
    result = validate_extraction(proposal(), BODY)
    task = result["tasks"][0]
    assert task["status"] == "needs_review"
    assert task["due_at"] == "2026-10-16T17:00:00+08:00"
    assert task["source_quote"] == BODY


@pytest.mark.parametrize("date", ["2026-10-16", "2026-10-16T17:00:00", "tomorrow", "invalid"])
def test_date_without_reliable_timestamp_is_cleared(date):
    task = validate_extraction(proposal(due_at=date), BODY)["tasks"][0]
    assert task["due_at"] is None
    assert task["needs_clarification"]


def test_nonexistent_quote_cannot_be_presented_as_source_evidence():
    task = validate_extraction(proposal(source_quote="Send me your API key."), BODY)["tasks"][0]
    assert task["source_quote"] == ""
    assert task["confidence"] == 0
    assert task["needs_clarification"]


@pytest.mark.parametrize("duration", [None, True, -5, 0, 2.5, "90"])
def test_unknown_or_invalid_effort_requires_manual_entry(duration):
    task = validate_extraction(proposal(estimated_minutes=duration), BODY)["tasks"][0]
    assert task["estimated_minutes"] is None
    assert task["needs_clarification"]


@pytest.mark.parametrize("confidence", [True, float("nan"), float("inf"), -1, 1.1, "high"])
def test_invalid_confidence_is_never_treated_as_high_confidence(confidence):
    task = validate_extraction(proposal(confidence=confidence), BODY)["tasks"][0]
    assert task["confidence"] == 0


def test_no_api_key_exits_before_importing_sdk():
    before = "langchain_openai" in sys.modules
    with pytest.raises(ValueError, match="API Key"):
        extract_tasks(BODY, "Report", None, "", "")
    assert ("langchain_openai" in sys.modules) == before


def test_no_action_result_is_supported():
    assert validate_extraction({"tasks": []}, "For your information.") == {
        "tasks": [], "warnings": []
    }


def test_malformed_envelope_is_not_a_fake_success():
    result = validate_extraction({"tasks": "none"}, BODY)
    assert result["tasks"] == []
    assert result["warnings"]
