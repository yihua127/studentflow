"""Local confirmed records. No mailbox or calendar side effects."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    def connect(self):
        return sqlite3.connect(self.path)

    def save_task(self, task: dict) -> str:
        return self.save_tasks([task])[0]

    def save_tasks(self, tasks: list[dict]) -> list[str]:
        """Save a reviewed selection atomically, updating matching source/title pairs."""
        rows = []
        updated_at = datetime.now(timezone.utc).isoformat()
        for task in tasks:
            record = dict(task)
            record_id = hashlib.sha256(
                (str(record.get("mail_id", "manual")) + "|" + record["title"].strip()).encode()
            ).hexdigest()
            record["id"] = record_id
            record["updated_at"] = updated_at
            rows.append((record_id, json.dumps(record, ensure_ascii=False)))
        with self.connect() as db:
            db.executemany(
                "INSERT INTO tasks VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                rows,
            )
        return [row[0] for row in rows]

    def tasks(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM tasks ORDER BY rowid").fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_plan(self, plan: dict) -> str:
        payload = json.dumps(plan, ensure_ascii=False, sort_keys=True)
        record_id = hashlib.sha256(payload.encode()).hexdigest()
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO plans VALUES (?, ?)", (record_id, payload))
        return record_id
