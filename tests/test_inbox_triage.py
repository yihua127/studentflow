"""Inbox triage contracts; all assistant responses are local/offline fixtures."""

import json
import sqlite3
from copy import deepcopy
from types import SimpleNamespace

import pytest

from studentflow import inbox_triage as triage
from studentflow.assistant_context import selectable_sources
from studentflow.gmail import GmailAuthError, GmailError

ACCOUNT = "student@example.invalid"


def mail(number=1, **changes):
    return {
        "id": f"local-{number}", "gmail_id": f"abc{number}", "provider": "gmail",
        "gmail_account": ACCOUNT, "subject": f"Course message {number}",
        "sender": "Teacher <teacher@example.invalid>",
        "body": f"Please reply to confirm receipt of task {number}.",
        "sent_at": "2026-11-05T10:00:00+08:00", **changes,
    }


def completed(box, *, category="needs_reply", status="completed", skip=0):
    sources = box.invoke("list_sources", {}, role="email")["sources"]
    for source in sources[skip:]:
        identifier = source["source_id"]
        text = box.invoke("read_source", {"source_id": identifier}, role="email")["text"]
        assert box.invoke("record_triage", {"items": [{
            "source_id": identifier, "category": category, "reason": "The sender requests a reply.",
            "citations": [{"source_id": identifier, "quote": text}],
        }]}, role="email")["ok"]
    return SimpleNamespace(status=status, usage={"model_calls": 2, "input_tokens": 10,
                                                 "usage_complete": True}, events=[])


def runner(_request, box, **_kwargs):
    return completed(box)


def unexpected_call(*_args, **_kwargs):
    raise AssertionError("No new classification was expected")


def classify(mails=None, **kwargs):
    return triage.classify_messages(mails if mails is not None else [mail()], ACCOUNT,
                                   "TEST-KEY", kwargs.pop("model", "model-a"),
                                   runner=kwargs.pop("runner", runner), **kwargs)


def test_batch_is_bounded_and_results_keep_message_order():
    chunks, progress = [], []

    def count_runner(request, box, **kwargs):
        chunks.append(box.selection_summary()["email_sources"])
        assert box.selection_summary()["saved_tasks"] == 0
        return runner(request, box, **kwargs)

    result = classify([mail(i) for i in range(12)], runner=count_runner,
                      progress=lambda done, _label: progress.append(done))
    assert result["status"] == "completed"
    assert chunks == [3, 3, 3, 3]
    assert [item["gmail_id"] for item in result["results"]] == [f"abc{i}" for i in range(12)]
    assert result["usage"]["model_calls"] == 8
    assert progress[-1] == 1.0


def test_large_selection_has_no_fixed_cap():
    result = classify([mail(i) for i in range(31)])
    assert result["status"] == "completed"
    assert len(result["results"]) == 31


def test_identical_content_keeps_distinct_gmail_messages():
    result = classify([mail(1), mail(1, gmail_id="second-copy")])
    assert result["status"] == "completed"
    assert len(result["results"]) == 2
    assert result["results"][0]["citations"][0]["source_id"] != (
        result["results"][1]["citations"][0]["source_id"])


def test_empty_body_stays_unclassified_without_model_call():
    result = classify([mail(body="   ")], runner=unexpected_call)
    record = result["results"][0]
    assert result["status"] == "failed"
    assert record["category"] is None and record["error"] == "unreadable_body"
    assert result["usage"]["model_calls"] == 0


def test_truncated_body_stays_unclassified_without_guessing():
    result = classify([mail(warnings=[
        "The message body exceeds 200,000 characters and has been shortened.",
    ])], runner=unexpected_call)
    assert result["results"][0]["error"] == "incomplete_body"
    assert result["results"][0]["category"] is None


def test_missing_record_is_partial_and_prose_does_not_count():
    def missing(_request, box, **_kwargs):
        response = completed(box, skip=1)
        response.answer = "Both messages were classified successfully."
        response.artifacts = {"triage": [{"category": "ignore", "source_id": "forged"}]}
        return response

    result = classify([mail(1), mail(2)], runner=missing)
    assert result["status"] == "partial"
    assert result["results"][0]["error"] == "missing_result"
    assert result["results"][0]["category"] is None
    assert result["results"][1]["category"] == "needs_reply"


