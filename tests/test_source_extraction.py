"""Unique-source pipeline tests use local documents and injected extractors."""

import io
from copy import deepcopy

import pytest
from docx import Document

from studentflow import extraction
from studentflow.attachments import extract_attachment
from studentflow.extraction_cache import ExtractionCache
from studentflow.source_extraction import analyze_sources

ACCOUNT = "student@example.invalid"
STAMP = "2026-10-09T09:00:00+08:00"
TEXT = "Submit the research outline by 16 October at 17:00."


def document(text=TEXT):
    doc = Document()
    doc.add_paragraph(text)
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def mail(identifier, *, content=None, body="", sent_at=STAMP, filename="syllabus.docx"):
    return {
        "id": identifier, "subject": "Course message " + identifier,
        "body": body, "sent_at": sent_at, "gmail_account": ACCOUNT,
        "attachments": [] if content is None else [{
            "id": "attachment-" + identifier, "filename": filename, "content": content,
        }],
    }


def task(quote, *, attachment_id=None, basis="absolute", due="2026-10-16T17:00:00+08:00", title="Submit outline"):
    return {
        "title": title, "due_at": due, "estimated_minutes": 60,
        "source_quote": quote, "source_attachment_id": attachment_id,
        "deadline_basis": basis, "confidence": 0.8, "needs_clarification": [],
    }


def fake_extractor(calls):
    def extract(body, subject, sent_at, api_key, model, **kwargs):
        calls.append({"body": body, "subject": subject, "sent_at": sent_at, "model": model, **deepcopy(kwargs)})
        if "attachments" in kwargs:
            attachment = kwargs["attachments"][0]
            quote = extract_attachment(attachment)["text"]
            item = task(quote, attachment_id=attachment["id"], title="Document task " + str(len(calls)))
        else:
            item = task(body, title="Body task")
        return {"tasks": [item], "warnings": []}
    return extract


def run(mails, extractor, *, cache=None, account=ACCOUNT, model="model-test", **kwargs):
    return analyze_sources(mails, account, "synthetic-key", model, extractor=extractor, cache=cache, **kwargs)


def test_identical_attachment_is_one_job_before_parallel_work_and_maps_each_source():
    content = document()
    mails = [mail("a", content=content, body="Body A"), mail("b", content=content, body="Body B", filename="renamed.docx")]
    before = deepcopy(mails)
    calls = []
    results = run(mails, fake_extractor(calls))
    attachment_calls = [call for call in calls if "attachments" in call]
    assert len(calls) == 3 and len(attachment_calls) == 1
    [call] = attachment_calls
    assert call["subject"] == "Attached document" and call["body"] == "" and call["sent_at"] is None
    assert call["attachments"][0]["id"].startswith("document-")
    a = next(item for item in results["a"]["result"]["tasks"] if item["source_attachment_id"])
    b = next(item for item in results["b"]["result"]["tasks"] if item["source_attachment_id"])
    assert a["title"] == b["title"]
    assert a["_source_item_id"] == b["_source_item_id"]
    assert a["_source_key"] == b["_source_key"]
    assert a["_source_hash"] == b["_source_hash"]
    assert a["source_attachment_id"] == "attachment-a" and b["source_attachment_id"] == "attachment-b"
    assert a["source_filename"] == "syllabus.docx" and b["source_filename"] == "renamed.docx"
    assert mails == before


def test_persistent_restart_reuses_result_and_stable_item_identity(tmp_path):
    content = document()
    calls = []
    extractor = fake_extractor(calls)
    cache = ExtractionCache(tmp_path / "cache.sqlite3")
    first = run([mail("a", content=content)], extractor, cache=cache)
    second = run([mail("b", content=content, filename="different.docx")], extractor,
                 cache=ExtractionCache(cache.path))
    assert len(calls) == 1
    assert second["b"]["cached"] is True
    assert first["a"]["result"]["tasks"][0]["_source_item_id"] == second["b"]["result"]["tasks"][0]["_source_item_id"]


@pytest.mark.parametrize("changed", ["bytes", "account", "model", "prompt", "force"])
def test_changed_extraction_inputs_or_force_start_fresh_analysis(tmp_path, monkeypatch, changed):
    calls = []
    extractor = fake_extractor(calls)
    cache = ExtractionCache(tmp_path / "cache.sqlite3")
    message = mail("a", content=document())
    first = run([message], extractor, cache=cache)
    options = {}
    if changed == "bytes":
        message["attachments"][0]["content"] = document(TEXT + " Revised.")
    elif changed == "account":
        options["account"] = "other@example.invalid"
    elif changed == "model":
        options["model"] = "different-model"
    elif changed == "prompt":
        monkeypatch.setattr(extraction, "SYSTEM_PROMPT", extraction.SYSTEM_PROMPT + "\nNew extraction rule.")
    elif changed == "force":
        options["reanalyze"] = True
    second = run([message], extractor, cache=cache, **options)
    assert len(calls) == 2
    assert second["a"]["cached"] is False
    assert first["a"]["result"]["tasks"][0]["_source_item_id"] != second["a"]["result"]["tasks"][0]["_source_item_id"]


