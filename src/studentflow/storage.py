"""Local confirmed records. No mailbox or calendar side effects."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS daily_reviews (day TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS deleted_task_ids (task_id TEXT PRIMARY KEY)")

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

    def save_tasks(self, tasks: list[dict]) -> list[str]:
        """Save a reviewed selection atomically, updating matching source/title pairs."""
        updated_at = datetime.now(timezone.utc).isoformat()
        identifiers = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = {row[0]: json.loads(row[1]) for row in db.execute("SELECT id, payload FROM tasks")}
            deleted = {row[0] for row in db.execute("SELECT task_id FROM deleted_task_ids")}
            for task in tasks:
                record = dict(task)
                title = str(record.get("title", "")).strip()
                if not title:
                    raise ValueError("Enter a task name.")
                record["title"] = title
                identity = (str(record.get("mail_id", "manual")), title)
                record_id = next((key for key, value in existing.items()
                                  if (str(value.get("mail_id", "manual")), value["title"]) == identity), None)
                record_id = record_id or hashlib.sha256("|".join(identity).encode()).hexdigest()
                # A renamed task retains its ID; a new proposal using its old name
                # must not silently overwrite that user's edit.
                while record_id in deleted or (record_id in existing and (
                    str(existing[record_id].get("mail_id", "manual")), existing[record_id]["title"]
                ) != identity):
                    record_id = hashlib.sha256((record_id + "|new").encode()).hexdigest()
                previous = existing.get(record_id, {})
                record = previous | record
                # Re-extraction and repeated saves must not reset completed work.
                record["completed_minutes"] = previous.get("completed_minutes", record.get("completed_minutes", 0))
                if record["completed_minutes"] and (record.get("estimated_minutes") or 0) < record["completed_minutes"]:
                    raise ValueError("The estimate cannot be less than the time already completed. Edit the task in Tasks.")
                record.update(id=record_id, updated_at=updated_at)
                record = self._task(record)
                db.execute(
                    "INSERT INTO tasks VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                    (record_id, json.dumps(record, ensure_ascii=False)),
                )
                existing[record_id] = record
                identifiers.append(record_id)
        return identifiers

    def tasks(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM tasks ORDER BY rowid").fetchall()
        return [self._task(json.loads(row[0])) for row in rows]

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
                          updated_at=datetime.now(timezone.utc).isoformat())
            record = self._task(record)
            db.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(record, ensure_ascii=False), task_id))
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
            saved_at = datetime.now(timezone.utc).isoformat()
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
        instant = now or datetime.now(timezone.utc)
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
