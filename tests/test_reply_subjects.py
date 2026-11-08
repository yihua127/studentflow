"""Source-bound reply subjects, using offline business tools and fake Gmail metadata."""

import sqlite3
from copy import deepcopy
from email.header import Header

import pytest
from test_gmail_send import Inbox, preview

from studentflow.agent_tools import AssistantTools
from studentflow.reply_validation import original_reply_subject, reply_subject_matches


@pytest.mark.parametrize(("original", "expected"), [
    ("Coursework instructions", "Re: Coursework instructions"),
    ("  Coursework instructions  ", "Re: Coursework instructions"),
    ("re: Coursework instructions", "re: Coursework instructions"),
    ("Re: re: Coursework instructions", "Re: re: Coursework instructions"),
    ("Fwd: Coursework instructions", "Re: Fwd: Coursework instructions"),
    ("课程作业要求", "Re: 课程作业要求"),
    (Header("课程作业要求", "utf-8").encode(), "Re: 课程作业要求"),
    ("=?UTF-8?Q?Re=3A_Coursework_instructions?=", "Re: Coursework instructions"),
    ("x" * 994, "Re: " + "x" * 994),
    ("Re: " + "x" * 994, "Re: " + "x" * 994),
])
def test_original_subject_is_preserved_with_one_reply_prefix_if_needed(original, expected):
    assert original_reply_subject(original) == expected


@pytest.mark.parametrize("original", [
    None, 123, "", "   ", "Re:", "Re: re: ", "x" * 995,
    "Re: " + "x" * 995, "x" * 8193, "Course\nBcc: attacker@example.test",
    "Course\r\n continuation", "Course\twork", "Course\x00work", "Course\x7fwork",
    "=?unknown-charset?b?dGVzdA==?=", "=?utf-8?b?a?=", "=?utf-8?invalid?test?=",
    "=?utf-8?b?Q291cnNld29yayBpbnN0cnVjdGlvbnMKQmNjOiBhdHRhY2tlckBleGFtcGxlLnRlc3Q=?=",
])
def test_unsafe_empty_or_oversized_original_subject_is_not_guessed(original):
    assert original_reply_subject(original) is None


@pytest.mark.parametrize(("subject", "original", "matches"), [
    ("Re: Coursework instructions", "Coursework instructions", True),
    ("re: RE: COURSEWORK INSTRUCTIONS", "Coursework instructions", True),
    ("Coursework instructions", "Re: Coursework instructions", True),
    ("Re: 课程作业要求", Header("课程作业要求", "utf-8").encode(), True),
    ("Re: Coursework  instructions", "Coursework instructions", False),
    ("Re: Coursework instruction", "Coursework instructions", False),
    ("Re: FW: Coursework instructions", "Coursework instructions", False),
    ("Different subject", "Coursework instructions", False),
    ("Re: Coursework instructions\nBcc: attacker@example.test", "Coursework instructions", False),
    ("Re:", "", False),
    (None, "Coursework instructions", False),
])
def test_subject_match_has_no_fuzzy_thread_matching(subject, original, matches):
    assert reply_subject_matches(subject, original) is matches


def make_box(*, provider="gmail", original="Coursework instructions", include_title=True):
    source = {"source_id": "email", "provider": provider, "sender": "teacher@example.test",
              "text": "Please acknowledge receipt of the instructions."}
    if include_title:
        source["title"] = original
    snapshot = {"sources": [source]}
    return AssistantTools(snapshot), snapshot


def draft_payload():
    return {"source_id": "email", "subject": "Re: A model-written replacement title",
            "body": "Hello,\nThank you for the instructions.\nAlex", "recipient": "teacher@example.test",
            "citations": [{"source_id": "email", "quote": "Please acknowledge receipt of the instructions."}],
            "style_source_ids": []}


def test_recorded_gmail_draft_uses_original_subject_and_passes_thread_review(monkeypatch):
    def no_database(*_args, **_kwargs):
        raise AssertionError("Recording a draft must not open a database")
    monkeypatch.setattr(sqlite3, "connect", no_database)
    box, snapshot = make_box()
    before = deepcopy(snapshot)
    proposed = draft_payload()
    result = box.invoke("record_reply_draft", proposed, role="email")
    assert result["ok"] and result["sent"] is False
    recorded = box.artifacts["reply_drafts"][0]
    assert recorded["subject"] == "Re: Coursework instructions"
    for field in ("body", "recipient", "citations", "style_source_ids"):
        assert recorded[field] == proposed[field]
    assert recorded["saved"] is False and recorded["sent"] is False
    assert snapshot == before
    # The actual unchanged sending validator accepts the source-bound title.
    reviewed = preview(Inbox(), subject=recorded["subject"], body=recorded["body"])
    assert reviewed.subject == recorded["subject"]


@pytest.mark.parametrize("original", ["re: Re: Coursework instructions", "x" * 600, "x" * 994])
def test_original_subject_overrides_model_without_rewriting_existing_prefix_or_truncating(original):
    box, _ = make_box(original=original)
    proposed = draft_payload()
    assert len(proposed["subject"]) <= 500
    result = box.invoke("record_reply_draft", proposed, role="email")
    assert result["ok"]
    assert box.artifacts["reply_drafts"][0]["subject"] == original_reply_subject(original)
    assert len(box.artifacts["reply_drafts"][0]["subject"]) <= 998


@pytest.mark.parametrize("original", [None, "", "Course\nBcc: attacker@example.test",
                                       "=?unknown-charset?b?dGVzdA==?=", "x" * 999])
def test_present_but_invalid_gmail_title_rejects_without_falling_back_to_model(original):
    box, _ = make_box(original=original)
    result = box.invoke("record_reply_draft", draft_payload(), role="email")
    assert result["ok"] is False and result["error"]["code"] == "invalid_subject"
    assert not box.artifacts["reply_drafts"]


@pytest.mark.parametrize(("provider", "include_title"), [("attachment", True), ("gmail", False)])
def test_non_gmail_or_legacy_missing_title_keeps_existing_draft_behavior(provider, include_title):
    box, _ = make_box(provider=provider, include_title=include_title)
    result = box.invoke("record_reply_draft", draft_payload(), role="email")
    assert result["ok"]
    assert box.artifacts["reply_drafts"][0]["subject"] == draft_payload()["subject"]
    assert "kept from the original" not in result["message"]
