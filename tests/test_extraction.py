"""Offline validation tests; no OpenAI API request is made."""

import io
import json
import sys
from types import SimpleNamespace

import pytest
from docx import Document

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


@pytest.mark.parametrize("basis", [None, [], {}, True, "unrecognized"])
def test_invalid_deadline_basis_is_unknown_without_losing_the_proposal(basis):
    task = validate_extraction(proposal(deadline_basis=basis), BODY)["tasks"][0]
    assert task["deadline_basis"] == "unknown"
    assert task["source_quote"] == BODY


@pytest.mark.parametrize("basis", ["absolute", "message_relative", "unknown"])
def test_valid_deadline_basis_is_preserved_for_source_context(basis):
    task = validate_extraction(proposal(deadline_basis=basis), BODY)["tasks"][0]
    assert task["deadline_basis"] == basis


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


def docx_attachment(text=BODY):
    document = Document()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return {"id": "assignment-doc", "filename": "Assignment.docx", "content": buffer.getvalue()}


def mock_model(monkeypatch, payload):
    calls = []

    class Chat:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def with_structured_output(self, schema, **kwargs):
            calls.append(schema.model_json_schema())
            return self

        def invoke(self, messages):
            calls.append(json.loads(messages[1][1]))
            return payload

    monkeypatch.setitem(sys.modules, "langchain_openai", SimpleNamespace(ChatOpenAI=Chat))
    return calls


@pytest.mark.parametrize("body", ["", "For your information. See attached."])
def test_attachment_only_task_is_sent_to_model_and_keeps_filename(monkeypatch, body):
    calls = mock_model(monkeypatch, proposal(source_attachment_id="assignment-doc"))
    result = extract_tasks(body, "Assignment", "2026-10-09T09:00:00+08:00", "fake-key", "mock", [docx_attachment()])
    assert calls[-1]["body"] == body
    assert calls[-1]["attachments"][0]["text"] == BODY
    assert result["tasks"][0]["source_quote"] == BODY
    assert result["tasks"][0]["source_attachment_id"] == "assignment-doc"
    assert result["tasks"][0]["source_filename"] == "Assignment.docx"
    assert result["tasks"][0]["status"] == "needs_review"


def test_quote_must_belong_to_named_attachment_and_not_body_or_other_file():
    documents = [{"id": "a", "filename": "a.pdf", "text": "Different text", "pages": []},
                 {"id": "b", "filename": "b.pdf", "text": BODY, "pages": []}]
    task = validate_extraction(proposal(source_attachment_id="a"), BODY, documents)["tasks"][0]
    assert task["source_quote"] == ""
    assert task["confidence"] == 0
    assert task["source_filename"] == "a.pdf"


def test_unknown_attachment_id_cannot_be_used_as_evidence():
    task = validate_extraction(proposal(source_attachment_id="invented"), BODY)["tasks"][0]
    assert task["source_quote"] == ""
    assert task["needs_clarification"]


def test_attachment_page_is_derived_from_verified_quote():
    docs = [{"id": "a", "filename": "a.pdf", "text": BODY,
             "pages": [{"page": 3, "text": BODY}]}]
    task = validate_extraction(proposal(source_attachment_id="a", source_page=999), "", docs)["tasks"][0]
    assert task["source_page"] == 3


def test_image_attachment_is_not_pretended_to_have_ocr(monkeypatch):
    calls = mock_model(monkeypatch, {"tasks": []})
    attachment = {"id": "image", "filename": "scan.png", "content": b"\x89PNG\r\n\x1a\n"}
    result = extract_tasks("See attached", "Document", None, "fake", "mock", [attachment])
    assert calls[-1]["attachments"] == []
    assert any("Text recognition is not available" in warning for warning in result["warnings"])
    with pytest.raises(ValueError, match="No readable"):
        extract_tasks("", "Document", None, "fake", "mock", [attachment])


