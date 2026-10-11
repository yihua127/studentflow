"""Offline regressions for source-bound proposal deduplication."""

import hashlib
import io
from copy import deepcopy
from itertools import permutations

import pytest
from docx import Document

from studentflow.task_dedup import (
    SOURCE_FIELDS,
    body_source_hash,
    legacy_source_ids,
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


def canonical_task(message=None, *, job="job-one", item="item-one", **changes):
    return task(message, _source_key=hashlib.sha256(job.encode()).hexdigest(),
                _source_item_id=hashlib.sha256(item.encode()).hexdigest(), **changes)


def test_cached_identity_survives_changed_display_details():
    original = canonical_task()
    edited = canonical_task(title="Tutor-reviewed outline", due_at="2030-01-01T17:00:00+08:00",
                            estimated_minutes=180)
    assert same_task(original, edited)
    assert len(merge_duplicate_tasks([original, edited])) == 1


@pytest.mark.parametrize("job", ["job-one", "job-two"])
def test_distinct_canonical_items_never_fall_back_to_same_title_and_quote(job):
    first = canonical_task()
    second = canonical_task(job=job, item="item-two")
    assert not same_task(first, second)
    assert len(merge_duplicate_tasks([first, second])) == 2


def test_validated_aliases_link_new_and_old_generation_ids():
    first = canonical_task(title="First extraction wording")
    second = canonical_task(job="new-job", item="new-item", title="New wording for same action")
    linked = deepcopy(second)
    linked["sources"] = merge_sources(first["sources"], second["sources"])
    assert same_task(first, linked) and same_task(second, linked)
    for order in permutations([first, linked, second]):
        merged = merge_duplicate_tasks(list(order))
        assert len(merged) == 1
        assert merged[0]["source_duplicate_count"] == 3


def test_same_canonical_id_rejects_other_account_or_different_actual_file():
    file = attachment()
    original = canonical_task(mail(attachments=[file]), source_attachment_id="a1")
    other_account = canonical_task(mail(gmail_account="other@example.invalid", attachments=[file]),
                                   source_attachment_id="a1")
    other_bytes = canonical_task(mail(attachments=[attachment(QUOTE + " Changed.")]), source_attachment_id="a1")
    assert not same_task(original, other_account)
    assert not same_task(original, other_bytes)
    assert not same_task(canonical_task(), canonical_task(mail("other-body")))


def test_same_canonical_id_keeps_course_scope_and_unknown_account_separate():
    file = attachment()
    original = canonical_task(mail("m1", subject="MATH101 assignment", attachments=[file]),
                              source_attachment_id="a1")
    other_course = canonical_task(mail("m2", subject="HIST201 assignment", attachments=[file]),
                                  source_attachment_id="a1")
    unknown_account = canonical_task(mail("m1", subject="MATH101 assignment", gmail_account=None,
                                         attachments=[file]), source_attachment_id="a1")
    assert not same_task(original, other_course)
    assert not same_task(original, unknown_account)
    assert len(merge_duplicate_tasks([original, other_course, unknown_account])) == 3
    legacy = canonical_task(mail(gmail_account=None))
    assert same_task(legacy, deepcopy(legacy))


def test_source_ids_are_local_only_and_bound_to_actual_source_hash():
    message = mail()
    correct = canonical_task(message, _source_hash=body_source_hash(message))
    assert correct["sources"][0]["source_item_id"]
    incorrect = canonical_task(message, _source_hash=hashlib.sha256(b"different body").hexdigest())
    assert incorrect["sources"][0]["source_item_id"] is None
    raw_model_fields = task(message, source_key="a" * 64, source_item_id="b" * 64)
    assert raw_model_fields["sources"][0]["source_item_id"] is None
    file = attachment()
    correct_file = canonical_task(mail(attachments=[file]), source_attachment_id="a1",
                                  _source_hash=hashlib.sha256(file["content"]).hexdigest())
    assert correct_file["sources"][0]["source_item_id"]
    stale_file = canonical_task(mail(attachments=[file]), source_attachment_id="a1", _source_hash="a" * 64)
    assert stale_file["sources"][0]["source_item_id"] is None


def test_legacy_ids_are_deterministic_and_source_upgrade_does_not_duplicate_evidence():
    old = {"id": "old-task-id"}
    sources = task()["sources"]
    original = deepcopy(sources)
    upgraded = legacy_source_ids(old, sources)
    assert sources == original
    assert upgraded[0]["source_item_id"] == hashlib.sha256(b"saved-task|old-task-id").hexdigest()
    assert upgraded[0]["source_key"] == hashlib.sha256(b"saved-source|m1").hexdigest()
    assert merge_sources(sources, upgraded) == upgraded
    assert merge_sources(upgraded, sources) == upgraded


def test_forwarded_copies_of_one_cached_item_group_even_when_quote_validation_failed():
    file = attachment()
    digest = hashlib.sha256(file["content"]).hexdigest()
    first = canonical_task(mail("m1", attachments=[file]), source_attachment_id="a1",
                           source_quote="", _source_hash=digest)
    second = canonical_task(mail("m2", subject="Fw: Syllabus", attachments=[dict(file, id="forward")]),
                            source_attachment_id="forward", source_quote="", _source_hash=digest)
    assert first["sources"][0]["source_hash"] == digest
    assert same_task(first, second)
    [merged] = merge_duplicate_tasks([first, second])
    assert merged["source_duplicate_count"] == 2
    assert merged["source_quote"] == ""
    assert all(source["source_quote"] == "" for source in merged["sources"])
    assert {source["mail_id"] for source in merged["sources"]} == {"m1", "m2"}


def test_same_cached_body_item_with_missing_quote_groups_only_with_matching_local_hash():
    message = mail()
    first = canonical_task(message, source_quote="", _source_hash=body_source_hash(message))
    second = canonical_task(message, source_quote="", _source_hash=body_source_hash(message), title="Another label")
    assert same_task(first, second)
    changed_message = mail(body=QUOTE + " A different body.")
    changed = canonical_task(changed_message, source_quote="", _source_hash=body_source_hash(changed_message))
    assert not same_task(first, changed)


@pytest.mark.parametrize("proof", [None, "not-a-hash", "a" * 64])
def test_missing_quote_ids_without_verified_binding_never_group(proof):
    message = mail(attachments=[attachment()])
    first = canonical_task(message, source_attachment_id="a1", source_quote="", _source_hash=proof)
    assert first["sources"][0]["source_hash"] is None
    assert not same_task(first, deepcopy(first))
    assert len(merge_duplicate_tasks([first, deepcopy(first)])) == 2


@pytest.mark.parametrize("different", ["item", "account", "unknown_account", "file_bytes", "course"])
def test_missing_quote_canonical_grouping_preserves_identity_and_source_guards(different):
    file = attachment()
    first_mail = mail("m1", subject="MATH101 Assignment", attachments=[file])
    second_mail = mail("m2", subject="MATH101 Assignment", attachments=[file])
    item = "item-one"
    if different == "item":
        item = "other-item"
    elif different == "account":
        second_mail["gmail_account"] = "other@example.invalid"
    elif different == "unknown_account":
        second_mail["gmail_account"] = None
    elif different == "file_bytes":
        second_mail["attachments"] = [attachment(QUOTE + " Changed contents.")]
    elif different == "course":
        second_mail["subject"] = "HIST201 Assignment"
    first = canonical_task(first_mail, source_attachment_id="a1", source_quote="",
                           _source_hash=hashlib.sha256(file["content"]).hexdigest())
    second = canonical_task(second_mail, item=item, source_attachment_id="a1", source_quote="",
                            _source_hash=hashlib.sha256(second_mail["attachments"][0]["content"]).hexdigest())
    assert not same_task(first, second)
    assert len(merge_duplicate_tasks([first, second])) == 2


def test_raw_model_source_hash_does_not_supply_a_local_binding():
    message = mail()
    task_with_raw_fields = canonical_task(message, source_quote="", source_hash=body_source_hash(message))
    assert task_with_raw_fields["sources"][0]["source_hash"] is None
    assert not same_task(task_with_raw_fields, deepcopy(task_with_raw_fields))


def test_adding_bound_hash_to_existing_evidence_does_not_duplicate_it():
    unbound = canonical_task()["sources"][0]
    bound = canonical_task(_source_hash=body_source_hash(mail()))["sources"][0]
    assert merge_sources([unbound], [bound]) == [bound]
    assert merge_sources([bound], [unbound]) == [bound]
