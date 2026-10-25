"""Synthetic body/document extraction, evidence reconciliation and reviewed storage.

The offline judge uses explicit fixture identities, never live credentials or a
model. These tests verify the integration contract, not live model accuracy.
"""

import io
from copy import deepcopy

import pytest
from docx import Document

from studentflow.batch_ui import reconcile_batch_results, review_rows, validate_selected_rows
from studentflow.extraction_cache import ExtractionCache
from studentflow.source_extraction import analyze_sources
from studentflow.storage import Store
from studentflow.task_reconciliation import reconcile_tasks

ACCOUNT = "learner@example.invalid"
DUE = "2026-10-30T18:00:00+08:00"


def mention(title, quote, identity, *, minutes=50, due=DUE):
    return {
        "task": {
            "title": title, "source_quote": quote, "due_at": due,
            "estimated_minutes": minutes, "deadline_basis": "absolute",
            "needs_clarification": [], "confidence": 0.9,
        },
        "identity": identity,
    }


def synthetic_pipeline(tmp_path, body_mentions, attachment_mentions):
    document = Document()
    for item in attachment_mentions:
        document.add_paragraph(item["task"]["source_quote"])
    content = io.BytesIO()
    document.save(content)
    mail = {
        "id": "synthetic-survey-mail", "gmail_id": "synthetic-survey-mail",
        "gmail_account": ACCOUNT, "provider": "gmail",
        "subject": "Campus survey project", "sent_at": "2026-10-23T12:00:00+08:00",
        "body": "\n".join(item["task"]["source_quote"] for item in body_mentions)
        or "For your information; the project brief is attached.",
        "attachments": [{
            "id": "survey-brief", "filename": "survey-brief.docx",
            "content": content.getvalue(), "warnings": [],
        }],
    }
    identities = {item["task"]["source_quote"]: item["identity"]
                  for item in body_mentions + attachment_mentions}
    extraction_calls, comparisons = [], []

    def extractor(body, subject, sent_at, api_key, model, attachments=None):
        kind = "attachment" if attachments else "body"
        extraction_calls.append(kind)
        fixtures = attachment_mentions if attachments else body_mentions
        return {"tasks": [deepcopy(item["task"]) for item in fixtures], "warnings": []}

    def judge(pairs):
        comparisons.extend(deepcopy(pairs))
        decisions = []
        for pair in pairs:
            left_quote = pair["left"]["sources"][0]["quote"]
            right_quote = pair["right"]["sources"][0]["quote"]
            left, right = identities[left_quote], identities[right_quote]
            matches = ["same" if a == b else "different" for a, b in zip(left, right)]
            identical = all(match == "same" for match in matches)
            decisions.append({
                "pair_id": pair["pair_id"],
                "verdict": "same_task" if identical else "separate",
                "action_match": matches[0], "deliverable_match": matches[1],
                "occurrence_match": matches[2], "conflicting_details": False,
                "left_quote": left_quote, "right_quote": right_quote,
                "reason": "Both quotations identify the same submission."
                if identical else "The quotations describe distinct actions or deliverables.",
            })
        return {"decisions": decisions}

    def reconciler(proposals, references, api_key, model):
        return reconcile_tasks(proposals, references, api_key, model, judge=judge)

    cache = ExtractionCache(tmp_path / "sources.sqlite3")

    def extract(*, reanalyze=False):
        outcome = analyze_sources(
            [mail], ACCOUNT, "offline-key", "offline-model", extractor=extractor,
            cache=cache, reanalyze=reanalyze,
        )[mail["id"]]
        assert "result" in outcome
        assert outcome["partial"] is False
        return [{"mail_id": mail["id"], "gmail_id": mail["gmail_id"],
                 "cache_key": "synthetic-review", **outcome}]

    def reconcile(raw, references=()):
        return reconcile_batch_results(
            raw, {mail["id"]: mail}, list(references), "offline-key", "offline-model",
            reconciler=reconciler,
        )

    return {mail["id"]: mail}, extract, reconcile, extraction_calls, comparisons


def overlap_fixture():
    dataset = mention(
        "Submit cleaned campus survey dataset",
        "Submit the cleaned campus survey dataset by 29 October 2026 at 18:00; allow 40 minutes.",
        ("submit", "dataset", "survey-project"), minutes=40,
        due="2026-10-29T18:00:00+08:00",
    )
    analysis = mention(
        "Submit 200-word interpretation of campus survey results",
        "Submit a 200-word interpretation of the campus survey results by 30 October 2026 at 18:00; allow 50 minutes.",
        ("submit", "written-interpretation", "survey-project"),
    )
    paraphrase = mention(
        "Submit written interpretation for campus survey",
        "Write a 200-word explanation of the campus survey findings and hand it in by 30 October 2026 at 18:00; allow 50 minutes.",
        ("submit", "written-interpretation", "survey-project"),
    )
    slides = mention(
        "Upload campus survey presentation slides",
        "Upload the campus survey presentation slides by 31 October 2026 at 18:00; allow 30 minutes.",
        ("submit", "slides", "survey-project"), minutes=30,
        due="2026-10-31T18:00:00+08:00",
    )
    consent = mention(
        "Complete campus survey consent checklist",
        "Complete the campus survey consent checklist by 1 November 2026 at 18:00; allow 15 minutes.",
        ("complete", "consent-checklist", "survey-project"), minutes=15,
        due="2026-11-01T18:00:00+08:00",
    )
    return [dataset, analysis, slides], [deepcopy(dataset), paraphrase, consent]


