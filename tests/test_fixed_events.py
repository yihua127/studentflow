from unittest.mock import Mock

import pytest

from studentflow.extraction import SYSTEM_PROMPT as TASK_PROMPT
from studentflow.fixed_events import (
    SYSTEM_PROMPT,
    as_busy_events,
    extract_fixed_events,
    merge_fixed_event_proposals,
    prepare_sources,
    validate_confirmed_event,
    validate_fixed_events,
)
from studentflow.fixed_events_ui import event_review_rows, parse_event_time, reviewed_fixed_events
from studentflow.storage import Store

ACCOUNT = "test@example.com"
QUOTE = "DSS5105 final exam is on 28 October 2026, 14:00-16:00 Singapore time, lasting 120 minutes."


def source(**changes):
    return {"id": "body", "text": QUOTE, "account": ACCOUNT, "mail_id": "mail-a",
            "source_attachment_id": None, "source_filename": None, **changes}


def payload(**changes):
    return {"events": [{"title": "Attend DSS5105 final exam", "event_key": "DSS5105 final exam",
                        "kind": "exam", "course": "DSS5105", "start_at": "2026-10-28T14:00:00+08:00",
                        "end_at": "2026-10-28T16:00:00+08:00", "duration_minutes": 120,
                        "time_basis": "absolute", "start_evidence": "28 October 2026, 14:00",
                        "end_evidence": "16:00", "duration_evidence": "120 minutes",
                        "in_person": True, "location": "LT1", "needs_clarification": [],
                        "sources": [{"source_id": "body", "source_quote": QUOTE}], **changes}]}


def proposal(**changes):
    event = validate_fixed_events(payload(), [source()])["events"][0]
    event.update(changes)
    return event


def test_known_fixed_interval_and_source_duration():
    event = proposal()
    assert event["start_at"] == "2026-10-28T14:00:00+08:00"
    assert event["end_at"] == "2026-10-28T16:00:00+08:00"
    assert event["status"] == "needs_review"
    assert event["sources"][0]["source_quote"] == QUOTE


def test_derive_end_only_from_stated_duration():
    event = validate_fixed_events(payload(end_at=None, end_evidence=None), [source()])["events"][0]
    assert event["end_at"] == "2026-10-28T16:00:00+08:00"


@pytest.mark.parametrize("duration", [None, 0, -1, True, 2.5, "120", 10081])
def test_missing_or_invalid_duration_does_not_invent_end(duration):
    event = validate_fixed_events(payload(end_at=None, end_evidence=None, duration_minutes=duration),
                                  [source()])["events"][0]
    assert event["end_at"] is None
    with pytest.raises(ValueError, match="exact start and end"):
        validate_confirmed_event(event)


def test_estimated_study_minutes_are_not_event_duration():
    event = validate_fixed_events(payload(end_at=None, end_evidence=None,
                                         duration_evidence="AI suggests 120 minutes"), [source()])["events"][0]
    assert event["duration_minutes"] is None
    assert event["end_at"] is None


def test_duration_value_must_equal_the_explicit_numeric_evidence():
    event = validate_fixed_events(payload(end_at=None, end_evidence=None, duration_minutes=90),
                                  [source()])["events"][0]
    assert event["duration_minutes"] is None and event["end_at"] is None


def test_missing_start_does_not_use_deadline_or_duration():
    event = validate_fixed_events(payload(start_at=None, start_evidence=None, due_at="2026-10-28T23:59:00+08:00"),
                                  [source()])["events"][0]
    assert event["start_at"] is None
    with pytest.raises(ValueError):
        validate_confirmed_event(event)


def test_no_quote_or_wrong_source_drops_event():
    result = validate_fixed_events(payload(sources=[{"source_id": "missing", "source_quote": QUOTE}]), [source()])
    assert not result["events"]
    assert result["warnings"]


def test_relative_date_without_anchor_is_not_resolved():
    event = validate_fixed_events(payload(time_basis="message_relative"), [source()])["events"][0]
    assert event["start_at"] is None and event["end_at"] is None


