"""Local confirmed records. No mailbox or calendar side effects."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from studentflow.effort_review import build_effort_review, validate_effort_review
from studentflow.task_dedup import legacy_source_ids, merge_sources, same_task


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS daily_reviews (day TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS deleted_task_ids (task_id TEXT PRIMARY KEY)")
            db.execute("CREATE TABLE IF NOT EXISTS fixed_events (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=15)

    @staticmethod
    def _task(record: dict) -> dict:
        """Decorate legacy records without rewriting a user's existing database."""
        result = dict(record)
        estimate = result.get("estimated_minutes")
        known = isinstance(estimate, int) and not isinstance(estimate, bool) and estimate > 0
        completed = result.get("completed_minutes", 0)
        if isinstance(completed, bool) or not isinstance(completed, int) or completed < 0:
            completed = 0
        result["completed_minutes"] = completed
        result["remaining_minutes"] = max(0, estimate - completed) if known else None
        result["is_completed"] = bool(known and completed >= estimate)
        return result

    def save_task(self, task: dict) -> str:
        return self.save_tasks([task])[0]

    @staticmethod
    def _evidence(record: dict, db) -> list[dict]:
        """Recover legacy evidence without treating an attachment ID as a hash."""
        sources = merge_sources([], record.get("sources", []))
        if not sources:
            sources = merge_sources([], [record])
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachment_documents'"
        ).fetchone():
            return legacy_source_ids(record, sources)
        recovered = []
        for source in sources:
            source = dict(source)
            attachment_id = source.get("source_attachment_id")
            mail_id = source.get("mail_id")
            if attachment_id and mail_id and not source.get("attachment_sha256"):
                rows = db.execute(
                    "SELECT payload, content FROM attachment_documents "
                    "WHERE json_extract(payload, '$.mail_id')=? AND json_extract(payload, '$.id')=?",
                    (mail_id, attachment_id),
                ).fetchall()
                account = str(source.get("gmail_account") or "").strip().casefold()
                candidates = []
                for payload, content in rows:
                    metadata = json.loads(payload)
                    saved_account = str(metadata.get("gmail_account") or "").strip().casefold()
                    if account and saved_account != account:
                        continue
                    digest = hashlib.sha256(bytes(content)).hexdigest() if content else None
                    candidates.append((saved_account, digest))
                # Older tasks omit the account. Recover it only when all exact
                # library matches agree on one known account and nonempty file.
                identities = set(candidates)
                if len(identities) == 1:
                    saved_account, digest = identities.pop()
                    if digest and (account or saved_account):
                        source["attachment_sha256"] = digest
                        source["gmail_account"] = account or saved_account
            recovered.append(source)
        return legacy_source_ids(record, recovered)

    @staticmethod
    def _legacy_body_scope(previous: dict, sources: list[dict], incoming: dict) -> list[dict]:
        """Bind old Gmail body evidence only to the same raw message and quote."""
        if previous.get("sources") or previous.get("gmail_account") or previous.get("source_name") != "Gmail":
            return sources
        recovered = []
        for source in sources:
            source = dict(source)
            mail_id, quote = source.get("mail_id"), source.get("source_quote")
            if (not source.get("gmail_account") and not source.get("source_attachment_id")
                    and isinstance(mail_id, str) and re.fullmatch(r"[0-9a-f]{64}", mail_id)
                    and isinstance(quote, str) and quote.strip()):
                accounts = {
                    item.get("gmail_account") for item in incoming.get("sources", [])
                    if item.get("mail_id") == mail_id and not item.get("source_attachment_id")
                    and item.get("source_quote") == quote
                }
                if len(accounts) == 1 and None not in accounts and "" not in accounts:
                    source["gmail_account"] = accounts.pop()
            recovered.append(source)
        return merge_sources([], recovered)

    @classmethod
    def _same_reviewed_task(cls, incoming: dict, previous: dict, sources: list[dict]) -> bool:
        sources = cls._legacy_body_scope(previous, sources, incoming)
        saved = dict(previous, sources=sources)
        if same_task(incoming, saved):
            return True
        # The original reviewed identity is immutable. Re-extraction should not
        # undo a user's later title, deadline, estimate, or progress edits.
        original = previous.get("_extraction_identity")
        return isinstance(original, dict) and same_task(incoming, saved | {
            key: original.get(key) for key in ("title", "due_at", "estimated_minutes")
        })

    def save_tasks(self, tasks: list[dict]) -> list[str]:
        """Save atomically, merging proven repeat extractions without undoing edits."""
        updated_at = datetime.now(UTC).isoformat()
        identifiers = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = {row[0]: json.loads(row[1]) for row in db.execute("SELECT id, payload FROM tasks")}
            deleted = {row[0] for row in db.execute("SELECT task_id FROM deleted_task_ids")}
            evidence_cache = {}
            for task in tasks:
                record = dict(task)
                title = str(record.get("title", "")).strip()
                if not title:
                    raise ValueError("Enter a task name.")
                record["title"] = title
                if "effort_review" in record:
                    record["effort_review"] = validate_effort_review(
                        record["effort_review"], record.get("estimated_minutes")
                    )
                    record["estimated_minutes"] = record["effort_review"]["confirmed_minutes"]
                reviewed = bool(merge_sources([], record.get("sources", [])))
                if reviewed:
                    record["sources"] = self._evidence(record, db)
                    for key, value in existing.items():
                        if key not in evidence_cache:
                            evidence_cache[key] = self._evidence(value, db)
                    match = next((key for key, value in existing.items()
                                  if self._same_reviewed_task(record, value, evidence_cache[key])), None)
                    if match is not None:
                        previous = existing[match]
                        matched_sources = self._legacy_body_scope(previous, evidence_cache[match], record)
                        merged = merge_sources(matched_sources, record["sources"])
                        if previous.get("sources") != merged or "_extraction_identity" not in previous:
                            previous = dict(previous, sources=merged, updated_at=updated_at)
                            previous.setdefault("_extraction_identity", {
                                key: existing[match].get(key)
                                for key in ("title", "due_at", "estimated_minutes")
                            })
                            db.execute("UPDATE tasks SET payload=? WHERE id=?",
                                       (json.dumps(previous, ensure_ascii=False), match))
                            existing[match] = previous
                            evidence_cache[match] = merged
                        identifiers.append(match)
                        continue
                identity = (str(record.get("mail_id", "manual")), title)
                record_id = None if reviewed else next((key for key, value in existing.items()
                                                        if (str(value.get("mail_id", "manual")), value["title"]) == identity), None)
                record_id = record_id or hashlib.sha256("|".join(identity).encode()).hexdigest()
                # A renamed task retains its ID; a new proposal using its old name
                # must not silently overwrite that user's edit.
                while record_id in deleted or (record_id in existing and (reviewed or (
                    str(existing[record_id].get("mail_id", "manual")), existing[record_id]["title"]
                ) != identity)):
                    record_id = hashlib.sha256((record_id + "|new").encode()).hexdigest()
                previous = existing.get(record_id, {})
                record = previous | record
                if previous and "effort_review" in record:
                    # Re-saving an extraction is not another human effort review.
                    # Existing estimates may only change through an explicit edit.
                    record["estimated_minutes"] = previous.get("estimated_minutes")
                    if "effort_review" in previous:
                        record["effort_review"] = previous["effort_review"]
                    else:
                        record.pop("effort_review", None)
                    record["status"] = previous.get("status", "pending")
                if reviewed:
                    record["_extraction_identity"] = {
                        key: record.get(key) for key in ("title", "due_at", "estimated_minutes")
                    }
                # Re-extraction and repeated saves must not reset completed work.
                record["completed_minutes"] = previous.get("completed_minutes", record.get("completed_minutes", 0))
                if record["completed_minutes"] and (record.get("estimated_minutes") or 0) < record["completed_minutes"]:
                    raise ValueError("The estimate cannot be less than the time already completed. Edit the task in Tasks.")
                record.update(id=record_id, updated_at=updated_at)
                if reviewed:
                    record["sources"] = legacy_source_ids(record, record["sources"])
                record = self._task(record)
                db.execute(
                    "INSERT INTO tasks VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                    (record_id, json.dumps(record, ensure_ascii=False)),
                )
                existing[record_id] = record
                evidence_cache.pop(record_id, None)
                identifiers.append(record_id)
        return identifiers

    def tasks(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM tasks ORDER BY rowid").fetchall()
        return [self._task(json.loads(row[0])) for row in rows]

    def task_references(self) -> list[dict]:
        """Read task details with stable source IDs for semantic reconciliation."""
        with self.connect() as db:
            records = [self._task(json.loads(row[0]))
                       for row in db.execute("SELECT payload FROM tasks ORDER BY rowid")]
            return [dict(record, sources=self._evidence(record, db)) for record in records]

    def update_task(self, task_id: str, changes: dict, *, expected_updated_at=None) -> dict:
        """Edit a saved task by ID, with optimistic concurrency and preserved evidence."""
        allowed = {"title", "due_at", "estimated_minutes", "completed_minutes"}
        if set(changes) - allowed:
            raise ValueError("Only task details and study progress can be edited.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("This task is no longer available. Refresh the task list.")
            previous = json.loads(row[0])
            if expected_updated_at is not None and previous.get("updated_at") != expected_updated_at:
                raise ValueError("This task changed in another view. Refresh and review the latest details.")
            record = previous | changes
            title = str(record.get("title", "")).strip()
            if not title:
                raise ValueError("Enter a task name.")
            record["title"] = title
            estimate = record.get("estimated_minutes")
            if estimate is not None and (isinstance(estimate, bool) or not isinstance(estimate, int) or not 0 <= estimate <= 10000):
                raise ValueError("Estimated study time must be a whole number from 0 to 10000.")
            estimate = estimate or None
            completed = record.get("completed_minutes", 0)
            if isinstance(completed, bool) or not isinstance(completed, int) or completed < 0:
                raise ValueError("Completed study time must be a nonnegative whole number.")
            if completed > (estimate or 0):
                raise ValueError("Completed study time cannot exceed the total estimate.")
            due = record.get("due_at") or None
            if due:
                try:
                    parsed = datetime.fromisoformat(due)
                    if parsed.utcoffset() is None:
                        raise ValueError
                except (ValueError, TypeError):
                    raise ValueError("Enter a valid deadline with a timezone.") from None
            for other_id, payload in db.execute("SELECT id, payload FROM tasks WHERE id<>?", (task_id,)):
                other = json.loads(payload)
                if str(other.get("mail_id", "manual")) == str(record.get("mail_id", "manual")) and other["title"] == title:
                    raise ValueError("Another task from this message has this name. Choose a different name.")
            record.update(estimated_minutes=estimate, completed_minutes=completed, due_at=due,
                          status="confirmed" if due and estimate else "pending",
                          updated_at=datetime.now(UTC).isoformat())
            if ("estimated_minutes" in changes and "effort_review" in previous
                    and estimate != previous.get("estimated_minutes")):
                review = previous["effort_review"]
                record["effort_review"] = build_effort_review(
                    review, estimate, model=review.get("model", "")
                )
            record = self._task(record)
            db.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(record, ensure_ascii=False), task_id))
        return record

    def list_fixed_events(self, account: str | None = None) -> list[dict]:
        """Read local human-confirmed appointments without calendar side effects."""
        from studentflow.fixed_events import normalize_account

        selected = normalize_account(account) if account is not None else None
        with self.connect() as db:
            records = [json.loads(row[0]) for row in db.execute("SELECT payload FROM fixed_events")]
        return sorted((record for record in records if selected is None or record["account"] == selected),
                      key=lambda record: (record["start_at"], record["id"]))

    def save_fixed_events(self, events: list[dict]) -> list[str]:
        """Save reviewed proposals atomically; repeat extraction keeps human edits."""
        from uuid import uuid4

        from studentflow.fixed_events import same_reviewed_event, validate_confirmed_event

        checked = [validate_confirmed_event(event) for event in events]
        identifiers = []
        stamp = datetime.now(UTC).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            records = [json.loads(row[0]) for row in db.execute("SELECT payload FROM fixed_events")]
            for event in checked:
                previous = next((record for record in records if same_reviewed_event(event, record)), None)
                if previous is not None:
                    for source in event["sources"]:
                        if source not in previous["sources"]:
                            previous["sources"].append(source)
                    # Preserve current title, start, end, location and commute.
                    db.execute("UPDATE fixed_events SET payload=? WHERE id=?",
                               (json.dumps(previous, ensure_ascii=False), previous["id"]))
                    identifiers.append(previous["id"])
                    continue
                record = dict(event, id=uuid4().hex, created_at=stamp, updated_at=stamp)
                record["_extraction_identity"] = event.get("_extraction_identity") or {
                    key: event.get(key) for key in (
                        "title", "event_key", "course", "kind", "start_at", "end_at", "location", "in_person",
                    )
                }
                db.execute("INSERT INTO fixed_events VALUES (?, ?)",
                           (record["id"], json.dumps(record, ensure_ascii=False)))
                records.append(record)
                identifiers.append(record["id"])
        return identifiers

    def update_fixed_event(self, identifier: str, changes: dict, *, account: str) -> dict:
        from studentflow.fixed_events import normalize_account, validate_confirmed_event

        selected = normalize_account(account)
        allowed = {"title", "start_at", "end_at", "location", "in_person", "travel_minutes", "kind"}
        if set(changes) - allowed:
            raise ValueError("Only event details can be changed here.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM fixed_events WHERE id=?", (identifier,)).fetchone()
            record = json.loads(row[0]) if row else None
            if not record or record["account"] != selected:
                raise ValueError("This event is not available for the selected account.")
            updated = validate_confirmed_event(record | changes)
            updated["updated_at"] = datetime.now(UTC).isoformat()
            db.execute("UPDATE fixed_events SET payload=? WHERE id=?",
                       (json.dumps(updated, ensure_ascii=False), identifier))
        return updated

    def delete_fixed_event(self, identifier: str, *, account: str) -> None:
        from studentflow.fixed_events import normalize_account

        selected = normalize_account(account)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM fixed_events WHERE id=?", (identifier,)).fetchone()
            if row is None or json.loads(row[0])["account"] != selected:
                raise ValueError("This event is not available for the selected account.")
            db.execute("DELETE FROM fixed_events WHERE id=?", (identifier,))

    def review_task_effort(self, task_id: str, final_minutes: int | None, suggestion: dict,
                           *, expected_updated_at: str) -> dict:
        """Atomically apply one person's estimate review without changing evidence or progress."""
        if not isinstance(expected_updated_at, str) or not expected_updated_at:
            raise ValueError("Refresh the task before reviewing its study-time estimate.")
        if not isinstance(suggestion, dict):
            raise ValueError("The study-time suggestion must be a record.")  # noqa: TRY004
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("This task is no longer available. Refresh the task list.")
            previous = json.loads(row[0])
            if previous.get("updated_at") != expected_updated_at:
                raise ValueError("This task changed in another view. Refresh and review the latest details.")
            advice = dict(suggestion)
            # Source-stated effort is evidence, not a later model's new guess.
            if isinstance(previous.get("effort_review"), dict):
                advice["stated_minutes"] = previous["effort_review"].get("stated_minutes")
            review = build_effort_review(advice, final_minutes, model=advice.get("model", ""))
            estimate = review["confirmed_minutes"]
            if previous.get("completed_minutes", 0) > (estimate or 0):
                raise ValueError("The estimate cannot be less than the time already completed.")
            record = previous | {
                "estimated_minutes": estimate,
                "effort_review": review,
                "status": "confirmed" if previous.get("due_at") and estimate else "pending",
                "updated_at": datetime.now(UTC).isoformat(),
            }
            record = self._task(record)
            db.execute("UPDATE tasks SET payload=? WHERE id=?",
                       (json.dumps(record, ensure_ascii=False), task_id))
        return record

    def delete_task(self, task_id: str, *, expected_updated_at=None) -> bool:
        """Remove one selected task while keeping files and recorded study history."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return False
            record = json.loads(row[0])
            if expected_updated_at is not None and record.get("updated_at") != expected_updated_at:
                raise ValueError("This task changed in another view. Refresh and review it before deleting.")
            # Reserve the ID so a deliberately re-added task cannot inherit old
            # schedule references or previously recorded daily progress.
            db.execute("INSERT OR IGNORE INTO deleted_task_ids VALUES (?)", (task_id,))
            db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        return True

    def save_plan(self, plan: dict) -> str:
        payload = json.dumps(plan, ensure_ascii=False, sort_keys=True)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            snapshot = plan.get("inputs", {}).get("tasks")
            if snapshot is not None:
                current = [self._task(json.loads(row[0])) for row in db.execute("SELECT payload FROM tasks ORDER BY rowid")]
                if current != snapshot:
                    raise ValueError("Tasks or study progress changed. Generate a fresh plan before saving.")
            latest = db.execute("SELECT id, payload FROM plans ORDER BY rowid DESC LIMIT 1").fetchone()
            if latest:
                previous = json.loads(latest[1])
                previous.pop("id", None)
                previous.pop("saved_at", None)
                if json.dumps(previous, ensure_ascii=False, sort_keys=True) == payload:
                    return latest[0]
            saved_at = datetime.now(UTC).isoformat()
            record_id = hashlib.sha256((payload + saved_at).encode()).hexdigest()
            saved = dict(plan, id=record_id, saved_at=saved_at)
            db.execute("INSERT INTO plans VALUES (?, ?)", (record_id, json.dumps(saved, ensure_ascii=False)))
        return record_id

    @staticmethod
    def _plans(db) -> list[dict]:
        deleted = {row[0] for row in db.execute("SELECT task_id FROM deleted_task_ids")}
        plans = []
        for identifier, payload in db.execute("SELECT id, payload FROM plans ORDER BY rowid"):
            plan = dict(json.loads(payload), id=identifier)
            for field in ("blocks", "unscheduled"):
                if field in plan.get("result", {}):
                    plan["result"][field] = [item for item in plan["result"][field]
                                             if item.get("task_id") not in deleted]
            if "tasks" in plan.get("inputs", {}):
                plan["inputs"]["tasks"] = [task for task in plan["inputs"]["tasks"]
                                           if task.get("id") not in deleted]
            # Keep empty plan versions: their activation still supersedes earlier plans.
            plans.append(plan)
        return plans

    def plans(self) -> list[dict]:
        with self.connect() as db:
            return self._plans(db)

    def latest_plan(self) -> dict | None:
        plans = self.plans()
        return plans[-1] if plans else None

    def daily_reviews(self) -> list[dict]:
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM daily_reviews ORDER BY day")]

    def progress_by_day(self) -> dict[str, int]:
        return {record["date"]: sum(record["minutes"].values()) for record in self.daily_reviews()}

    def record_daily_review(self, day: str, plan_id: str, minutes: dict, *, now=None) -> dict:
        """Apply one day's explicit progress atomically; retries never double-count it."""
        from studentflow.daily_review import planned_days

        try:
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("Choose a valid review date.") from None
        if not isinstance(minutes, dict) or any(
            isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10000
            for value in minutes.values()
        ):
            raise ValueError("Study progress must use whole minutes from 0 to 10000.")
        instant = now or datetime.now(UTC)
        if instant.utcoffset() is None:
            raise ValueError("The review time must include a timezone.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM daily_reviews WHERE day=?", (day,)).fetchone()
            if row:
                existing = json.loads(row[0])
                if existing["plan_id"] == plan_id and existing.get("requested_minutes", existing["minutes"]) == minutes:
                    return existing
                raise ValueError("Progress for this day is already saved. Edit a task in Tasks to correct its total.")
            planned = planned_days(self._plans(db)).get(day)
            if not planned or planned["plan_id"] != plan_id:
                raise ValueError("The saved plan changed. Refresh and review the current schedule.")
            if instant < datetime.fromisoformat(planned["due_at"]):
                raise ValueError("Daily review opens at the day's study end time.")
            if set(minutes) - set(planned["minutes"]):
                raise ValueError("Progress must refer to tasks in this day's saved plan.")
            applied = {}
            for identifier, amount in minutes.items():
                row = db.execute("SELECT payload FROM tasks WHERE id=?", (identifier,)).fetchone()
                if row is None:
                    raise ValueError("A planned task is no longer available. Refresh the schedule.")
                task = self._task(json.loads(row[0]))
                available = task["remaining_minutes"]
                if available is None and amount:
                    raise ValueError("Enter the task's total study estimate before recording progress.")
                credited = min(amount, available or 0)
                applied[identifier] = credited
                task["completed_minutes"] += credited
                task["updated_at"] = instant.isoformat()
                task = self._task(task)
                db.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(task, ensure_ascii=False), identifier))
            record = {"date": day, "plan_id": plan_id, "minutes": applied,
                      "requested_minutes": minutes, "submitted_at": instant.isoformat()}
            db.execute("INSERT INTO daily_reviews VALUES (?, ?)", (day, json.dumps(record, ensure_ascii=False)))
        return record