def test_combined_source_limit_is_explicit_and_preserves_exact_evidence(monkeypatch):
    from studentflow import extraction
    monkeypatch.setattr(extraction, "MAX_SOURCE_CHARS", 40)
    calls = mock_model(monkeypatch, {"tasks": []})
    result = extract_tasks("Body", "Document", None, "fake", "mock", [docx_attachment("A" * 60)])
    assert len(calls[-1]["attachments"][0]["text"]) == 36
    assert any("combined text limit" in warning for warning in result["warnings"])


def test_wrapped_pdf_quote_restores_original_whitespace_and_page():
    original = "Research  outline\t20%\n12\u00a0October 2026, 17:00"
    source = "Course assessments\n" + original + "\nOther information"
    docs = [{"id": "pdf", "filename": "syllabus.pdf", "text": source,
             "pages": [{"page": 2, "text": source}]}]
    payload = proposal(source_quote="Research outline 20% 12 October 2026, 17:00", source_attachment_id="pdf")
    item = validate_extraction(payload, "", docs)["tasks"][0]
    assert item["source_quote"] == original
    assert item["source_quote"] in source
    assert item["source_page"] == 2
    assert item["confidence"] == 0.8


def test_body_quote_restores_newlines_without_changing_words():
    body = "Please\nsubmit the report by 16 October,\t17:00. Allow 90 minutes."
    item = validate_extraction(proposal(source_quote=BODY), body)["tasks"][0]
    assert item["source_quote"] == body
    assert item["source_attachment_id"] is None


@pytest.mark.parametrize("quote", [
    "Please submit the essay by 16 October, 17:00. Allow 90 minutes.",
    "Please submit the report by 17 October, 17:00. Allow 90 minutes.",
    "Please submit the report by 16 October; 17:00. Allow 90 minutes.",
    "please submit the report by 16 October, 17:00. Allow 90 minutes.",
])
def test_whitespace_tolerance_never_accepts_different_words_dates_punctuation_or_case(quote):
    item = validate_extraction(proposal(source_quote=quote), BODY)["tasks"][0]
    assert item["source_quote"] == ""
    assert item["confidence"] == 0


@pytest.mark.parametrize("source_id", [None, "incorrect-id", "syllabus.pdf"])
def test_single_document_only_analysis_binds_its_real_source_locally(source_id):
    docs = [{"id": "real-document-id", "filename": "syllabus.pdf", "text": BODY, "pages": []}]
    item = validate_extraction(proposal(source_attachment_id=source_id), "", docs)["tasks"][0]
    assert item["source_attachment_id"] == "real-document-id"
    assert item["source_filename"] == "syllabus.pdf"
    assert item["source_quote"] == BODY


@pytest.mark.parametrize("body,documents,source_id", [
    ("See attached.", [{"id": "a", "filename": "a.pdf", "text": BODY, "pages": []}], None),
    ("See attached.", [{"id": "a", "filename": "a.pdf", "text": BODY, "pages": []}], "wrong"),
    ("", [{"id": "a", "filename": "a.pdf", "text": BODY, "pages": []},
          {"id": "b", "filename": "b.pdf", "text": "Different instructions", "pages": []}], None),
    ("", [{"id": "a", "filename": "a.pdf", "text": BODY, "pages": []},
          {"id": "b", "filename": "b.pdf", "text": "Different instructions", "pages": []}], "b"),
])
def test_single_document_binding_does_not_weaken_mixed_source_validation(body, documents, source_id):
    item = validate_extraction(proposal(source_attachment_id=source_id), body, documents)["tasks"][0]
    assert item["source_quote"] == ""
    assert item["confidence"] == 0


def test_single_source_binding_still_rejects_fabricated_quote():
    docs = [{"id": "a", "filename": "a.pdf", "text": BODY, "pages": []}]
    item = validate_extraction(proposal(source_attachment_id=None, source_quote="Invented instruction."), "", docs)["tasks"][0]
    assert item["source_quote"] == ""
    assert item["confidence"] == 0
