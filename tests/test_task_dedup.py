"""Offline regressions for source-bound proposal deduplication."""

import hashlib
import io
from copy import deepcopy
from itertools import permutations

import pytest
from docx import Document

from studentflow.task_dedup import (
    SOURCE_FIELDS,
    merge_duplicate_tasks,
    merge_sources,
    same_task,
    source_evidence,
)

QUOTE = "Submit the research outline by 12 October 2026 at 17:00."
DUE = "2026-10-12T17:00:00+08:00"


def attachment(text=QUOTE, identifier="a1", filename="syllabus.docx"):
    document = Document()
    document.add_paragraph(text)
    output = io.BytesIO()
    document.save(output)
    return {"id": identifier, "filename": filename, "content": output.getvalue()}


def mail(identifier="m1", **changes):
    return {"id": identifier, "gmail_account": "student@example.invalid", "body": QUOTE,
            "subject": "Syllabus", "sent_at": "2026-10-09T12:00:00+08:00",
            "attachments": [], **changes}


def task(message=None, **changes):
    message = mail() if message is None else message
    result = {"title": "Submit research outline", "due_at": DUE,
              "estimated_minutes": None, "source_quote": QUOTE,
              "source_attachment_id": None, "needs_clarification": [], **changes}
    result["sources"] = [source_evidence(result, message)]
    return result


def test_duplicate_body_and_attachment_preserve_both_evidence_sources():
    message = mail(attachments=[attachment()])
    body, file = task(message), task(message, source_attachment_id="a1")
    result = merge_duplicate_tasks([body, file])
    assert len(result) == 1
    assert result[0]["source_duplicate_count"] == 2
    assert {source["source_attachment_id"] for source in result[0]["sources"]} == {None, "a1"}


def test_copied_attachment_in_different_messages_is_one_task_despite_different_quotes():
    content = attachment(text=QUOTE + "\nUpload the outline, essay and slides.")
    first = task(mail("m1", attachments=[content]), source_attachment_id="a1")
    forwarded = task(mail("m2", subject="Fw: Syllabus", attachments=[dict(content, id="forward")]),
                     source_attachment_id="forward", source_quote="Upload the outline, essay and slides.")
    result = merge_duplicate_tasks([first, forwarded])
    assert len(result) == 1
    assert [source["mail_id"] for source in result[0]["sources"]] == ["m1", "m2"]
    assert result[0]["source_quote"] == QUOTE


def test_filename_does_not_connect_different_file_contents():
    first = task(mail("m1", attachments=[attachment()]), source_attachment_id="a1")
    other = task(mail("m2", attachments=[attachment(QUOTE + " Different course.")]),
                 source_attachment_id="a1")
    assert not same_task(first, other)


def test_two_different_attachments_in_one_message_are_kept_separate():
    message = mail(attachments=[attachment(), attachment(QUOTE + " Other assignment.", "a2")])
    assert not same_task(task(message, source_attachment_id="a1"),
                         task(message, source_attachment_id="a2"))


@pytest.mark.parametrize("changes", [
    {"title": "Submit research essay"},
    {"due_at": "2026-10-13T17:00:00+08:00"},
    {"due_at": None},
    {"due_at": "2026-10-12T17:00:00"},
    {"due_at": "invalid"},
    {"source_quote": "Fabricated quotation"},
])
def test_different_or_unverifiable_tasks_are_not_merged(changes):
    assert not same_task(task(), task(**changes))


def test_timezone_equivalence_and_conservative_title_normalization():
    assert same_task(task(), task(title="  SUBMIT  research outline。 ",
                                  due_at="2026-10-12T09:00:00Z"))
    assert not same_task(task(title="Submit report 1"), task(title="Submit report 2"))


def test_missing_deadlines_require_identical_normalized_quotes():
    message = mail(body=QUOTE + "\nPlease upload the outline.")
    assert same_task(task(message, due_at=None), task(message, due_at=None))
    assert not same_task(task(message, due_at=None),
                         task(message, due_at=None, source_quote="Please upload the outline."))


def test_unknown_effort_does_not_bridge_conflicting_known_estimates():
    result = merge_duplicate_tasks([task(), task(estimated_minutes=60), task(estimated_minutes=90)])
    assert len(result) == 2
    assert result[0]["source_duplicate_count"] == 2
    assert result[0]["estimated_minutes"] is None
    assert result[1]["estimated_minutes"] == 90


def test_conflicting_course_codes_preserve_separate_generic_titles():
    message = mail(body="DSS5105: " + QUOTE + "\nDSS5101: " + QUOTE)
    first = task(message, source_quote="DSS5105: " + QUOTE)
    second = task(message, source_quote="DSS5101: " + QUOTE)
    assert not same_task(first, second)
    assert len(merge_duplicate_tasks([task(message), first, second])) == 2


