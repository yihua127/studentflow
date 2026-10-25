"""Explicit, account-bound transfer of one Inbox message to the assistant."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from studentflow.assistant_context import source_key

HANDOFF_KEY = "sf_reply_handoff"
NAVIGATION_KEY = "sf_app_pending_tab"


def clear_reply_handoff(state: MutableMapping[str, Any]) -> None:
    """Drop Gmail-dependent requests while preserving unrelated meeting work."""
    state.pop(HANDOFF_KEY, None)
    state.pop(NAVIGATION_KEY, None)
    state.pop("sf_assistant_sources", None)
    state.pop("sf_assistant_result", None)
    state.pop("sf_general_sources", None)
    state.pop("sf_general_result", None)
    for key in list(state):
        if str(key).startswith(("sf_assistant_consent_", "sf_general_consent_")):
            state.pop(key, None)


def queue_reply_handoff(
    state: MutableMapping[str, Any], account: str, mail_id: str, mails: dict,
) -> dict:
    """Transfer only the message explicitly chosen with Reply with assistant."""
    account = str(account or "").strip().casefold()
    mail = mails.get(mail_id, {})
    if (not account or not mail_id or mail.get("provider") != "gmail"
            or str(mail.get("gmail_account") or "").strip().casefold() != account):
        raise ValueError("This email is no longer available for the connected Gmail account.")
    handoff = {
        "account": account, "mail_id": mail_id,
        "source_id": source_key("gmail", account, str(mail_id)),
    }
    state[HANDOFF_KEY] = handoff
    state[NAVIGATION_KEY] = "Assistant"
    state["sf_assistant_workspace"] = "Email replies"
    return dict(handoff)


def consume_reply_handoff(
    state: MutableMapping[str, Any], available: list[dict], account: str,
) -> dict | None:
    """Select an explicit handoff once, only while its account and source match.

    Call before rendering the assistant source chooser. Request and writing style
    stay under the caller's control; this function never runs a model.
    """
    handoff = state.pop(HANDOFF_KEY, None)
    if handoff is None:
        return None
    account = str(account or "").strip().casefold()
    valid = isinstance(handoff, dict) and account and handoff.get("account") == account
    source = next((item for item in available if isinstance(handoff, dict)
                   and item.get("source_id") == handoff.get("source_id")), None)
    if (not valid or not source or source.get("provider") != "gmail"
            or str(source.get("account") or "").strip().casefold() != account
            or str(source.get("resource_id") or "") != handoff.get("mail_id")):
        clear_reply_handoff(state)
        return None
    state["sf_assistant_sources"] = [handoff["source_id"]]
    state.pop("sf_assistant_result", None)
    for key in list(state):
        if str(key).startswith("sf_assistant_consent_"):
            state.pop(key, None)
    return dict(handoff)
