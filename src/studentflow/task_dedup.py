"""Conservative, source-bound duplicate detection for reviewed task proposals.

Only exact normalized titles and compatible deadlines/effort are candidates.
Mail identity or the bytes of a shared attachment must also connect the evidence.
No model calls or persistent changes are made here.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from copy import deepcopy
from datetime import UTC, datetime

SOURCE_FIELDS = (
    "mail_id", "gmail_account", "mail_subject", "mail_sent_at",
    "source_attachment_id", "source_filename", "source_quote", "attachment_sha256",
)
_COURSE_CODE = re.compile(r"\b([A-Za-z]{2,8})[ -]?(\d{3,5}[A-Za-z]?)\b")
_NON_COURSE_PREFIXES = {
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "year", "page",
}
_INVALID_DEADLINE = object()


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _normalized(value, *, title=False) -> str:
    result = " ".join(unicodedata.normalize("NFKC", _text(value)).casefold().split())
    # Do not strip internal punctuation, digits, course names, or action words.
    return result.rstrip(" .!?。！？;；:") if title else result


def _clean_source(source: dict) -> dict:
    result = {key: source.get(key) if isinstance(source.get(key), str) else None
              for key in SOURCE_FIELDS}
    result["gmail_account"] = _text(result["gmail_account"]).strip().casefold() or None
    digest = _text(result["attachment_sha256"]).strip().lower()
    result["attachment_sha256"] = digest if re.fullmatch(r"[0-9a-f]{64}", digest) else None
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
    return _clean_source({
        "mail_id": mail.get("id"), "gmail_account": mail.get("gmail_account"),
        "mail_subject": mail.get("subject"), "mail_sent_at": mail.get("sent_at"),
        "source_attachment_id": attachment_id,
        "source_filename": attachment.get("filename") if attachment else None,
        "source_quote": quote, "attachment_sha256": digest,
    })


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
                seen.add(identity)
                result.append(clean)
    return result


def _sources(task: dict) -> list[dict]:
    sources = merge_sources(task.get("sources", []), [])
    # Legacy saved tasks keep their original provenance on the task itself.
    if not sources:
        sources = [_clean_source(task)]
    return [source for source in sources if _text(source.get("source_quote")).strip()]


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
    return {(prefix + number).casefold() for prefix, number in _COURSE_CODE.findall(quote)
            if prefix.casefold() not in _NON_COURSE_PREFIXES}


def _compatible(left: dict, right: dict) -> bool:
    title = _normalized(left.get("title"), title=True)
    if not title or title != _normalized(right.get("title"), title=True):
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


def _evidence_related(left: dict, right: dict, *, missing_due: bool) -> bool:
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
    for field in ("source_quote", "mail_subject"):
        courses_a = _course_codes(_text(left.get(field)))
        courses_b = _course_codes(_text(right.get(field)))
        if courses_a and courses_b and courses_a.isdisjoint(courses_b):
            return False
    return not missing_due or _normalized(quote_a) == _normalized(quote_b)


def same_task(left: dict, right: dict) -> bool:
    """Whether two task identities have compatible details and linked evidence.

    Nonempty quotes in supplied sources must already have been validated using
    source_evidence. Legacy stored provenance is accepted as previously reviewed.
    A missing account never proves a same-mail link to a known different scope.
    """
    if not _compatible(left, right):
        return False
    missing_due = _deadline(left.get("due_at")) is None
    return any(_evidence_related(a, b, missing_due=missing_due)
               for a in _sources(left) for b in _sources(right))


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
            if (all(_compatible(member, candidate) for member in groups[index])
                    and same_task(kept, candidate)):
                kept["sources"] = merge_sources(kept["sources"], candidate["sources"])
                notes = []
                for member in (kept, candidate):
                    for note in member.get("needs_clarification", []):
                        if isinstance(note, str) and note not in notes:
                            notes.append(note)
                kept["needs_clarification"] = notes
                kept["source_duplicate_count"] += candidate["source_duplicate_count"]
                groups[index].append(candidate)
                break
        else:
            results.append(candidate)
            groups.append([deepcopy(candidate)])
    return results
