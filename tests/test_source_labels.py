"""Different Chat messages must remain recognizable and independently selectable."""

from copy import deepcopy

import pytest

from studentflow.source_labels import (
    chronological_sources,
    source_labels,
    source_preview_heading,
)


def chat(identifier, text="Test message", **fields):
    return {
        "source_id": identifier, "provider": "google_chat", "title": "StudentFlow Test",
        "text": text, "sender_name": "Alex", "sent_at": "2026-10-25T01:02:03Z", **fields,
    }


def test_four_same_room_messages_show_different_body_previews_and_stable_ids():
    records = [chat(str(index), f"Discussion part {index}") for index in range(1, 5)]
    before = deepcopy(records)
    labels = source_labels(records)
    assert len(set(labels.values())) == 4
    assert list(labels) == ["1", "2", "3", "4"]
    for index in range(1, 5):
        assert f"Discussion part {index}" in labels[str(index)]
        assert "2026-10-25 09:02:03 SGT · Alex" in labels[str(index)]
    assert records == before


def test_identical_messages_time_sender_and_truncated_prefix_still_have_unique_labels():
    records = [chat("one", "same " * 60), chat("two", "same " * 60)]
    labels = source_labels(records)
    assert labels["one"].startswith("1. ")
    assert labels["two"].startswith("2. ")
    assert labels["one"] != labels["two"]
    assert labels["one"].endswith("…")
    assert len(labels["one"].rsplit(" · ", 1)[-1]) <= 140
    assert "one" not in labels["one"] and "two" not in labels["two"]


def test_chronological_ordinals_handle_offsets_not_lexicographic_timestamp_order():
    records = [
        chat("later", sent_at="2026-10-25T07:00:00Z"),
        chat("earlier", sent_at="2026-10-25T09:00:00+08:00"),
        chat("unknown", sent_at=None),
    ]
    ordered = chronological_sources(records)
    assert [item["source_id"] for item in ordered] == ["earlier", "later", "unknown"]
    labels = source_labels(records)
    assert labels["earlier"].startswith("1. 2026-10-25 09:00:00 SGT")
    assert labels["later"].startswith("2. 2026-10-25 15:00:00 SGT")
    assert labels["unknown"].startswith("3. Time not provided")
    assert records[0]["source_id"] == "later"


@pytest.mark.parametrize("stamp", [None, "", "invalid", "2026-10-25", "2026-10-25T09:00:00"])
def test_missing_or_naive_time_and_missing_sender_never_gain_invented_metadata(stamp):
    record = chat("test", sent_at=stamp, sender_name="", sender_id="")
    label = source_labels([record])["test"]
    assert "Time not provided" in label
    assert "Sender not provided" in label
    assert "SGT" not in label


def test_sender_id_fallback_does_not_infer_sender_from_names_inside_message_body():
    record = chat("test", "Maya will prepare slides", sender_name=None, sender_id="users/123")
    label = source_labels([record])["test"]
    assert " · users/123 · Maya will prepare slides" in label
    assert " · Maya · " not in label


def test_body_and_sender_whitespace_collapsed_without_changing_source_evidence():
    record = chat("test", "Line one\n\nLine\ttwo", sender_name="  Alex\nSmith ")
    label = source_labels([record])["test"]
    assert label.endswith(" · Alex Smith · Line one Line two")
    assert record["text"] == "Line one\n\nLine\ttwo"


def test_editing_body_changes_label_but_preserves_identity_mapping():
    record = chat("stable-message-id", "Original")
    before = source_labels([record])
    after = source_labels([{**record, "text": "Corrected"}])
    assert before.keys() == after.keys()
    assert before["stable-message-id"] != after["stable-message-id"]


def test_unique_email_and_attachment_labels_keep_existing_format():
    records = [
        {"source_id": "mail", "provider": "gmail", "title": "Meeting notes"},
        {"source_id": "pdf", "provider": "attachment", "title": "syllabus.pdf"},
    ]
    assert source_labels(records) == {
        "mail": "Gmail · Meeting notes", "pdf": "Attachment · syllabus.pdf",
    }


def test_duplicate_attachment_and_mail_titles_include_real_metadata_and_remain_distinct():
    records = [
        {"source_id": "pdf1", "provider": "attachment", "title": "syllabus.pdf",
         "mail_id": "message-a", "sent_at": "2026-10-25T01:02:03Z"},
        {"source_id": "pdf2", "provider": "attachment", "title": "syllabus.pdf",
         "mail_id": "message-a", "sent_at": "2026-10-25T01:02:03Z"},
        {"source_id": "mail1", "provider": "gmail", "title": "Meeting notes",
         "sender": "Alex <alex@example.invalid>"},
        {"source_id": "mail2", "provider": "gmail", "title": "Meeting notes",
         "sender": "Alex <alex@example.invalid>"},
    ]
    labels = source_labels(records)
    assert len(set(labels.values())) == 4
    assert "2026-10-25 09:02:03 SGT" in labels["pdf1"]
    assert "message-a" not in labels["pdf1"]
    assert "Alex <alex@example.invalid>" in labels["mail1"]
    assert "Item 2" in labels["pdf2"]


def test_existing_title_matching_generated_suffix_does_not_cause_collision():
    records = [
        {"source_id": "a", "provider": "attachment", "title": "notes"},
        {"source_id": "b", "provider": "attachment", "title": "notes"},
        {"source_id": "c", "provider": "attachment", "title": "notes · Item 1"},
    ]
    assert len(set(source_labels(records).values())) == 3


def test_preview_heading_contains_chat_room_sender_and_local_timestamp():
    result = source_preview_heading(chat("one"))
    assert result == "Google Chat · StudentFlow Test · 2026-10-25 09:02:03 SGT · Alex"
    assert "Test message" not in result


def test_email_rfc_timestamp_is_read_without_guessing_missing_timezone():
    record = chat("one", sent_at="Sun, 25 Oct 2026 02:34:56 +0000")
    assert "2026-10-25 10:34:56 SGT" in source_labels([record])["one"]


@pytest.mark.parametrize("records", [
    [{"source_id": ""}], [{"title": "No ID"}], [chat("same"), chat("same")],
])
def test_invalid_identity_is_rejected_instead_of_overwriting_a_message(records):
    with pytest.raises(ValueError, match="distinct, non-empty"):
        source_labels(records)


def test_empty_input_has_no_labels_or_sorted_records():
    assert source_labels([]) == {}
    assert chronological_sources([]) == []
