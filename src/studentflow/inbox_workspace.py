"""Shared email selection and reading surface for the Inbox workspace."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from email.utils import parseaddr, parsedate_to_datetime
from html import escape

import streamlit as st

from studentflow.email_label_catalog import DEFAULT_NAMES, LabelCatalog
from studentflow.inbox_triage import InboxTriageStore


def set_selection(identifiers):
    selected = list(dict.fromkeys(identifiers))
    st.session_state["batch_selected"] = selected
    st.session_state["batch_consent"] = False
    st.session_state.pop("sf_triage_preview", None)
    for key in list(st.session_state):
        if key.startswith("sf_mail_check_"):
            st.session_state[key] = key.removeprefix("sf_mail_check_") in selected


def _toggle_message(identifier):
    selected = list(st.session_state.get("batch_selected", []))
    if st.session_state.get("sf_mail_check_" + identifier):
        if identifier not in selected:
            selected.append(identifier)
    elif identifier in selected:
        selected.remove(identifier)
    set_selection(selected)


def _open_message(identifier):
    st.session_state["sf_mail_pending_open"] = identifier
    st.session_state["sf_mail_focus"] = identifier
    st.session_state["sf_inbox_workflow"] = "Read email"
    if not st.session_state.get("batch_selected"):
        set_selection([identifier])


def clear_workspace():
    for key in list(st.session_state):
        if key.startswith("sf_mail_"):
            st.session_state.pop(key, None)


def _short_date(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        try:
            parsed = parsedate_to_datetime(value)
        except (ValueError, TypeError, IndexError):
            return ""
    return parsed.strftime("%d %b")


def label_state(store, account, identifiers):
    if not account:
        return {}, dict(DEFAULT_NAMES)
    try:
        records = InboxTriageStore(store.path).list(account, identifiers)
        names = LabelCatalog(store.path).get(account)["names"]
        return records, names
    except (ValueError, OSError, sqlite3.Error):
        st.warning("Saved labels could not be loaded. Your messages are still available.")
        return {}, dict(DEFAULT_NAMES)


def render_mail_list(headers, store, account):
    """Select by Gmail ID; reading a subject is an explicit, separate action."""
    by_id = {header["id"]: header for header in headers}
    selected = [identifier for identifier in st.session_state.get("batch_selected", []) if identifier in by_id]
    pending = st.session_state.pop("sf_triage_pending_selection", None)
    if pending is not None:
        selected = [identifier for identifier in pending if identifier in by_id]
        set_selection(selected)
    elif selected != st.session_state.get("batch_selected", []):
        set_selection(selected)
    records, names = label_state(store, account, list(by_id))
    query = st.text_input("Search messages", placeholder="Search subject or sender", key="sf_mail_search")
    filters = {"all": "All messages", "unclassified": "Unclassified", **names}
    if st.session_state.get("sf_mail_filter", "all") not in filters:
        st.session_state["sf_mail_filter"] = "all"
    chosen = st.selectbox("Filter by label", list(filters), format_func=filters.get, key="sf_mail_filter")
    visible = []
    for header in headers:
        record = records.get(header["id"], {})
        category = record.get("category", "unclassified")
        matches_label = chosen == "all" or chosen == category or chosen in record.get("tag_ids", [])
        matches_text = query.strip().casefold() in (str(header.get("subject", "")) + " " + str(header.get("sender", ""))).casefold()
        if matches_label and matches_text:
            visible.append(header)
    left, right = st.columns(2)
    left.button("Select all shown", key="sf_mail_all", on_click=set_selection,
                args=([header["id"] for header in visible],), disabled=not visible, width="stretch", wrap=True)
    right.button("Clear selection", key="sf_mail_clear", on_click=set_selection,
                 args=([],), disabled=not selected, width="stretch")
    hidden = len(set(selected) - {header["id"] for header in visible})
    st.caption(f"{len(visible)} shown · {len(selected)} selected" + (f" · {hidden} selected outside this view" if hidden else ""))
    with st.container(height=560, border=False, key="sf_mail_list"):
        if not visible:
            st.html('<div class="sf-empty-state"><strong>No messages to show</strong><p>Refresh your inbox or adjust the search and label filter.</p></div>')
        for header in visible:
            identifier = header["id"]
            active = st.session_state.get("sf_mail_focus") == identifier
            with st.container(key=("sf_mail_row_active_" if active else "sf_mail_row_") + identifier):
                check, body = st.columns([0.1, 0.9], gap="small")
                key = "sf_mail_check_" + identifier
                st.session_state[key] = identifier in selected
                check.checkbox("Select " + (header.get("subject") or "Untitled message"),
                               key=key, label_visibility="collapsed", on_change=_toggle_message, args=(identifier,))
                with body:
                    st.button(header.get("subject") or "Untitled message", key="sf_mail_open_" + identifier,
                              type="tertiary", width="stretch", wrap=True,
                              on_click=_open_message, args=(identifier,))
                    sender = parseaddr(str(header.get("sender", "")))
                    st.caption((sender[0] or sender[1] or "Unknown sender") + " · " + _short_date(header.get("sent_at")))
                    record = records.get(identifier, {})
                    category = record.get("category")
                    tone = {"needs_action": "sf-pill-warning", "ignore": "sf-pill-neutral"}.get(category, "")
                    labels = [(names.get(category, "Unclassified"), tone)]
                    labels += [(names[tag], "sf-pill-success") for tag in record.get("tag_ids", []) if tag in names]
                    st.html('<div class="sf-mail-labels">' + " ".join(
                        '<span class="sf-pill ' + tone + '">' + escape(label) + '</span>' for label, tone in labels
                    ) + '</div>')
    return selected


def render_mail_body(mail, time_label):
    st.subheader(mail.get("subject") or "Untitled message")
    st.caption(f"From: {mail.get('sender', '')} · {time_label} · Singapore time")
    # Source content is plain text, never interpreted as HTML or instructions.
    st.html('<div class="sf-mail-body">' + escape(str(mail.get("body") or "No readable message text.")) + '</div>')