def test_only_relative_document_tasks_are_bound_per_distinct_message_date(tmp_path):
    content = document(TEXT + "\nSubmit the reflection tomorrow at 17:00.")
    calls = []

    def extractor(body, subject, sent_at, api_key, model, attachments=None):
        calls.append(sent_at)
        identifier = attachments[0]["id"]
        absolute = task(TEXT, attachment_id=identifier)
        relative = task("Submit the reflection tomorrow at 17:00.", attachment_id=identifier,
                        basis="message_relative", due=None if sent_at is None else sent_at,
                        title="Submit reflection")
        return {"tasks": [absolute, relative], "warnings": []}

    messages = [mail("a", content=content), mail("b", content=content, sent_at="2026-10-10T09:00:00+08:00"),
                mail("c", content=content, sent_at="2026-10-09T01:00:00Z")]
    cache = ExtractionCache(tmp_path / "cache.sqlite3")
    results = run(messages, extractor, cache=cache)
    assert len(calls) == 3 and calls.count(None) == 1
    by_mail = {key: {item["title"]: item for item in value["result"]["tasks"]} for key, value in results.items()}
    assert by_mail["a"]["Submit outline"]["_source_item_id"] == by_mail["b"]["Submit outline"]["_source_item_id"]
    assert by_mail["a"]["Submit reflection"]["_source_item_id"] == by_mail["c"]["Submit reflection"]["_source_item_id"]
    assert by_mail["a"]["Submit reflection"]["_source_item_id"] != by_mail["b"]["Submit reflection"]["_source_item_id"]
    rerun = run(messages, extractor, cache=ExtractionCache(cache.path))
    assert len(calls) == 3 and all(entry["cached"] for entry in rerun.values())


def test_missing_date_cannot_reuse_another_message_relative_deadline():
    content = document("Submit tomorrow at 17:00.")
    calls = []

    def extractor(body, subject, sent_at, api_key, model, attachments=None):
        calls.append(sent_at)
        return {"tasks": [task("Submit tomorrow at 17:00.", attachment_id=attachments[0]["id"],
                                basis="message_relative", due=sent_at)], "warnings": []}

    results = run([mail("a", content=content), mail("b", content=content, sent_at=None)], extractor)
    assert len(calls) == 2
    assert results["a"]["result"]["tasks"][0]["due_at"] is not None
    assert results["b"]["result"]["tasks"][0]["due_at"] is None


def test_body_deadline_update_is_preserved_beside_context_free_document_task():
    calls = []
    result = run([mail("a", content=document(), body="The outline deadline is now 18 October at 17:00.")], fake_extractor(calls))
    items = result["a"]["result"]["tasks"]
    assert len(items) == 2
    assert {bool(item["source_attachment_id"]) for item in items} == {False, True}
    assert any("now 18 October" in item["source_quote"] for item in items)


def test_partial_failure_retries_only_failed_source_and_hides_provider_details(tmp_path):
    calls = []
    good = fake_extractor(calls)
    failures = [True]

    def extractor(body, subject, sent_at, api_key, model, **kwargs):
        if kwargs and failures[0]:
            calls.append({"failed": True})
            raise RuntimeError("private provider request details")
        return good(body, subject, sent_at, api_key, model, **kwargs)

    messages = [mail("a", content=document(), body="Submit body assignment.")]
    cache = ExtractionCache(tmp_path / "cache.sqlite3")
    first = run(messages, extractor, cache=cache)
    assert first["a"]["partial"] is True
    assert len(first["a"]["result"]["tasks"]) == 1
    assert "private provider" not in str(first)
    failures[0] = False
    second = run(messages, extractor, cache=ExtractionCache(cache.path))
    assert len(calls) == 3
    assert second["a"]["partial"] is False
    assert len(second["a"]["result"]["tasks"]) == 2