def test_conflicting_duplicate_results_are_not_silently_selected():
    def duplicate(_request, box, **_kwargs):
        completed(box)
        return completed(box, category="ignore")

    result = classify(runner=duplicate)
    assert result["status"] == "failed"
    assert result["results"][0]["error"] == "duplicate_result"


@pytest.mark.parametrize("tamper", ["foreign_source", "foreign_quote", "invented_quote"])
def test_scope_and_evidence_checked_at_batch_boundary(tamper):
    def corrupt(_request, box, **_kwargs):
        response = completed(box)
        item = box.artifacts["triage"][0]
        if tamper == "foreign_source":
            item["source_id"] = "not-selected"
        elif tamper == "foreign_quote":
            item["citations"][0]["source_id"] = "not-selected"
        else:
            item["citations"][0]["quote"] = "A fabricated requirement."
        return response

    result = classify(runner=corrupt)
    assert result["status"] == "failed"
    assert result["results"][0]["category"] is None


def test_other_account_and_duplicate_ids_rejected_before_any_processing(tmp_path):
    store = triage.InboxTriageStore(tmp_path / "state.sqlite")
    with pytest.raises(ValueError, match="different"):
        classify([mail(1), mail(2, gmail_account="other@example.invalid")], store=store,
                 runner=unexpected_call)
    assert store.list(ACCOUNT) == {}
    with pytest.raises(ValueError, match="only once"):
        classify([mail(), mail()], runner=unexpected_call)


def test_cache_survives_reopening_is_account_scoped_and_whitelists_fields(tmp_path):
    path = tmp_path / "state.sqlite"
    store = triage.InboxTriageStore(path)
    result = classify(store=store)
    saved = dict(result["results"][0], api_key="SECRET", credentials={"refresh_token": "SECRET"})
    saved["citations"][0]["token"] = "SECRET"
    store.save_suggestion(saved)
    reopened = triage.InboxTriageStore(path)
    again = classify(store=reopened, runner=unexpected_call)
    assert again["results"][0]["cached"] is True
    assert reopened.get("other@example.invalid", mail()["gmail_id"]) is None
    assert reopened.get(ACCOUNT.upper(), mail()["gmail_id"]) is not None
    assert reopened.list(ACCOUNT, []) == {}
    with sqlite3.connect(path) as db:
        payload = db.execute("SELECT payload FROM inbox_triage").fetchone()[0]
    assert "SECRET" not in payload and "api_key" not in payload


def test_reviewed_edit_survives_reanalysis_and_model_change(tmp_path):
    store = triage.InboxTriageStore(tmp_path / "state.sqlite")
    first = classify(store=store)["results"][0]
    reviewed = store.review(ACCOUNT, first["gmail_id"], "needs_action",
                            expected_fingerprint=first["fingerprint"],
                            expected_updated_at=first["updated_at"])
    assert reviewed["reviewed"] and reviewed["updated_at"] != first["updated_at"]
    repeated = classify(store=store, reanalyze=True, model="model-b")["results"][0]
    assert repeated["model"] == "model-b"
    assert repeated["suggested_category"] == "needs_reply"
    assert repeated["category"] == "needs_action" and repeated["reviewed"]
    assert repeated["reviewed_at"] == reviewed["reviewed_at"]
    assert repeated["fingerprint"] == first["fingerprint"]


def test_content_change_invalidates_review_and_stale_review_is_rejected(tmp_path):
    store = triage.InboxTriageStore(tmp_path / "state.sqlite")
    first = classify(store=store)["results"][0]
    store.review(ACCOUNT, first["gmail_id"], "ignore", expected_fingerprint=first["fingerprint"])
    second = classify([mail(body="Please reply with your new availability.")], store=store)["results"][0]
    assert second["fingerprint"] != first["fingerprint"]
    assert second["reviewed"] is False and second["category"] == "needs_reply"
    with pytest.raises(ValueError, match="changed"):
        store.review(ACCOUNT, first["gmail_id"], "ignore", expected_fingerprint=first["fingerprint"])
    with pytest.raises(ValueError, match="changed"):
        store.review(ACCOUNT, first["gmail_id"], "ignore", expected_fingerprint=second["fingerprint"],
                     expected_updated_at=first["updated_at"])