def test_conflicting_end_and_explicit_duration_stays_review():
    text = QUOTE + " The duration is 90 minutes."
    event = validate_fixed_events(payload(duration_minutes=90, duration_evidence="90 minutes",
                                         sources=[{"source_id": "body", "source_quote": text}]),
                                  [source(text=text)])["events"][0]
    assert event["end_at"] is None
    assert any("conflict" in note for note in event["needs_clarification"])


def test_attachment_page_and_body_sources_are_both_retained():
    raw = payload(sources=[{"source_id": "body", "source_quote": QUOTE},
                           {"source_id": "attachment-0", "source_quote": QUOTE}])
    sources = [source(), source(id="attachment-0", source_attachment_id="att-a",
                                source_filename="exam.pdf", attachment_sha256="abc",
                                pages=[{"page": 3, "text": QUOTE}])]
    event = validate_fixed_events(raw, sources)["events"][0]
    assert len(event["sources"]) == 2
    assert event["sources"][1]["source_filename"] == "exam.pdf"
    assert event["sources"][1]["source_page"] == 3


def test_repeat_mentions_merge_only_same_occurrence_and_source():
    first, second = proposal(), proposal()
    second["sources"][0].update(source_attachment_id="a", source_filename="exam.pdf", attachment_sha256="sha")
    merged = merge_fixed_event_proposals([first, second])
    assert len(merged) == 1 and len(merged[0]["sources"]) == 2
    second["start_at"] = "2026-10-29T14:00:00+08:00"
    assert len(merge_fixed_event_proposals([first, second])) == 2


@pytest.mark.parametrize("change", [
    {"course": "DSS5102"}, {"kind": "presentation"}, {"location": "LT2"},
    {"in_person": False}, {"event_key": "DSS5105 midterm exam"},
])
def test_conflicts_are_not_silently_merged(change):
    assert len(merge_fixed_event_proposals([proposal(), proposal(**change)])) == 2


def test_same_times_across_accounts_or_unrelated_mail_do_not_merge():
    second = proposal(account="another@example.com")
    second["sources"][0]["account"] = "another@example.com"
    assert len(merge_fixed_event_proposals([proposal(), second])) == 2
    second = proposal()
    second["sources"][0]["mail_id"] = "unrelated"
    assert len(merge_fixed_event_proposals([proposal(), second])) == 2


