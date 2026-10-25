"""Persistent source-bound fixed-event proposals for the unified inbox analysis."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from pathlib import Path

from studentflow import fixed_events
from studentflow.extraction import _aware_iso, attachment_fingerprint

CACHE_SCHEMA_VERSION = "studentflow.fixed-event-extraction.v1"
_EVENT_TEXT = {"title", "event_key", "kind", "course", "account", "location", "status"}
_IDENTITY_FIELDS = {"title", "event_key", "course", "kind", "start_at", "end_at",
                    "location", "in_person"}
_SOURCE_TEXT = {"account", "mail_id", "mail_subject", "source_attachment_id",
                "source_filename", "attachment_sha256", "source_quote"}
_FAILURE_MESSAGE = (
    "Fixed-event analysis failed for this message. Check AI access and try again. "
    "No fixed events were saved."
)
_CACHE_WARNING = (
    "The local analysis cache was unavailable. Suggestions are still available for review; "
    "repeating analysis may call AI again."
)


def fixed_event_cache_key(account: str, mail: dict, model: str) -> str:
    """Bind every input affecting interpretation, without storing raw message text."""
    scope = {
        "schema": CACHE_SCHEMA_VERSION,
        "version": fixed_events.FIXED_EVENTS_VERSION,
        "prompt": fixed_events.SYSTEM_PROMPT,
        "account": fixed_events.normalize_account(account),
        "model": model.strip(),
        "mail_id": mail.get("id"),
        "body": mail.get("body") or "",
        "subject": mail.get("subject") or "",
        "sent_at": mail.get("sent_at"),
        "attachments": attachment_fingerprint(mail.get("attachments", [])),
    }
    serialized = json.dumps(scope, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _validated_result(result: dict) -> dict:
    """Copy only proposal fields; malformed persisted data is never trusted."""
    if (not isinstance(result, dict) or not isinstance(result.get("events"), list)
            or not isinstance(result.get("warnings"), list)
            or any(not isinstance(value, str) for value in result["warnings"])):
        raise ValueError("The fixed-event result must contain event and warning lists.")
    events = []
    for raw in result["events"]:
        if (not isinstance(raw, dict) or not isinstance(raw.get("title"), str)
                or not raw["title"].strip() or raw.get("kind") not in fixed_events.KINDS
                or raw.get("status") != "needs_review"):
            raise ValueError("Fixed events must remain valid review proposals.")
        account = fixed_events.normalize_account(raw.get("account"))
        if raw.get("account") != account:
            raise ValueError("Fixed-event account identity is invalid.")
        clean = {}
        for key in _EVENT_TEXT:
            value = raw.get(key)
            if not isinstance(value, str):
                raise TypeError("Fixed-event text fields must contain text.")
            clean[key] = value
        for key in ("start_at", "end_at"):
            value = raw.get(key)
            if value is not None and _aware_iso(value) is None:
                raise ValueError("Fixed-event times must include a timezone.")
            clean[key] = value
        duration = raw.get("duration_minutes")
        if duration is not None and (type(duration) is not int or not 0 < duration <= 10080):
            raise ValueError("Invalid fixed-event duration.")
        travel = raw.get("travel_minutes")
        if type(travel) is not int or travel != 0:
            raise ValueError("Extracted events cannot invent travel time.")
        attendance = raw.get("in_person")
        if attendance is not None and type(attendance) is not bool:
            raise ValueError("Invalid attendance mode.")
        notes = raw.get("needs_clarification")
        if not isinstance(notes, list) or any(not isinstance(note, str) for note in notes):
            raise ValueError("Invalid event review notes.")
        identity = raw.get("_extraction_identity")
        if (not isinstance(identity, dict)
                or not _IDENTITY_FIELDS.issubset(identity)
                or any(identity[key] != raw.get(key) for key in _IDENTITY_FIELDS)):
            raise ValueError("The extraction identity must match the original proposal.")
        refs = raw.get("sources")
        if not isinstance(refs, list) or not refs:
            raise ValueError("Fixed-event proposals require source evidence.")
        sources = []
        for ref in refs:
            if not isinstance(ref, dict):
                raise TypeError("Invalid fixed-event evidence.")
            source = {key: ref.get(key) for key in _SOURCE_TEXT}
            if any(value is not None and not isinstance(value, str) for value in source.values()):
                raise ValueError("Invalid fixed-event source fields.")
            if (source["account"] != account or not source["mail_id"]
                    or not source["source_quote"] or not source["source_quote"].strip()):
                raise ValueError("Fixed-event evidence must match its account and message.")
            page = ref.get("source_page")
            if page is not None and (type(page) is not int or page < 1):
                raise ValueError("Invalid attachment page.")
            source["source_page"] = page
            sources.append(source)
        clean.update(duration_minutes=duration, travel_minutes=travel, in_person=attendance,
                     needs_clarification=list(notes), sources=sources,
                     _extraction_identity={key: identity[key] for key in _IDENTITY_FIELDS})
        events.append(clean)
    return {"events": events, "warnings": list(result["warnings"])}


def _matches_sources(result: dict, sources: list[dict], account: str, mail_id: str) -> bool:
    for event in result["events"]:
        if event["account"] != account:
            return False
        for ref in event["sources"]:
            if ref["account"] != account or ref["mail_id"] != mail_id:
                return False
            match = next((source for source in sources if all(
                source.get(key) == ref.get(key) for key in (
                    "account", "mail_id", "mail_subject", "source_attachment_id",
                    "source_filename", "attachment_sha256",
                )
            ) and ref["source_quote"] in source["text"]), None)
            if match is None:
                return False
            if ref.get("source_page") is not None and not any(
                page.get("page") == ref["source_page"]
                and ref["source_quote"] in page.get("text", "")
                for page in match.get("pages", [])
            ):
                return False
    return True


class FixedEventCache:
    """A separate table of successful, unsaved extraction proposals."""

    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS fixed_event_extraction_cache ("
                       "cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=15)

    def get(self, key: str) -> dict | None:
        with closing(self.connect()) as db, db:
            row = db.execute("SELECT payload FROM fixed_event_extraction_cache WHERE cache_key=?",
                             (key,)).fetchone()
        if row is None:
            return None
        try:
            return _validated_result(json.loads(row[0]))
        except (TypeError, ValueError, OverflowError):
            return None

    def put(self, key: str, result: dict) -> None:
        clean = _validated_result(result)
        payload = json.dumps(clean, ensure_ascii=False, allow_nan=False)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO fixed_event_extraction_cache VALUES (?, ?) "
                       "ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload", (key, payload))


def analyze_fixed_event_messages(
    mails: list[dict], account: str, api_key: str, model: str, cache: FixedEventCache | None,
    *, reanalyze: bool = False, extractor=None,
) -> dict:
    """Analyze selected mail, reusing successes and exposing safe per-message failures."""
    account = fixed_events.normalize_account(account)
    extractor = extractor or fixed_events.extract_fixed_events
    results, events, warnings = [], [], []
    for mail in mails:
        mail_id = str(mail.get("id") or "")
        try:
            sources, _ = fixed_events.prepare_sources(mail, account)
            if not sources:
                raise ValueError("No readable source is available.")
            key = fixed_event_cache_key(account, mail, model)
            cache_warnings = [_CACHE_WARNING] if cache is None else []
            cached = None
            if cache is not None and not reanalyze:
                try:
                    cached = cache.get(key)
                except Exception:  # noqa: BLE001 - Cache failure must not discard service results.
                    cache_warnings.append(_CACHE_WARNING)
            if cached is not None and not _matches_sources(cached, sources, account, mail_id):
                cached = None
            if cached is None:
                result = _validated_result(extractor(mail, api_key, model, account=account))
                if not _matches_sources(result, sources, account, mail_id):
                    raise ValueError("Fixed-event evidence did not match the selected message.")
                if cache is not None:
                    try:
                        cache.put(key, result)
                    except Exception:  # noqa: BLE001 - Cache failure must not discard service results.
                        cache_warnings.append(_CACHE_WARNING)
            else:
                result = cached
            result["warnings"] = list(dict.fromkeys(result["warnings"] + cache_warnings))
            results.append({"mail_id": mail_id, **deepcopy(result), "cached": cached is not None})
            events.extend(result["events"])
            warnings.extend(result["warnings"])
        except Exception:  # noqa: BLE001 - Provider/source exceptions may contain private content.
            results.append({"mail_id": mail_id, "error": _FAILURE_MESSAGE, "cached": False})
    return {"events": fixed_events.merge_fixed_event_proposals(events),
            "warnings": list(dict.fromkeys(warnings)), "results": results,
            "complete": all("error" not in result for result in results)}