def test_cache_version_invalidates_without_erasing_review(tmp_path, monkeypatch):
    store = triage.InboxTriageStore(tmp_path / "state.sqlite")
    first = classify(store=store)["results"][0]
    store.review(ACCOUNT, first["gmail_id"], "ignore", expected_fingerprint=first["fingerprint"])
    monkeypatch.setattr(triage, "TRIAGE_VERSION", "new-prompt-version")
    new = classify(store=store)["results"][0]
    assert new["version"] == "new-prompt-version"
    assert new["category"] == "ignore" and new["reviewed"]


def test_partial_runner_with_every_message_verified_is_complete():
    result = classify(runner=lambda _req, box, **_kw: completed(box, status="partial"))
    assert result["status"] == "completed"
    assert result["results"][0]["status"] == "classified"
    assert result["warnings"] == []


def test_partial_runner_missing_a_message_does_not_claim_completion():
    result = classify([mail(1), mail(2)],
                      runner=lambda _req, box, **_kw: completed(box, status="partial", skip=1))
    assert result["status"] == "partial"
    assert sorted(item["status"] for item in result["results"]) == ["classified", "unclassified"]
    assert any("stopped before completing" in warning for warning in result["warnings"])


def test_exception_is_sanitized_and_does_not_overwrite_review(tmp_path):
    store = triage.InboxTriageStore(tmp_path / "state.sqlite")
    first = classify(store=store)["results"][0]
    reviewed = store.review(ACCOUNT, first["gmail_id"], "ignore",
                            expected_fingerprint=first["fingerprint"])

    def fail(_request, _box, **_kwargs):
        raise RuntimeError("SECRET provider details and email content")

    result = classify(store=store, reanalyze=True, runner=fail)
    assert result["status"] == "failed" and result["results"][0]["category"] is None
    assert "SECRET" not in json.dumps(result)
    assert store.get(ACCOUNT, first["gmail_id"]) == reviewed


def test_corrupt_saved_record_is_ignored_and_reclassified(tmp_path):
    store = triage.InboxTriageStore(tmp_path / "state.sqlite")
    classify(store=store)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE inbox_triage SET payload=?", (json.dumps({"category": "ignore"}),))
    assert store.get(ACCOUNT, "abc1") is None
    result = classify(store=store)
    assert result["status"] == "completed" and not result["results"][0]["cached"]


def test_user_override_never_changes_original_ai_reason_or_evidence(tmp_path):
    store = triage.InboxTriageStore(tmp_path / "state.sqlite")
    first = classify(store=store)["results"][0]
    original = deepcopy(first)
    reviewed = store.review(ACCOUNT, "abc1", "needs_action",
                            expected_fingerprint=first["fingerprint"])
    assert reviewed["suggested_category"] == "needs_reply"
    assert reviewed["reason"] == original["reason"]
    assert reviewed["citations"] == original["citations"]
    source = selectable_sources({mail()["gmail_id"]: mail()}, [], [])[0]
    assert reviewed["citations"][0]["source_id"] == source["source_id"]


def test_persistence_failure_is_marked_but_preserves_reviewable_result(tmp_path, monkeypatch):
    store = triage.InboxTriageStore(tmp_path / "state.sqlite")

    def fail(_record):
        raise sqlite3.OperationalError("SECRET database details")

    monkeypatch.setattr(store, "save_suggestion", fail)
    result = classify(store=store)
    assert result["results"][0]["persistence_failed"] is True
    assert result["results"][0]["category"] == "needs_reply"
    assert result["results"][0]["citations"]
    assert result["warnings"] and "SECRET" not in json.dumps(result)


class Mailbox:
    def __init__(self, account=ACCOUNT, fail_ids=()):
        self.account = account
        self.calls = []
        self.fail_ids = set(fail_ids)
        self.closed = False

    def profile(self):
        return {"email_address": self.account}

    def fetch_eml(self, identifier):
        self.calls.append(identifier)
        if identifier in self.fail_ids:
            raise GmailError("SECRET provider and message details")
        return (b"From: teacher@example.invalid\r\nSubject: Same content\r\n"
                b"Date: Thu, 5 Nov 2026 10:00:00 +0800\r\n\r\nPlease reply to confirm.")

    def close(self):
        self.closed = True


def fetch(monkeypatch, mailbox, identifiers, mails=None):
    monkeypatch.setattr(triage, "connect", lambda *_args: mailbox)
    mails = {} if mails is None else mails
    result = triage.fetch_classification_messages(identifiers, "client", "token",
                                                  {"email_address": ACCOUNT}, mails)
    return result, mails


