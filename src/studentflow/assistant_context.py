"""Explicitly selected, credential-free inputs for one assistant request."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from studentflow.run_diagnostics import diagnostic_errors


def source_key(provider: str, account: str, resource: str) -> str:
    identity = [provider, account.strip().casefold(), resource]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def selectable_sources(mails: dict, documents: list[dict], messages: list[dict]) -> list[dict]:
    """Normalize local readable sources; never include attachment bytes or tokens."""
    result = {}
    for mid, mail in mails.items():
        account = str(mail.get("gmail_account") or "")
        identifier = source_key("gmail", account, str(mid))
        result[identifier] = {
            "source_id": identifier, "provider": "gmail", "account": account,
            "title": str(mail.get("subject") or "Untitled message"),
            "text": str(mail.get("body") or ""), "sent_at": mail.get("sent_at"),
            "sender": str(mail.get("sender") or ""), "thread_id": mail.get("thread_id"),
            "gmail_id": mail.get("gmail_id"), "reply_to": mail.get("reply_to"),
            "resource_id": str(mid),
        }
    for document in documents:
        if not document.get("text"):
            continue
        account = str(document.get("gmail_account") or "")
        resource = str(document.get("library_id") or document.get("id") or "")
        if not resource:
            continue
        identifier = source_key("attachment", account, resource)
        result[identifier] = {
            "source_id": identifier, "provider": "attachment", "account": account,
            "title": str(document.get("filename") or "Attachment"),
            "text": str(document["text"]), "sent_at": document.get("mail_sent_at"),
            "resource_id": resource, "mail_id": document.get("mail_id"),
        }
    for message in messages:
        # Chat entries without readable text remain visible in the connection
        # panel but cannot be presented to an agent as complete source evidence.
        if not message.get("text"):
            continue
        account = str(message.get("account") or "")
        resource = str(message.get("name") or message.get("source_id") or "")
        if not resource:
            continue
        identifier = source_key("google_chat", account, resource)
        result[identifier] = {
            "source_id": identifier, "provider": "google_chat", "account": account,
            "title": str(message.get("title") or "Chat message"),
            "text": str(message["text"]), "sent_at": message.get("sent_at"),
            "thread_id": message.get("thread_id"), "resource_id": resource,
            "sender_id": message.get("sender_id", ""),
            "sender_name": message.get("sender_name", ""),
            "sender_type": message.get("sender_type", ""),
            "thread_reply": bool(message.get("thread_reply", False)),
        }
    return list(result.values())


def select_records(records: list[dict], identifiers: list[str], field: str) -> list[dict]:
    available = {str(record[field]): record for record in records}
    if any(identifier not in available for identifier in identifiers):
        raise ValueError("Some selected information is no longer available. Review your selection.")
    return [deepcopy(available[identifier]) for identifier in dict.fromkeys(identifiers)]


def snapshot_signature(snapshot: dict) -> str:
    """Exclude the clock only; changed evidence, progress or preferences invalidate results."""
    value = {key: item for key, item in snapshot.items() if key != "now"}
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def run_metadata(result, model: str, started_at: str, elapsed_seconds: float) -> dict:
    """An explicit allowlist keeps response text and tool arguments out of run logs."""
    usage = getattr(result, "usage", {})
    numeric_usage = {
        key: value for key, value in usage.items()
        if key in {"requests", "input_tokens", "output_tokens", "total_tokens", "model_calls",
                   "tool_calls", "cached_tokens", "unreported_model_calls"}
        and isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    if isinstance(usage.get("usage_complete"), bool):
        numeric_usage["usage_complete"] = usage["usage_complete"]
    return {
        "started_at": started_at, "model": model,
        "status": str(getattr(result, "status", "failed")),
        "elapsed_seconds": round(elapsed_seconds, 2), "usage": numeric_usage,
        "tool_event_count": len(getattr(result, "events", [])),
        "errors": diagnostic_errors(getattr(result, "events", [])),
    }