def test_relative_binding_failure_keeps_absolute_and_unresolved_tasks(tmp_path):
    content = document(TEXT + "\nSubmit reflection tomorrow.")
    calls = []
    failed = [True]

    def extractor(body, subject, sent_at, api_key, model, attachments=None):
        calls.append(sent_at)
        if sent_at and failed[0]:
            raise RuntimeError("sensitive request")
        return {"tasks": [task(TEXT, attachment_id=attachments[0]["id"]),
                          task("Submit reflection tomorrow.", attachment_id=attachments[0]["id"],
                               title="Submit reflection", basis="message_relative", due=sent_at)], "warnings": []}

    cache = ExtractionCache(tmp_path / "cache.sqlite3")
    first = run([mail("a", content=content)], extractor, cache=cache)["a"]
    assert first["partial"] and len(first["result"]["tasks"]) == 2
    assert first["result"]["tasks"][1]["due_at"] is None
    failed[0] = False
    second = run([mail("a", content=content)], extractor, cache=cache)["a"]
    assert len(calls) == 3 and not second["partial"]


def test_empty_body_and_unreadable_images_never_trigger_model_or_fake_ocr():
    calls = []
    results = run([mail("empty"), mail("image", content=b"\x89PNG\r\n\x1a\n", filename="scan.png")], fake_extractor(calls))
    assert calls == []
    assert results["empty"]["result"]["tasks"] == []
    assert results["image"]["result"]["tasks"] == []
    assert any("readable" in warning.lower() or "recognition" in warning.lower() for warning in results["image"]["result"]["warnings"])


def test_session_dictionary_cache_reuses_body_without_attachment_keyword():
    calls = []

    def body_only(body, subject, sent_at, api_key, model):
        calls.append(body)
        return {"tasks": [task(body)], "warnings": []}

    cache = {}
    messages = [mail("a", body="Submit the report.")]
    first = run(messages, body_only, cache=cache)
    second = run(messages, body_only, cache=cache)
    assert calls == ["Submit the report."]
    assert second["a"]["cached"]
    assert first["a"]["result"]["tasks"] == second["a"]["result"]["tasks"]


@pytest.mark.parametrize("bad_binding", ["missing", "different_quote", "duplicate_quote"])
def test_incomplete_relative_binding_keeps_every_task_and_retries_without_reanalyzing_base(tmp_path, bad_binding):
    relative_quotes = ["Submit reflection tomorrow at 17:00.", "Upload slides tomorrow at 19:00."]
    content = document(TEXT + "\n" + "\n".join(relative_quotes))
    calls, bad = [], [True]

    def extractor(body, subject, sent_at, api_key, model, attachments=None):
        calls.append(sent_at)
        identifier = attachments[0]["id"]
        tasks = [task(TEXT, attachment_id=identifier)] + [
            task(quote, attachment_id=identifier, basis="message_relative", due=sent_at,
                 title="Relative task " + str(index)) for index, quote in enumerate(relative_quotes)
        ]
        if sent_at and bad[0]:
            if bad_binding == "missing":
                tasks.pop()
            elif bad_binding == "different_quote":
                tasks[-1]["source_quote"] = "A different instruction."
            else:
                tasks[-1]["source_quote"] = tasks[-2]["source_quote"]
        return {"tasks": tasks, "warnings": []}

    cache = ExtractionCache(tmp_path / "cache.sqlite3")
    first = run([mail("a", content=content)], extractor, cache=cache)["a"]
    assert first["partial"]
    assert len(first["result"]["tasks"]) == 3
    assert all(item["due_at"] is None for item in first["result"]["tasks"] if item["deadline_basis"] == "message_relative")
    assert any("Relative attachment deadlines" in warning for warning in first["result"]["warnings"])
    bad[0] = False
    second = run([mail("a", content=content)], extractor, cache=ExtractionCache(cache.path))["a"]
    assert len(calls) == 3 and calls.count(None) == 1
    assert len(second["result"]["tasks"]) == 3 and not second["partial"]


def test_context_free_warning_omits_missing_mail_date_and_restores_each_filename():
    content = document()
    missing_date = "The sent time is missing or has no valid timezone. Relative dates need manual review."

    def extractor(body, subject, sent_at, api_key, model, attachments=None):
        return {"tasks": [task(TEXT, attachment_id=attachments[0]["id"])],
                "warnings": [missing_date, attachments[0]["filename"] + ": Some text was truncated."]}

    result = run([mail("a", content=content), mail("b", content=content, filename="copy.docx")], extractor)
    assert missing_date not in result["a"]["result"]["warnings"]
    assert "syllabus.docx: Some text was truncated." in result["a"]["result"]["warnings"]
    assert "copy.docx: Some text was truncated." in result["b"]["result"]["warnings"]
