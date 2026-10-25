"""Cache a complete, explicitly requested inbox analysis without credentials."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from studentflow.effort_batch import VERSION as EFFORT_VERSION
from studentflow.extraction import SYSTEM_PROMPT, attachment_fingerprint
from studentflow.fixed_events import FIXED_EVENTS_VERSION
from studentflow.fixed_events import SYSTEM_PROMPT as FIXED_EVENTS_PROMPT
from studentflow.source_extraction import PIPELINE_VERSION
from studentflow.task_reconciliation import RECONCILIATION_VERSION

UNIFIED_VERSION = "unified-inbox-v1"
_RESULT_FIELDS = ("status", "task_result", "event_result", "events", "usage", "warnings")
_CREDENTIAL_FIELDS = {
    "api_key", "apikey", "access_token", "refresh_token", "id_token", "token",
    "client_secret", "authorization", "credentials", "credential_path", "token_path",
}


def analysis_cache_key(account, mails, model, references, suggest_effort):
    # Read the effort contract only during explicit analysis, not while rendering the inbox.
    from studentflow.effort_estimation import _INSTRUCTIONS as effort_prompt

    payload = [UNIFIED_VERSION, PIPELINE_VERSION, RECONCILIATION_VERSION,
               FIXED_EVENTS_VERSION, EFFORT_VERSION, SYSTEM_PROMPT, FIXED_EVENTS_PROMPT,
               effort_prompt if suggest_effort else None, account.strip().casefold(), model,
               references, suggest_effort,
               [(mail["id"], mail.get("gmail_id"), mail.get("body"), mail.get("subject"),
                 mail.get("sent_at"), attachment_fingerprint(mail.get("attachments", [])))
                for mail in mails]]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _without_credentials(value):
    if isinstance(value, dict):
        return {key: _without_credentials(item) for key, item in value.items()
                if not isinstance(key, str) or key.casefold() not in _CREDENTIAL_FIELDS}
    if isinstance(value, list):
        return [_without_credentials(item) for item in value]
    return value


def _clean_result(result):
    if not isinstance(result, dict):
        raise TypeError("Inbox analysis must be an object.")
    clean = _without_credentials({field: result.get(field) for field in _RESULT_FIELDS})
    tasks, events = clean["task_result"], clean["event_result"]
    if (clean["status"] != "completed" or not isinstance(tasks, dict)
            or tasks.get("status") != "completed" or not isinstance(tasks.get("results"), list)
            or not isinstance(tasks.get("effort"), dict) or not isinstance(events, dict)
            or not isinstance(events.get("events"), list) or events.get("complete") is not True
            or events.get("status", "completed") != "completed"
            or not isinstance(clean["events"], list) or not isinstance(clean["usage"], dict)
            or not isinstance(clean["warnings"], list)):
        raise ValueError("Only complete inbox analyses can be cached.")
    for entry in tasks["results"]:
        if (not isinstance(entry, dict) or "error" in entry or entry.get("partial")
                or not isinstance(entry.get("result"), dict)
                or not isinstance(entry["result"].get("tasks"), list)):
            raise ValueError("A partial task analysis cannot be cached as complete.")
    event_results = events.get("results", [])
    if not isinstance(event_results, list) or any(
        not isinstance(entry, dict) or "error" in entry for entry in event_results
    ):
        raise ValueError("A partial fixed-event analysis cannot be cached as complete.")
    return clean


class InboxAnalysisCache:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS inbox_analysis_cache "
                       "(cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    def get(self, key):
        with closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute("SELECT payload FROM inbox_analysis_cache WHERE cache_key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            return _clean_result(json.loads(row[0]))
        except (ValueError, TypeError, KeyError, AttributeError):
            return None

    def put(self, key, result):
        # Only these result fields are persisted, never callback objects/API settings.
        clean = _clean_result(result)
        encoded = json.dumps(clean, ensure_ascii=False, allow_nan=False)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT OR REPLACE INTO inbox_analysis_cache VALUES (?, ?)", (key, encoded))
