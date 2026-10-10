"""Durable local attachment originals, searchable text, and deletion markers."""

from __future__ import annotations

import hashlib
import json
import ntpath
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from studentflow.attachments import MAX_BYTES
from studentflow.extraction import message_documents


def library_id(mail: dict, attachment: dict) -> str:
    """Use a source-scoped key, never a filename as a filesystem path."""
    identity = [mail.get("gmail_account", "").casefold(), mail["id"],
                attachment["id"], attachment.get("filename", "attachment")]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def safe_filename(value: str) -> str:
    name = ntpath.basename(str(value).replace("\x00", ""))
    return re.sub(r"[\x00-\x1f\x7f]", "", name).strip()[:240] or "attachment"


class AttachmentLibrary:
    """Keep BLOB originals in the task database, independent of a browser session.

    Explicit deletion removes the original and extracted text. Only an opaque
    deletion marker remains so reopening a message cannot silently restore it.
    """

    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS attachment_documents ("
                       "library_id TEXT PRIMARY KEY, payload TEXT NOT NULL, content BLOB NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS attachment_deletions (library_id TEXT PRIMARY KEY)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=15)

    def save_for_message(self, mail: dict, *, only_library_id: str | None = None) -> list[str]:
        documents = message_documents(mail)
        rows = []
        for attachment, document in zip(mail.get("attachments", []), documents, strict=True):
            identifier = library_id(mail, attachment)
            if only_library_id is not None and identifier != only_library_id:
                continue
            content = attachment.get("content", b"")
            # The ingestion layer normally enforces this, but the library is safe independently.
            if len(content) > MAX_BYTES:
                content = b""
            metadata = {
                **document, "filename": safe_filename(document["filename"]),
                "library_id": identifier, "mail_id": mail["id"],
                "message_subject": mail.get("subject", "Untitled message"),
                "mail_sent_at": mail.get("sent_at"), "mail_sender": mail.get("sender", ""),
                "gmail_account": mail.get("gmail_account"),
                "content_type": attachment.get("content_type", "application/octet-stream"),
                "saved_at": datetime.now(UTC).isoformat(),
                "has_original": bool(content), "saved": True,
            }
            rows.append((identifier, json.dumps(metadata, ensure_ascii=False), content))
        saved = []
        with self.connect() as db:
            for identifier, metadata, content in rows:
                if db.execute("SELECT 1 FROM attachment_deletions WHERE library_id=?", (identifier,)).fetchone():
                    continue
                db.execute("INSERT OR IGNORE INTO attachment_documents VALUES (?, ?, ?)",
                           (identifier, metadata, content))
                saved.append(identifier)
        return saved

    def documents(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM attachment_documents ORDER BY rowid").fetchall()
        return [json.loads(row[0]) for row in rows]

    def content(self, identifier: str) -> bytes | None:
        with self.connect() as db:
            row = db.execute("SELECT content FROM attachment_documents WHERE library_id=?", (identifier,)).fetchone()
        return bytes(row[0]) if row else None

    def deleted_ids(self) -> set[str]:
        with self.connect() as db:
            return {row[0] for row in db.execute("SELECT library_id FROM attachment_deletions")}

    def delete(self, identifier: str) -> None:
        with self.connect() as db:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("DELETE FROM attachment_documents WHERE library_id=?", (identifier,))
            db.execute("INSERT OR IGNORE INTO attachment_deletions VALUES (?)", (identifier,))


def available_documents(library: AttachmentLibrary, mails: dict) -> list[dict]:
    """Merge saved and currently opened sources without reviving deleted files."""
    merged = {document["library_id"]: document for document in library.documents()}
    deleted = library.deleted_ids()
    for mail in mails.values():
        for attachment, document in zip(mail.get("attachments", []), message_documents(mail), strict=True):
            identifier = library_id(mail, attachment)
            if identifier not in merged and identifier not in deleted:
                merged[identifier] = {
                    **document, "filename": safe_filename(document["filename"]),
                    "library_id": identifier, "mail_id": mail["id"],
                    "message_subject": mail.get("subject", "Untitled message"),
                    "content_type": attachment.get("content_type", "application/octet-stream"),
                    "has_original": bool(attachment.get("content")), "saved": False,
                }
    return list(merged.values())