def test_explicit_course_metadata_prevents_merging():
    assert not same_task(task(course="English"), task(course="History"))


def test_subject_course_codes_preserve_identical_assignment_templates():
    file = attachment()
    first = task(mail("m1", subject="DSS5105 Assignment", attachments=[file]),
                 source_attachment_id="a1")
    other = task(mail("m2", subject="DSS5101 Assignment", attachments=[file]),
                 source_attachment_id="a1")
    assert not same_task(first, other)
    assert len(merge_duplicate_tasks([first, other])) == 2


@pytest.mark.parametrize("field", ["subject", "quote"])
def test_generic_course_context_cannot_bridge_conflicting_courses(field):
    file = attachment("MATH101: " + QUOTE + "\nHIST201: " + QUOTE)
    tasks = []
    for index, course in enumerate(("MATH101", "", "HIST201")):
        message = mail(f"m{index}", attachments=[file],
                       subject=f"{course} Assignment" if field == "subject" else "Assignment")
        quote = f"{course}: {QUOTE}" if course and field == "quote" else QUOTE
        tasks.append(task(message, source_attachment_id="a1", source_quote=quote))
    for order in permutations(tasks):
        result = merge_duplicate_tasks(list(order))
        assert len(result) == 2
        assert sorted(item["source_duplicate_count"] for item in result) == [1, 2]


def test_account_scope_blocks_same_message_id_and_shared_attachment_across_accounts():
    file = attachment()
    first = task(mail(attachments=[file]), source_attachment_id="a1")
    other = task(mail(gmail_account="other@example.invalid", attachments=[file]),
                 source_attachment_id="a1")
    assert not same_task(first, other)
    assert not same_task(task(), task(mail(gmail_account=None)))
    assert same_task(task(mail(gmail_account=None)), task(mail(gmail_account=None)))


def test_invalid_source_quote_cannot_supply_a_matching_fingerprint():
    message = mail(attachments=[attachment()])
    valid = task(message, source_attachment_id="a1")
    invalid = task(message, source_attachment_id="a1", source_quote="Not in the document.")
    assert invalid["sources"][0]["source_quote"] == ""
    assert not same_task(valid, invalid)


def test_hash_comes_only_from_actual_nonempty_attachment_bytes():
    file = attachment()
    source = source_evidence(task(source_attachment_id="a1"), mail(attachments=[file]))
    assert source["attachment_sha256"] == hashlib.sha256(file["content"]).hexdigest()
    empty = dict(file, content=b"")
    assert source_evidence(task(source_attachment_id="a1"), mail(attachments=[empty]))["attachment_sha256"] is None
    assert source_evidence(task(), mail(attachments=[file]))["attachment_sha256"] is None


def test_source_builder_and_merger_do_not_mutate_cached_inputs():
    message = mail(attachments=[attachment()])
    original_message = deepcopy(message)
    first = task(message, needs_clarification=["Unknown effort."])
    second = task(message, source_attachment_id="a1", needs_clarification=["Review date."])
    originals = deepcopy([first, second])
    result = merge_duplicate_tasks([first, second])
    assert message == original_message
    assert [first, second] == originals
    assert result[0]["needs_clarification"] == ["Unknown effort.", "Review date."]
    result[0]["sources"][0]["mail_subject"] = "Changed"
    assert first["sources"][0]["mail_subject"] == "Syllabus"


def test_merge_sources_whitelists_and_preserves_distinct_quotes():
    source = {**source_evidence(task(), mail()), "body": "Private body", "content": b"Private bytes"}
    merged = merge_sources([source], [source, dict(source, source_quote="Other evidence")])
    assert len(merged) == 2
    assert all(set(value) == set(SOURCE_FIELDS) for value in merged)
    assert merged[0] is not source


def test_legacy_task_provenance_is_supported_without_fabricated_source_list():
    first = {key: value for key, value in task().items() if key != "sources"}
    first.update(mail_id="legacy", gmail_account=None)
    assert same_task(first, dict(first))


def test_stable_first_candidate_and_order_with_multiple_tasks():
    first = task(_row_id="first", estimated_minutes=None)
    different = task(title="Attend quiz", _row_id="second")
    duplicate = task(_row_id="third", estimated_minutes=60)
    result = merge_duplicate_tasks([first, different, duplicate])
    assert [item["_row_id"] for item in result] == ["first", "second"]
    assert result[0]["estimated_minutes"] is None
