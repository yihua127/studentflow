"""Conservative, source-bound duplicate detection for reviewed task proposals.

Canonical source item IDs identify cached and semantically linked proposals.
Legacy proposals retain conservative title matching. Provenance must also match.
No model calls or persistent changes are made here.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from copy import deepcopy
from datetime import UTC, datetime

from studentflow.course_codes import course_codes

SOURCE_FIELDS = (
    "mail_id", "gmail_account", "mail_subject", "mail_sent_at",
    "source_attachment_id", "source_filename", "source_quote", "attachment_sha256",
    "source_hash",
    "source_key", "source_item_id",
)
_INVALID_DEADLINE = object()
# A bare academic label has ONE action family, never a wildcard. This prevents
# an omitted verb from bridging, for example, preparing and submitting slides
# when proposals are merged or saved in different orders.
_SUBMISSION_ITEMS = (
    "outline", "essay", "slides", "report", "assignment", "homework",
    "proposal", "paper", "application", "worksheet", "problem set",
)
_ATTENDANCE_ITEMS = (
    "quiz", "exam", "test", "presentation", "lecture", "seminar", "workshop", "meeting",
)
_ASSESSMENT_ITEMS = ("quiz", "exam", "test")
_ACTION_PREFIX = re.compile(r"^(?:please )?(submit|upload|hand in|turn in|attend|take|sit) (.+)$")


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _normalized(value, *, title=False) -> str:
    result = " ".join(unicodedata.normalize("NFKC", _text(value)).casefold().split())
    # Do not strip internal punctuation, digits, course names, or action words.
    return result.rstrip(" .!?。！？;；:") if title else result


def _ends_with_item(label: str, items: tuple[str, ...]) -> bool:
    return any(label == item or label.endswith(" " + item) for item in items)


def _title_identity(value) -> tuple[str, str]:
    """Match omitted verbs only for a bounded academic noun/action pairing.

    Keep object wording, course codes, numbers and all other action words exact.
    In particular, "Prepare slides" and "Submit slides" stay separate even if
    the same batch also contains the ambiguous label "slides".
    """
    title = _normalized(value, title=True)
    match = _ACTION_PREFIX.fullmatch(title)
    action, label = match.groups() if match else (None, title)
    label = label.removeprefix("the ")
    if (action in {None, "submit", "upload", "hand in", "turn in"}
            and _ends_with_item(label, _SUBMISSION_ITEMS)):
        return "submission", label
    attendance_action = action in {None, "attend"} or (
        action in {"take", "sit"} and _ends_with_item(label, _ASSESSMENT_ITEMS)
    )
    if attendance_action and _ends_with_item(label, _ATTENDANCE_ITEMS):
        return "attendance", label
    return "exact", title


def _clean_source(source: dict) -> dict:
    result = {key: source.get(key) if isinstance(source.get(key), str) else None
              for key in SOURCE_FIELDS}
    result["gmail_account"] = _text(result["gmail_account"]).strip().casefold() or None
    digest = _text(result["attachment_sha256"]).strip().lower()
    result["attachment_sha256"] = digest if re.fullmatch(r"[0-9a-f]{64}", digest) else None
    source_hash = _text(result["source_hash"])
    result["source_hash"] = source_hash if re.fullmatch(r"[0-9a-f]{64}", source_hash) else None
    for field in ("source_key", "source_item_id"):
        value = _text(result[field])
        result[field] = value if re.fullmatch(r"[0-9a-f]{64}", value) else None
    if not result["source_key"] or not result["source_item_id"]:
        result["source_key"] = result["source_item_id"] = None
    return result


def source_evidence(task: dict, mail: dict) -> dict:
    """Capture a verified quote and safe provenance, never email bodies/file bytes.

    Attachment names and provider IDs are insufficient to identify file content.
    Empty attachments deliberately have no digest. The shallow mail copy prevents
    evidence_text's local parsing cache from mutating the caller's cached message.
    """
    from studentflow.extraction import evidence_text

    attachment_id = task.get("source_attachment_id")
    attachment = next((item for item in mail.get("attachments", [])
                       if attachment_id is not None and item.get("id") == attachment_id), None)
    quote = _text(task.get("source_quote"))
    try:
        source = evidence_text(dict(mail), attachment_id)
    except (KeyError, TypeError, ValueError, OSError):
        source = ""
    if not quote.strip() or quote not in source:
        quote = ""
    content = (attachment or {}).get("content")
    digest = (hashlib.sha256(content).hexdigest()
              if isinstance(content, (bytes, bytearray)) and content else None)
    expected_hash = task.get("_source_hash")
    actual_hash = digest if attachment_id is not None else body_source_hash(mail)
    matching_source = expected_hash is None or expected_hash == actual_hash
    bound_hash = (actual_hash if isinstance(expected_hash, str)
                  and re.fullmatch(r"[0-9a-f]{64}", expected_hash)
                  and expected_hash == actual_hash else None)
    return _clean_source({
        "mail_id": mail.get("id"), "gmail_account": mail.get("gmail_account"),
        "mail_subject": mail.get("subject"), "mail_sent_at": mail.get("sent_at"),
        "source_attachment_id": attachment_id,
        "source_filename": attachment.get("filename") if attachment else None,
        "source_quote": quote, "attachment_sha256": digest,
        "source_hash": bound_hash,
        "source_key": task.get("_source_key") if matching_source else None,
        "source_item_id": task.get("_source_item_id") if matching_source else None,
    })


def body_source_hash(mail: dict) -> str:
    """Identify exactly the body inputs used by the cached source extraction."""
    payload = [mail.get("subject") or "", mail.get("body") or "", mail.get("sent_at")]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def evidence_for_task(task: dict, mail: dict) -> list[dict]:
    return [source_evidence(task, mail)]


def merge_sources(left: list[dict], right: list[dict]) -> list[dict]:
    """Return distinct, whitelisted evidence in first-seen order."""
    result, seen = [], set()
    for collection in (left, right):
        if not isinstance(collection, list):
            continue
        for source in collection:
            if not isinstance(source, dict):
                continue
            clean = _clean_source(source)
            identity = tuple(clean[key] for key in SOURCE_FIELDS)
            if identity not in seen:
                # Adding canonical IDs to otherwise identical old evidence must
                # not leave two copies of the same source in saved records.
                duplicate = next((item for item in result
                                  if all(item[key] == clean[key] for key in SOURCE_FIELDS
                                         if key not in {"source_hash", "source_key", "source_item_id"})
                                  and (not item["source_hash"] or not clean["source_hash"]
                                       or item["source_hash"] == clean["source_hash"])
                                  and (not item["source_item_id"] or not clean["source_item_id"]
                                       or (item["source_item_id"] == clean["source_item_id"]
                                           and item["source_key"] == clean["source_key"]))), None)
                if duplicate is not None:
                    if not duplicate["source_item_id"] and clean["source_item_id"]:
                        duplicate.update(source_key=clean["source_key"], source_item_id=clean["source_item_id"])
                    if not duplicate["source_hash"] and clean["source_hash"]:
                        duplicate["source_hash"] = clean["source_hash"]
                    seen.add(identity)
                    continue
                seen.add(identity)
                result.append(clean)
    return result


def legacy_source_ids(record: dict, sources: list[dict]) -> list[dict]:
    """Decorate saved legacy evidence with opaque IDs without changing the record."""
    cleaned = merge_sources([], sources)
    identifier = record.get("id")
    if not isinstance(identifier, str) or not identifier:
        return cleaned
    item_id = hashlib.sha256(("saved-task|" + identifier).encode()).hexdigest()
    for source in cleaned:
        if not source.get("source_item_id") and source.get("mail_id"):
            source["source_item_id"] = item_id
            source["source_key"] = hashlib.sha256(("saved-source|" + source["mail_id"]).encode()).hexdigest()
    return cleaned


def _all_sources(task: dict) -> list[dict]:
    sources = merge_sources(task.get("sources", []), [])
    # Legacy saved tasks keep their original provenance on the task itself.
    if not sources:
        sources = [_clean_source(task)]
    return sources


def _sources(task: dict) -> list[dict]:
    return [source for source in _all_sources(task) if _text(source.get("source_quote")).strip()]


def _canonical_sources(task: dict) -> list[dict]:
    """Include quote-less copies only with a locally verified source binding.

    This groups repeated appearances of one cached suggestion. It does not make
    an unsupported quotation valid or authorize saving/semantic matching.
    """
    return [source for source in _all_sources(task)
            if source.get("source_item_id") and source.get("source_key")
            and (_text(source.get("source_quote")).strip()
                 or (source.get("source_hash")
                     and (not source.get("source_attachment_id")
                          or source["source_hash"] == source.get("attachment_sha256"))))]


def _deadline(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return _INVALID_DEADLINE
    try:
        parsed = datetime.fromisoformat(value.strip())
        if parsed.utcoffset() is None:
            return _INVALID_DEADLINE
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return _INVALID_DEADLINE


def _minutes(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _course_codes(quote: str) -> set[str]:
    return course_codes(quote)


def _compatible(left: dict, right: dict) -> bool:
    title = _normalized(left.get("title"), title=True)
    if not title or _title_identity(title) != _title_identity(right.get("title")):
        return False
    left_due, right_due = _deadline(left.get("due_at")), _deadline(right.get("due_at"))
    if left_due is _INVALID_DEADLINE or right_due is _INVALID_DEADLINE or left_due != right_due:
        return False
    left_minutes, right_minutes = _minutes(left.get("estimated_minutes")), _minutes(right.get("estimated_minutes"))
    if left_minutes and right_minutes and left_minutes != right_minutes:
        return False
    # Preserve explicitly labelled courses even when a model chooses a generic title.
    for field in ("course", "course_id", "course_code"):
        a, b = _normalized(left.get(field)), _normalized(right.get(field))
        if a and b and a != b:
            return False
    for field in ("source_quote", "mail_subject"):
        left_courses = set().union(*(_course_codes(_text(source.get(field)))
                                    for source in _sources(left)))
        right_courses = set().union(*(_course_codes(_text(source.get(field)))
                                     for source in _sources(right)))
        if left_courses and right_courses and left_courses.isdisjoint(right_courses):
            return False
    return True


def _evidence_related(left: dict, right: dict, *, missing_due: bool, check_context: bool = True) -> bool:
    account_a, account_b = left.get("gmail_account"), right.get("gmail_account")
    if account_a and account_b and account_a != account_b:
        return False
    same_mail = bool(left.get("mail_id") and left["mail_id"] == right.get("mail_id")
                     and account_a == account_b)
    same_attachment = bool(left.get("attachment_sha256")
                           and left["attachment_sha256"] == right.get("attachment_sha256"))
    if (left.get("source_attachment_id") and right.get("source_attachment_id")
            and left.get("attachment_sha256") and right.get("attachment_sha256")
            and not same_attachment):
        return False
    if not (same_mail or same_attachment):
        return False
    quote_a, quote_b = left["source_quote"], right["source_quote"]
    if check_context:
        for field in ("source_quote", "mail_subject"):
            courses_a = _course_codes(_text(left.get(field)))
            courses_b = _course_codes(_text(right.get(field)))
            if courses_a and courses_b and courses_a.isdisjoint(courses_b):
                return False
    return not missing_due or _normalized(quote_a) == _normalized(quote_b)


def _shared_source_item(left: dict, right: dict) -> bool:
    return any(a.get("source_item_id") and a["source_item_id"] == b.get("source_item_id")
               and a.get("source_key") == b.get("source_key")
               and a.get("gmail_account") == b.get("gmail_account")
               and (not a.get("source_hash") or not b.get("source_hash")
                    or a["source_hash"] == b["source_hash"])
               and _evidence_related(a, b, missing_due=False)
               for a in _canonical_sources(left) for b in _canonical_sources(right))


def same_task(left: dict, right: dict) -> bool:
    """Recognize canonical items independently of user-editable task details.

    Nonempty quotes in supplied sources must already have been validated using
    source_evidence. Legacy stored provenance is accepted as previously reviewed.
    A missing account never proves a same-mail link to a known different scope.
    """
    if _shared_source_item(left, right):
        return True
    left_ids = {source["source_item_id"] for source in _canonical_sources(left)}
    right_ids = {source["source_item_id"] for source in _canonical_sources(right)}
    # A new canonical item requires an explicit semantic alias to an old one.
    # Labels, dates, or quotations cannot override a distinct item identity.
    if left_ids and right_ids:
        return False
    if not _compatible(left, right):
        return False
    missing_due = _deadline(left.get("due_at")) is None
    return any(_evidence_related(a, b, missing_due=missing_due)
               for a in _sources(left) for b in _sources(right))


def _merge_candidate(kept: dict, candidate: dict) -> None:
    kept["sources"] = merge_sources(kept["sources"], candidate["sources"])
    notes = []
    for member in (kept, candidate):
        for note in member.get("needs_clarification", []):
            if isinstance(note, str) and note not in notes:
                notes.append(note)
    kept["needs_clarification"] = notes
    kept["source_duplicate_count"] += candidate["source_duplicate_count"]


def merge_duplicate_tasks(tasks: list[dict]) -> list[dict]:
    """Collapse reliable duplicates without changing primary candidate details.

    Keep all provenance and clarification notes. Check identity compatibility
    against every original member, so an unknown effort cannot bridge conflicting
    known estimates. Do not modify cached proposals or messages passed in tasks.
    """
    results, groups = [], []
    for original in tasks:
        candidate = deepcopy(original)
        candidate["sources"] = merge_sources(candidate.get("sources", []), [])
        count = candidate.get("source_duplicate_count", 1)
        candidate["source_duplicate_count"] = count if type(count) is int and count > 0 else 1
        for index, kept in enumerate(results):
            if (same_task(kept, candidate) and (
                    _shared_source_item(kept, candidate)
                    or all(_compatible(member, candidate) for member in groups[index]))):
                _merge_candidate(kept, candidate)
                groups[index].append(candidate)
                break
        else:
            results.append(candidate)
            groups.append([deepcopy(candidate)])
    # A validated alias may arrive after both generations were added. Close only
    # explicit canonical links; never create transitive links from title guesses.
    for index, kept in enumerate(results):
        other = index + 1
        while other < len(results):
            if _shared_source_item(kept, results[other]):
                _merge_candidate(kept, results.pop(other))
                other = index + 1
            else:
                other += 1
    return results
