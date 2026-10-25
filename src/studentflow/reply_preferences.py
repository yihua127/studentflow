"""Small, account-scoped local preferences for composing reviewed email replies."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

DEFAULT_REPLY_REQUEST = (
    "Draft an English reply to the selected email, matching the tone of my writing sample when provided. "
    "Keep my name exactly as supplied in the signature. Use the selected email for the subject, greeting "
    "and relevant details. Use my sample only for style; do not copy its factual details or leave template "
    "placeholders. If information is missing, use natural, general wording and list anything requiring my "
    "confirmation separately. Do not invent names, dates or commitments. Provide an editable draft for "
    "review. Do not send it. Keep all explanations in English."
)


def preference_account(account: str) -> str:
    """Use the connected Gmail identity; disconnected use has a separate local scope."""
    return str(account or "").strip().casefold()


def reply_preferences_path(data_dir: Path, account: str) -> Path:
    identity = "gmail:" + preference_account(account) if preference_account(account) else "local:disconnected"
    filename = hashlib.sha256(identity.encode("utf-8")).hexdigest() + ".json"
    return Path(data_dir) / "reply_preferences" / filename


def _validate(value: object, account: str) -> dict:
    if not isinstance(value, dict) or set(value) != {"version", "account", "style", "request", "style_approved"}:
        raise ValueError("Saved reply preferences could not be loaded.")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("Saved reply preferences have an unsupported version.")
    if value["account"] != preference_account(account):
        raise ValueError("Saved reply preferences belong to a different account.")
    for key, limit in (("style", 6000), ("request", 4000)):
        if not isinstance(value[key], str) or len(value[key]) > limit or "\x00" in value[key]:
            raise ValueError("Saved reply preferences contain an invalid field.")
    if type(value["style_approved"]) is not bool or (value["style_approved"] and not value["style"].strip()):
        raise ValueError("Saved reply preferences contain an invalid writing permission.")
    return dict(value)


def read_reply_preferences(data_dir: Path, account: str) -> dict | None:
    path = reply_preferences_path(data_dir, account)
    if not path.exists():
        return None
    if path.stat().st_size > 100_000:
        raise ValueError("Saved reply preferences are too large to load.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Saved reply preferences could not be loaded.") from error
    return _validate(value, account)


def save_reply_preferences(
    data_dir: Path, account: str, *, style: str, request: str, style_approved: bool,
) -> None:
    value = _validate({
        "version": 1, "account": preference_account(account), "style": style,
        "request": request, "style_approved": style_approved if style.strip() else False,
    }, account)
    # Never replace an unreadable prior file with a seemingly empty/default profile.
    read_reply_preferences(data_dir, account)
    path = reply_preferences_path(data_dir, account)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix="reply-", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def clear_reply_preferences(data_dir: Path, account: str) -> None:
    """Delete only this account's saved fields, including an explicitly cleared corrupt file."""
    reply_preferences_path(data_dir, account).unlink(missing_ok=True)