@pytest.mark.parametrize("reanalyze", [False, True], ids=["cached-replay", "fresh-extraction"])
def test_body_attachment_overlap_merges_and_replay_preserves_human_edits(tmp_path, reanalyze):
    body, attachment = overlap_fixture()
    mails, extract, reconcile, calls, comparisons = synthetic_pipeline(tmp_path, body, attachment)
    raw = extract()
    snapshot = deepcopy(raw)
    assert len(review_rows(raw, mails)[0]) == 6
    result = reconcile(raw)
    assert raw == snapshot
    assert result[0]["result"]["warnings"] == raw[0]["result"]["warnings"]
    rows, sources = review_rows(result, mails)
    assert len(rows) == 4
    assert {row["Task"] for row in rows} == {
        item["task"]["title"] for item in body + [attachment[-1]]
    }
    assert len(comparisons) == 2
    assert sorted(calls) == ["attachment", "body"]
    for title in (body[0]["task"]["title"], body[1]["task"]["title"]):
        row = next(row for row in rows if row["Task"] == title)
        evidence = sources[row["_row_id"]]["_task_sources"]
        assert len(evidence) == 2
        assert {bool(source["source_attachment_id"]) for source in evidence} == {False, True}
        expected_quotes = {item["task"]["source_quote"] for item in body + attachment
                           if item["identity"] == next(
                               item["identity"] for item in body if item["task"]["title"] == title)}
        assert {source["source_quote"] for source in evidence} == expected_quotes
        assert "Duplicate mentions combined (2)." in row["Review notes"]

    store = Store(tmp_path / "reviewed-tasks.sqlite3")
    records = validate_selected_rows([dict(row, Include=True) for row in rows], sources, True)
    original_ids = store.save_tasks(records)
    analysis = next(task for task in store.tasks() if task["estimated_minutes"] == 50)
    store.update_task(analysis["id"], {
        "estimated_minutes": 75, "completed_minutes": 20,
        "due_at": "2026-11-02T20:00:00+08:00", "title": "My revised analysis task",
    })
    reopened = Store(store.path)
    replay = reconcile(extract(reanalyze=reanalyze), reopened.task_references())
    replay_rows, replay_sources = review_rows(replay, mails)
    assert len(replay_rows) == 4
    replay_records = validate_selected_rows(
        [dict(row, Include=True) for row in replay_rows], replay_sources, True,
    )
    assert reopened.save_tasks(replay_records) == original_ids
    saved = reopened.tasks()
    assert len(saved) == 4
    retained = next(task for task in saved if task["id"] == analysis["id"])
    assert retained["title"] == "My revised analysis task"
    assert retained["estimated_minutes"] == 75
    assert retained["completed_minutes"] == 20
    assert retained["remaining_minutes"] == 55
    assert retained["due_at"] == "2026-11-02T20:00:00+08:00"
    assert {source["source_quote"] for source in retained["sources"]} == {
        body[1]["task"]["source_quote"], attachment[1]["task"]["source_quote"],
    }
    assert len(calls) == (4 if reanalyze else 2)


@pytest.mark.parametrize(("left", "right"), [
    (
        mention("Submit survey report", "Upload the survey analysis file by the stated deadline.",
                ("submit", "analysis", "survey-project")),
        mention("Submit survey report", "Upload the separate survey chart file by the stated deadline.",
                ("submit", "chart", "survey-project")),
    ),
    (
        mention("Submit survey report", "Submit the draft survey report for feedback.",
                ("submit", "report", "draft")),
        mention("Submit survey report", "Submit the final survey report for grading.",
                ("submit", "report", "final")),
    ),
    (
        mention("Review survey report", "Review the survey report and record feedback.",
                ("review", "report", "survey-project")),
        mention("Submit survey report", "Submit the survey report to the instructor.",
                ("submit", "report", "survey-project")),
    ),
], ids=["separate-deliverables", "draft-and-final", "separate-actions"])
def test_same_date_and_effort_do_not_merge_separate_body_document_tasks(tmp_path, left, right):
    mails, extract, reconcile, _calls, comparisons = synthetic_pipeline(tmp_path, [left], [right])
    rows, sources = review_rows(reconcile(extract()), mails)
    assert len(comparisons) == 1
    assert len(rows) == 2
    assert {row["Supporting text"] for row in rows} == {
        left["task"]["source_quote"], right["task"]["source_quote"],
    }
    assert all(len(sources[row["_row_id"]]["_task_sources"]) == 1 for row in rows)


def test_attachment_tasks_survive_body_with_no_task_instructions(tmp_path):
    _body, attachment = overlap_fixture()
    mails, extract, reconcile, calls, comparisons = synthetic_pipeline(tmp_path, [], attachment)
    rows, sources = review_rows(reconcile(extract()), mails)
    assert len(rows) == 3
    assert {row["Task"] for row in rows} == {item["task"]["title"] for item in attachment}
    assert all(sources[row["_row_id"]]["_source_attachment_id"] == "survey-brief" for row in rows)
    assert sorted(calls) == ["attachment", "body"]
    assert comparisons == []