def test_store_repeated_extraction_preserves_human_edits(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    event = proposal()
    identifier = store.save_fixed_events([event])[0]
    store.update_fixed_event(identifier, {"title": "My corrected exam", "start_at": "2026-10-28T15:00:00+08:00",
                                          "end_at": "2026-10-28T17:00:00+08:00", "travel_minutes": 40}, account=ACCOUNT)
    assert store.save_fixed_events([event]) == [identifier]
    saved = store.list_fixed_events(ACCOUNT)
    assert len(saved) == 1
    assert saved[0]["title"] == "My corrected exam"
    assert saved[0]["travel_minutes"] == 40
    assert saved[0]["start_at"].endswith("15:00:00+08:00")


def test_save_first_review_time_edit_then_replay_keeps_edit(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    edited = proposal(start_at="2026-10-28T15:00:00+08:00", end_at="2026-10-28T17:00:00+08:00")
    identifier = store.save_fixed_events([edited])[0]
    assert store.save_fixed_events([proposal()]) == [identifier]
    assert store.save_fixed_events([edited]) == [identifier]
    assert len(store.list_fixed_events(ACCOUNT)) == 1


def test_store_atomicity_account_isolation_and_delete(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    with pytest.raises(ValueError):
        store.save_fixed_events([proposal(), proposal(end_at=None)])
    assert store.list_fixed_events() == []
    identifier = store.save_fixed_events([proposal()])[0]
    assert store.list_fixed_events("other@example.com") == []
    with pytest.raises(ValueError):
        store.delete_fixed_event(identifier, account="other@example.com")
    with pytest.raises(ValueError):
        store.update_fixed_event(identifier, {"title": "wrong"}, account="other@example.com")
    store.delete_fixed_event(identifier, account=ACCOUNT)
    assert store.list_fixed_events() == []


def test_incomplete_proposal_filled_by_human_is_idempotent(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    incomplete = validate_fixed_events(payload(start_at=None, end_at=None, start_evidence=None,
                                              end_evidence=None), [source()])["events"][0]
    rows = event_review_rows([incomplete])
    rows[0].update({"Include": True, "Start (Singapore)": "2026-10-28 14:00",
                    "End (Singapore)": "2026-10-28 16:00"})
    reviewed = reviewed_fixed_events(rows, [incomplete])
    identifier = store.save_fixed_events(reviewed)[0]
    assert store.save_fixed_events(reviewed) == [identifier]
    assert len(store.list_fixed_events(ACCOUNT)) == 1


def test_review_only_selected_rows_and_rejects_incomplete():
    events = [proposal(), proposal(title="Other appointment")]
    rows = event_review_rows(events)
    rows[0]["Include"] = True
    reviewed = reviewed_fixed_events(rows, events)
    assert len(reviewed) == 1 and reviewed[0]["status"] == "confirmed"
    rows[0]["End (Singapore)"] = ""
    with pytest.raises(ValueError):
        reviewed_fixed_events(rows, events)


@pytest.mark.parametrize("value", ["2026-10-28", "tomorrow", "14:00"])
def test_review_requires_explicit_date_and_time(value):
    with pytest.raises(ValueError):
        parse_event_time(value)


def test_busy_adapter_uses_actual_duration_commute_and_unique_unknown_location():
    event = validate_confirmed_event(proposal(id="first", location="", travel_minutes=40))
    window = as_busy_events([event])[0]
    assert window["start"] == event["start_at"] and window["end"] == event["end_at"]
    assert window["mode"] == "onsite" and window["commute_minutes"] == 40
    assert window["location"] == "fixed-event-first"
    assert as_busy_events([proposal()]) == []


def test_source_collection_rejects_other_account_before_parsing():
    with pytest.raises(ValueError, match="different account"):
        prepare_sources({"id": "a", "gmail_account": "other@example.com"}, ACCOUNT)


def test_ai_boundary_mocked_schema_and_prompt_distinguish_preparation(monkeypatch):
    import langchain_openai

    mocked = Mock()
    mocked.return_value.with_structured_output.return_value.invoke.return_value = payload()
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", mocked)
    mail = {"id": "mail-a", "gmail_account": ACCOUNT, "body": QUOTE, "subject": "exam", "attachments": []}
    result = extract_fixed_events(mail, "fake-key", "test-model", account=ACCOUNT)
    assert len(result["events"]) == 1
    response_schema = mocked.return_value.with_structured_output.call_args.args[0].model_json_schema()
    assert "start_evidence" in response_schema["$defs"]["Event"]["properties"]
    assert "Exclude flexible preparation" in SYSTEM_PROMPT
    assert "Do not create study tasks for" in TASK_PROMPT
    assert "Do not invent preparation work" in TASK_PROMPT


def test_multiple_evidence_accounts_are_rejected():
    data = payload(sources=[{"source_id": "body", "source_quote": QUOTE},
                            {"source_id": "other", "source_quote": QUOTE}])
    result = validate_fixed_events(data, [source(), source(id="other", account="other@example.com")])
    assert result["events"] == []


def test_provider_failures_do_not_expose_source_or_key(monkeypatch):
    import langchain_openai

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Mock(side_effect=RuntimeError("PRIVATE")))
    with pytest.raises(RuntimeError, match="No events were saved") as captured:
        extract_fixed_events({"id": "a", "gmail_account": ACCOUNT, "body": QUOTE},
                             "secret", "fake", account=ACCOUNT)
    assert "PRIVATE" not in str(captured.value)