def test_fetch_identical_mime_has_distinct_ids_and_can_feed_reply_handoff(monkeypatch):
    from studentflow.reply_handoff import consume_reply_handoff, queue_reply_handoff

    mailbox = Mailbox()
    result, mails = fetch(monkeypatch, mailbox, ["first", "second"])
    assert len(mails) == 2 and len({item["mail_id"] for item in result}) == 2
    assert len({item["raw_sha256"] for item in mails.values()}) == 1
    assert mailbox.calls == ["first", "second"] and mailbox.closed
    state = {}
    selected = result[1]["mail_id"]
    queue_reply_handoff(state, ACCOUNT, selected, mails)
    handoff = consume_reply_handoff(state, selectable_sources(mails, [], []), ACCOUNT)
    assert handoff["mail_id"] == selected
    classified = classify(list(mails.values()))
    assert [item["gmail_id"] for item in classified["results"]] == ["first", "second"]


def test_fetch_checks_account_before_reading_or_reusing_any_message(monkeypatch):
    mailbox = Mailbox(account="other@example.invalid")
    cached = {"legacy": mail(gmail_id="first")}
    with pytest.raises(GmailAuthError, match="changed"):
        fetch(monkeypatch, mailbox, ["first"], cached)
    assert mailbox.calls == [] and mailbox.closed
    assert list(cached) == ["legacy"]


def test_fetch_preserves_partial_success_and_redacts_failure(monkeypatch):
    mailbox = Mailbox(fail_ids=["bad"])
    result, mails = fetch(monkeypatch, mailbox, ["first", "bad", "last"])
    assert [item["gmail_id"] for item in result] == ["first", "bad", "last"]
    assert "error" in result[1] and len(mails) == 2
    assert "SECRET" not in json.dumps(result)
    assert mailbox.closed


def test_fetch_reuses_only_same_account_gmail_id_and_keeps_cached_identity(monkeypatch):
    mailbox = Mailbox()
    cached = {
        "local-1": mail(gmail_id="first"),
        "foreign": mail(gmail_id="second", gmail_account="other@example.invalid"),
    }
    original = deepcopy(cached)
    result, mails = fetch(monkeypatch, mailbox, ["first", "second", "first"], cached)
    assert mailbox.calls == ["second"]
    assert len(result) == 2 and result[0]["mail_id"] == "local-1"
    assert len(mails) == 3
    assert mails["local-1"]["body"] == original["local-1"]["body"]
    assert mails["foreign"] == original["foreign"]
    assert mails[result[0]["mail_id"]]["gmail_account"] == ACCOUNT


def test_fetch_existing_legacy_email_does_not_add_alias_or_change_id(monkeypatch):
    mailbox = Mailbox()
    cached = {"legacy-raw-id": mail(id="legacy-raw-id", gmail_id="first")}
    result, mails = fetch(monkeypatch, mailbox, ["first"], cached)
    assert mailbox.calls == []
    assert result == [{"gmail_id": "first", "mail_id": "legacy-raw-id"}]
    assert list(mails) == ["legacy-raw-id"]
    assert mails["legacy-raw-id"]["id"] == "legacy-raw-id"


def test_fetch_cached_identity_collision_cannot_overwrite_other_email(monkeypatch):
    mailbox = Mailbox()
    cached = {
        "shared-id": mail(id="shared-id", gmail_id="other"),
        "miskeyed-entry": mail(id="shared-id", gmail_id="first"),
    }
    old_owner = deepcopy(cached["shared-id"])
    result, mails = fetch(monkeypatch, mailbox, ["first"], cached)
    assert mailbox.calls == []
    assert result[0]["mail_id"] != "shared-id"
    assert mails["shared-id"] == old_owner
    assert mails[result[0]["mail_id"]]["gmail_id"] == "first"


def add_tag(path, name="Course project", account=ACCOUNT):
    from studentflow.email_label_catalog import LabelCatalog

    catalog = LabelCatalog(path)
    before = catalog.get(account)
    after = catalog.add(account, name, expected_version=before["version"])
    return next(iter(set(after["custom_ids"]) - set(before["custom_ids"])))


