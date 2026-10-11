"""Persistent, account-scoped source extraction results and stable item IDs."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

CACHE_SCHEMA_VERSION = "studentflow.source-extraction.v1"
_TEXT_FIELDS = {
    "title", "due_at", "source_quote", "source_attachment_id", "source_filename", "status",
}
_INTEGER_FIELDS = {"estimated_minutes", "source_page"}
_DEADLINE_BASES = {"absolute", "message_relative", "unknown"}


def source_cache_key(
    kind: str,
    account: str | None,
    content_hash: str,
    model: str,
    context=None,
    *,
    schema_version: str = CACHE_SCHEMA_VERSION,
) -> str:
    """Hash content identity plus every input that can change its interpretation."""
    scope = {
        "schema": schema_version,
        "kind": kind,
        "account": (account or "").strip().casefold(),
        "content_hash": content_hash,
        "model": model.strip(),
        "context": context,
    }
    payload = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sanitize_result(result: dict) -> dict:
    if (not isinstance(result, dict) or not isinstance(result.get("tasks"), list)
            or not isinstance(result.get("warnings", []), list)):
        raise TypeError("The extraction result must contain task and warning lists.")
    tasks = []
    for task in result["tasks"]:
        if not isinstance(task, dict) or not isinstance(task.get("title"), str) or not task["title"].strip():
            raise ValueError("Cached tasks must have a valid task name.")
        clean = {key: task[key] for key in _TEXT_FIELDS
                 if key in task and (task[key] is None or isinstance(task[key], str))}
        clean.update({key: task[key] for key in _INTEGER_FIELDS
                      if key in task and (task[key] is None or type(task[key]) is int)})
        deadline_basis = task.get("deadline_basis")
        if isinstance(deadline_basis, str) and deadline_basis in _DEADLINE_BASES:
            clean["deadline_basis"] = deadline_basis
        source_hash = task.get("_source_hash")
        if isinstance(source_hash, str) and re.fullmatch(r"[0-9a-f]{64}", source_hash):
            clean["_source_hash"] = source_hash
        confidence = task.get("confidence")
        if type(confidence) in {int, float} and math.isfinite(confidence):
            clean["confidence"] = confidence
        clean["needs_clarification"] = [item for item in task.get("needs_clarification", [])
                                        if isinstance(item, str)] if isinstance(task.get("needs_clarification", []), list) else []
        if isinstance(task.get("_source_key"), str) and task["_source_key"]:
            clean["_source_key"] = task["_source_key"]
        item_id = task.get("_source_item_id")
        if isinstance(item_id, str) and re.fullmatch(r"[0-9a-f]{64}", item_id):
            clean["_source_item_id"] = item_id
        tasks.append(clean)
    return {"tasks": tasks, "warnings": [item for item in result.get("warnings", []) if isinstance(item, str)]}


def attach_item_ids(key: str, result: dict, *, generation: str | None = None) -> dict:
    """Return a copied result with fresh opaque IDs for this analysis generation."""
    clean = _sanitize_result(result)
    generation = generation or uuid4().hex
    for index, task in enumerate(clean["tasks"]):
        identity = json.dumps([key, generation, index], separators=(",", ":"))
        task["_source_key"] = key
        task["_source_item_id"] = hashlib.sha256(identity.encode()).hexdigest()
    return clean


class ExtractionCache:
    """Keep local validated proposals without retaining raw email or API settings."""

    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS source_extraction_cache ("
                       "cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=15)

    def get(self, key: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM source_extraction_cache WHERE cache_key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            result = _sanitize_result(json.loads(row[0]))
        except (TypeError, ValueError, OverflowError):
            return None
        identifiers = [task.get("_source_item_id") for task in result["tasks"]]
        if any(task.get("_source_key") != key for task in result["tasks"]):
            return None
        if None in identifiers or len(set(identifiers)) != len(identifiers):
            return None
        return result

    def put(self, key: str, result: dict) -> None:
        """Atomically replace one analysis; a fresh result receives fresh item IDs."""
        clean = _sanitize_result(deepcopy(result))
        identifiers = [task.get("_source_item_id") for task in clean["tasks"]]
        has_ids = (all(task.get("_source_key") == key for task in clean["tasks"])
                   and None not in identifiers and len(set(identifiers)) == len(identifiers))
        if not has_ids:
            clean = attach_item_ids(key, clean)
        payload = json.dumps(clean, ensure_ascii=False, allow_nan=False)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO source_extraction_cache VALUES (?, ?) "
                       "ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload", (key, payload))