def test_manual_tags_persist_through_reopen_reanalysis_and_model_changes(tmp_path):
    path = tmp_path / "state.sqlite"
    store = triage.InboxTriageStore(path)
    first = classify(store=store)["results"][0]
    first_tag, second_tag = add_tag(path), add_tag(path, "Urgent")
    reviewed = store.review(ACCOUNT, "abc1", "needs_action",
                            expected_fingerprint=first["fingerprint"],
                            tag_ids=[second_tag, first_tag])
    assert reviewed["tag_ids"] == sorted([first_tag, second_tag])
    reopened = triage.InboxTriageStore(path)
    assert reopened.get(ACCOUNT, "abc1")["tag_ids"] == reviewed["tag_ids"]
    repeated = classify(store=reopened, reanalyze=True, model="other-model")["results"][0]
    assert repeated["tag_ids"] == reviewed["tag_ids"]
    assert repeated["category"] == "needs_action" and repeated["reviewed"]
    assert repeated["suggested_category"] == "needs_reply"


def test_category_only_review_preserves_tags_and_explicit_empty_clears_them(tmp_path):
    store = triage.InboxTriageStore(tmp_path / "state.sqlite")
    first = classify(store=store)["results"][0]
    tag = add_tag(store.path)
    with_tag = store.review(ACCOUNT, "abc1", "needs_reply",
                            expected_fingerprint=first["fingerprint"], tag_ids=[tag])
    category_only = store.review(ACCOUNT, "abc1", "ignore",
                                 expected_fingerprint=with_tag["fingerprint"])
    assert category_only["tag_ids"] == [tag]
    cleared = store.review(ACCOUNT, "abc1", "ignore",
                          expected_fingerprint=category_only["fingerprint"], tag_ids=[])
    assert cleared["tag_ids"] == []


def test_source_change_resets_manual_tags_with_old_classification_review(tmp_path):
    store = triage.InboxTriageStore(tmp_path / "state.sqlite")
    first = classify(store=store)["results"][0]
    store.review(ACCOUNT, "abc1", "ignore", expected_fingerprint=first["fingerprint"],
                 tag_ids=[add_tag(store.path)])
    new = classify([mail(body="Please reply about the revised project.")], store=store)["results"][0]
    assert new["tag_ids"] == [] and not new["reviewed"]


def test_manual_tags_must_belong_to_the_reviewed_account(tmp_path):
    store = triage.InboxTriageStore(tmp_path / "state.sqlite")
    first = classify(store=store)["results"][0]
    foreign_tag = add_tag(store.path, account="another@example.invalid")
    for invalid_tag in (foreign_tag, "tag_" + "0" * 32):
        with pytest.raises(ValueError, match="not available for this account"):
            store.review(ACCOUNT, "abc1", "ignore", expected_fingerprint=first["fingerprint"],
                         tag_ids=[invalid_tag])
    assert store.get(ACCOUNT, "abc1") == first


@pytest.mark.parametrize("invalid", [
    "tag_" + "0" * 32, (), [None], [42], ["not-a-tag"], ["needs_reply"],
    ["tag_" + "A" * 32], ["tag_" + "0" * 32, "tag_" + "0" * 32],
])
def test_invalid_manual_tag_ids_are_rejected_without_changes(tmp_path, invalid):
    store = triage.InboxTriageStore(tmp_path / "state.sqlite")
    first = classify(store=store)["results"][0]
    with pytest.raises(ValueError, match="distinct custom labels"):
        store.review(ACCOUNT, "abc1", "ignore", expected_fingerprint=first["fingerprint"],
                     tag_ids=invalid)
    assert store.get(ACCOUNT, "abc1") == first


def test_suggestion_cannot_add_unreviewed_manual_tags(tmp_path):
    store = triage.InboxTriageStore(tmp_path / "state.sqlite")
    first = classify(store=store)["results"][0]
    forged = dict(first, tag_ids=[add_tag(store.path)])
    stored = store.save_suggestion(forged)
    assert stored["tag_ids"] == [] and not stored["reviewed"]


def test_legacy_saved_records_default_to_empty_tags(tmp_path):
    store = triage.InboxTriageStore(tmp_path / "state.sqlite")
    first = classify(store=store)["results"][0]
    legacy = {key: value for key, value in first.items() if key != "tag_ids"}
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE inbox_triage SET payload=?", (json.dumps(legacy),))
    assert store.get(ACCOUNT, "abc1")["tag_ids"] == []
    reviewed = store.review(ACCOUNT, "abc1", "ignore",
                            expected_fingerprint=first["fingerprint"])
    assert reviewed["tag_ids"] == []
