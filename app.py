"""StudentFlow: a local inbox, task and study-planning application."""

from __future__ import annotations

import csv
import io
import os
import re
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from studentflow.ai_settings import DEFAULT_MODEL, read_ai_settings, render_ai_settings
from studentflow.attachments import extract_attachment
from studentflow.batch_ui import clear_batch_state, render_batch_extraction
from studentflow.gmail import GmailAuthError, GmailError, connect
from studentflow.planning_ui import render_planner
from studentflow.retrieval import build_chunks, search_chunks
from studentflow.storage import Store

ROOT = Path(__file__).resolve().parent
ZONE = ZoneInfo("Asia/Singapore")
INBOX_PAGE_SIZE = 20


def display_time(value):
    if not value:
        return "Not specified"
    try:
        return datetime.fromisoformat(value).astimezone(ZONE).strftime("%d %b %Y, %H:%M")
    except (ValueError, TypeError):
        return str(value)


def show_warnings(warnings):
    for warning in dict.fromkeys(warnings):
        # Older browser sessions may retain parser messages from the previous UI.
        if re.search(r"[\u3400-\u9fff]", warning):
            st.warning("Some message details need review. Reopen the message to refresh its details.")
        else:
            st.warning(warning)


def available_messages():
    # Preserve source text without exposing old development fixtures.
    return {
        key: mail for key, mail in st.session_state.mails.items()
        if mail.get("provider") == "gmail" or mail.get("source_name", "").startswith("Gmail / ")
    }



def render_message_review(store):
    mails = available_messages()
    if not mails:
        return
    st.divider()
    st.subheader("Message review")
    identifiers = list(mails)
    active = st.session_state.get("active_mail")
    mid = st.selectbox(
        "Opened messages", identifiers,
        index=identifiers.index(active) if active in identifiers else len(identifiers) - 1,
        format_func=lambda key: mails[key]["subject"] or "Untitled message",
        key="review_message",
    )
    mail = mails[mid]
    st.write(f"**{mail['subject'] or 'Untitled message'}**")
    st.caption(f"From: {mail['sender']} · Sent: {display_time(mail['sent_at'])} · Singapore time")
    st.text_area("Message", mail["body"], height=210, disabled=True, key="body_" + mid)
    show_warnings(mail["warnings"])
    st.caption(f"{len(mail['attachments'])} attachment(s)")

    prefix = "task_" + mid
    with st.form("confirm_" + mid):
        st.write("**Add a task**")
        title = st.text_input("Task name", key=prefix + "_title")
        left, right = st.columns(2)
        with left:
            due_text = st.text_input(
                "Deadline (Singapore time)", placeholder="YYYY-MM-DD HH:MM",
                help="Leave blank if the deadline is not clear.", key=prefix + "_due",
            )
        with right:
            minutes = st.number_input(
                "Estimated study time (minutes)", min_value=0, max_value=10000,
                value=0, step=15, help="Use 0 if the required time is not yet known.",
                key=prefix + "_minutes",
            )
        quote = st.text_area(
            "Supporting text", help="Copy a passage from the message that describes the task.",
            height=90, key=prefix + "_quote",
        )
        reviewed = st.checkbox("I have reviewed this task and its supporting text.", key=prefix + "_reviewed")
        if st.form_submit_button("Save task", type="primary"):
            try:
                if not reviewed or not title.strip():
                    raise ValueError("Enter a task name and confirm that the task has been reviewed.")
                if not quote.strip() or quote.strip() not in mail["body"]:
                    raise ValueError("Supporting text must match a passage in this message.")
                due = None
                if due_text.strip():
                    try:
                        due = datetime.strptime(due_text.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=ZONE).isoformat()
                    except ValueError:
                        raise ValueError("Enter the deadline as YYYY-MM-DD HH:MM, or leave it blank.") from None
                complete = bool(due and minutes)
                store.save_task({
                    "mail_id": mid, "source_name": "Gmail", "title": title.strip(),
                    "due_at": due, "estimated_minutes": int(minutes) or None,
                    "status": "confirmed" if complete else "pending", "source_quote": quote.strip(),
                    "mail_subject": mail["subject"], "mail_sent_at": mail["sent_at"],
                    "mail_imported_at": mail.get("imported_at"),
                })
                st.success("Task saved." if complete else "Task saved as Needs details. Add a deadline and estimated time before planning.")
            except ValueError as exc:
                st.error(str(exc))


def reset_inbox(clear_opened=False):
    for key in (
        "gmail_headers", "gmail_next_page_token", "gmail_page_tokens",
        "gmail_selected_message", "gmail_notice",
    ):
        st.session_state.pop(key, None)
    if clear_opened:
        clear_batch_state()
        st.session_state.mails = {}
        st.session_state.candidates = {}
        st.session_state.documents = []
        st.session_state.pop("active_mail", None)
        st.session_state.pop("review_message", None)


def restore_gmail_connection(credential_path, token_path):
    """Resume a previously authorized device once per browser session."""
    st.session_state.gmail_restore_attempted = True
    st.session_state.gmail_consent = True
    st.session_state.pop("gmail_connection_error", None)
    st.session_state.pop("gmail_auth_required", None)
    try:
        with closing(connect(credential_path, token_path)) as mailbox:
            st.session_state.gmail_profile = mailbox.profile()
    except GmailError as exc:
        st.session_state.pop("gmail_profile", None)
        st.session_state.gmail_connection_error = str(exc)
        st.session_state.gmail_auth_required = isinstance(exc, GmailAuthError)


def show_gmail_error(exc):
    if isinstance(exc, GmailAuthError):
        reset_inbox(clear_opened=True)
        st.session_state.pop("gmail_profile", None)
        st.session_state.gmail_restore_attempted = True
        st.session_state.gmail_auth_required = True
        st.session_state.gmail_connection_error = str(exc)
        st.rerun()
    st.error(str(exc))


def load_inbox_page(credential_path, token_path, count, append=False):
    cursor = st.session_state.get("gmail_next_page_token") if append else None
    if append and not cursor:
        return
    previous_profile = st.session_state.get("gmail_profile")
    with closing(connect(credential_path, token_path)) as mailbox:
        profile = mailbox.profile()
        if previous_profile != profile:
            reset_inbox(clear_opened=True)
            st.session_state.gmail_profile = profile
            if append:
                st.session_state.gmail_notice = "The signed-in account changed. Refresh the inbox to continue."
                st.rerun()
        page = mailbox.list_message_page(count, page_token=cursor)

    consumed = set(st.session_state.get("gmail_page_tokens", [])) if append else set()
    if cursor:
        consumed.add(cursor)
    next_cursor = page["next_page_token"]
    if next_cursor and next_cursor in consumed:
        raise GmailError("The inbox could not advance. Refresh the inbox and try again.")

    # Apply the entire batch only after it succeeds, so failures remain retryable.
    existing = st.session_state.get("gmail_headers", []) if append else []
    by_id = {message["id"]: message for message in existing}
    by_id.update({message["id"]: message for message in page["messages"]})
    if not append:
        reset_inbox()
    st.session_state.gmail_profile = profile
    st.session_state.gmail_headers = list(by_id.values())
    st.session_state.gmail_next_page_token = next_cursor
    st.session_state.gmail_page_tokens = consumed


def render_inbox(store, data_dir, api_key, model):
    st.subheader("Inbox")
    st.caption("Turn selected messages into reviewed tasks.")
    credential_path = Path(os.environ.get("STUDENTFLOW_GMAIL_CLIENT", str(ROOT / "credentials" / "gmail_client.json")))
    token_path = data_dir / "auth" / "gmail_token.json"
    saved_sign_in = token_path.is_file()
    if saved_sign_in and not st.session_state.get("gmail_restore_attempted"):
        with st.spinner("Restoring Gmail connection..."):
            restore_gmail_connection(credential_path, token_path)
    elif not saved_sign_in and st.session_state.get("gmail_restore_attempted"):
        # Another window may have disconnected this device.
        reset_inbox(clear_opened=True)
        for key in ("gmail_profile", "gmail_restore_attempted", "gmail_auth_required", "gmail_connection_error"):
            st.session_state.pop(key, None)
        st.session_state.gmail_consent = False
    profile = st.session_state.get("gmail_profile")
    if profile:
        st.success("Gmail connected: " + profile.get("email_address", "Gmail"))
    with st.expander(
        "Gmail connection",
        expanded=not profile,
    ):
        if profile:
            st.caption("Your sign-in is saved on this device and restored when you reopen StudentFlow.")
        elif saved_sign_in:
            st.caption("A sign-in is saved on this device.")
        if not credential_path.is_file():
            st.info("Gmail has not been configured on this device. Complete the setup before connecting.")
            st.link_button("Gmail setup guide", "https://github.com/yihua127/studentflow/blob/main/docs/GMAIL_SETUP.md")
        if not saved_sign_in:
            st.checkbox("Allow StudentFlow to read my Gmail messages.", key="gmail_consent")
            st.caption("Stay signed in on this device until Disconnect Gmail is selected.")
        consent = saved_sign_in or st.session_state.get("gmail_consent", False)
        st.caption("Read-only access. Messages are not sent, deleted or marked as read.")
        connection_error = st.session_state.get("gmail_connection_error")
        if connection_error:
            st.warning(connection_error)
        if saved_sign_in and not profile and not st.session_state.get("gmail_auth_required"):
            if st.button("Retry connection", disabled=not credential_path.is_file()):
                restore_gmail_connection(credential_path, token_path)
                st.rerun()
        connect_label = "Switch account" if profile else "Reconnect Gmail" if saved_sign_in else "Connect Gmail"
        if st.button(connect_label, disabled=not credential_path.is_file() or not consent):
            reset_inbox(clear_opened=True)
            st.session_state.pop("gmail_profile", None)
            st.session_state.pop("gmail_connection_error", None)
            st.session_state.pop("gmail_auth_required", None)
            st.session_state.gmail_restore_attempted = True
            try:
                with st.spinner("Complete Google sign-in in the browser, then return here..."):
                    with closing(connect(credential_path, token_path, interactive=True)) as mailbox:
                        st.session_state.gmail_profile = mailbox.profile()
                st.rerun()
            except GmailError as exc:
                st.session_state.gmail_connection_error = str(exc)
                st.session_state.gmail_auth_required = isinstance(exc, GmailAuthError)
                st.warning(str(exc))
        if token_path.is_file() and st.button("Disconnect Gmail"):
            token_path.unlink(missing_ok=True)
            reset_inbox(clear_opened=True)
            for key in ("gmail_profile", "gmail_restore_attempted", "gmail_auth_required", "gmail_connection_error"):
                st.session_state.pop(key, None)
            st.session_state.pop("gmail_consent", None)
            st.session_state.gmail_signed_out = True
            st.rerun()
        if token_path.is_file():
            st.caption("Disconnecting clears the saved sign-in on this device. Saved tasks remain available.")
    if st.session_state.pop("gmail_signed_out", False):
        st.success("Gmail disconnected from this device.")
    notice = st.session_state.pop("gmail_notice", None)
    if notice:
        st.error(notice)
    ready = bool(st.session_state.get("gmail_profile") and consent and token_path.is_file())
    with st.expander("Task extraction", expanded=True):
        if st.button("Refresh inbox", disabled=not ready):
            try:
                with st.spinner("Loading inbox..."):
                    load_inbox_page(credential_path, token_path, INBOX_PAGE_SIZE)
            except GmailError as exc:
                show_gmail_error(exc)
        if st.session_state.get("gmail_next_page_token"):
            if st.button("Load more", disabled=not ready):
                try:
                    with st.spinner("Loading more messages..."):
                        load_inbox_page(credential_path, token_path, INBOX_PAGE_SIZE, append=True)
                    st.rerun()
                except GmailError as exc:
                    show_gmail_error(exc)
        headers = st.session_state.get("gmail_headers", [])
        if "gmail_headers" in st.session_state:
            st.caption(f"{len(headers)} message(s) loaded.")
            if headers and not st.session_state.get("gmail_next_page_token"):
                st.caption("All inbox messages loaded.")
            elif not headers and not st.session_state.get("gmail_next_page_token"):
                st.info("No messages in the inbox. Refresh after a new message arrives.")
        elif not token_path.is_file():
            st.info("Connect Gmail to view your inbox.")
        # A refresh may have discovered a different account.
        ready = bool(st.session_state.get("gmail_profile") and consent and token_path.is_file())
        render_batch_extraction(headers, credential_path, token_path, store, api_key, model, ready)
    render_message_review(store)



def render_tasks(tasks):
    st.subheader("Tasks")
    st.caption("Review confirmed tasks and complete any missing details before planning.")
    if not tasks:
        st.info("No tasks yet. Select messages in Inbox (Gmail) to extract or add tasks.")
        return
    rows = [{
        "Task": task["title"], "Deadline": display_time(task.get("due_at")),
        "Study time (min)": task.get("estimated_minutes"),
        "Status": "Confirmed" if task["status"] == "confirmed" else "Needs details",
        "Source message": task.get("mail_subject") or "Saved message",
    } for task in tasks]
    st.dataframe(rows, hide_index=True, width="stretch")
    with st.expander("Supporting text"):
        selection = st.selectbox("Task", list(range(len(tasks))), format_func=lambda index: tasks[index]["title"])
        st.text(tasks[selection].get("source_quote", "No supporting text saved."))
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    for row in rows:
        # Email-derived text must remain text when opened in a spreadsheet.
        writer.writerow({
            key: "'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value
            for key, value in row.items()
        })
    st.download_button("Download tasks", buffer.getvalue(), "studentflow_tasks.csv", "text/csv")


def render_attachments():
    st.subheader("Attachments")
    st.caption("Find information in attachments from opened messages.")
    mails = available_messages()
    attachment_count = sum(len(mail["attachments"]) for mail in mails.values())
    if not attachment_count:
        st.info("Open a message with a PDF or Word attachment from Inbox (Gmail) to get started.")
        return
    if st.button("Read attachments", type="primary"):
        documents = []
        for mail in mails.values():
            for attachment in mail["attachments"]:
                document = extract_attachment(attachment)
                document["mail_id"] = mail["id"]
                document["message_subject"] = mail["subject"]
                documents.append(document)
        st.session_state.documents = documents
    documents = [document for document in st.session_state.get("documents", []) if document.get("mail_id") in mails]
    status_labels = {
        "ok": "Ready", "partial": "Partially readable", "needs_ocr": "Text recognition needed",
        "error": "Could not read", "blocked": "Unavailable", "encrypted": "Password protected",
        "unsupported": "Unsupported file type", "empty": "No text found",
    }
    for document in documents:
        with st.expander(f"{document['filename']} · {status_labels.get(document['status'], 'Not available')}"):
            st.caption("Source message: " + mails[document["mail_id"]]["subject"])
            show_warnings(document["warnings"])
            st.text(document["text"])
    if not documents:
        st.caption("Read the attachments to make their text searchable.")
        return
    with st.form("attachment_search"):
        question = st.text_input("Search attachments", placeholder="Enter keywords from the document")
        search = st.form_submit_button("Search")
    if search:
        hits = search_chunks(question, build_chunks(documents))
        if not hits:
            st.info("No matching text found. Try different keywords.")
        for hit in hits:
            with st.container(border=True):
                st.write(hit["filename"])
                page = f"Page {hit['page']}" if hit["page"] else "Document text"
                st.caption(f"{mails[hit['mail_id']]['subject']} · {page}")
                st.text(hit["text"])


def main():
    st.set_page_config(page_title="StudentFlow", page_icon=str(ROOT / "assets" / "studentflow-icon.png"), layout="wide")
    st.set_option("client.toolbarMode", "viewer")
    st.title("StudentFlow")
    st.caption("Turn email into a clear study plan.")
    st.session_state.setdefault("mails", {})
    st.session_state.setdefault("candidates", {})
    data_dir = Path(os.environ.get("STUDENTFLOW_DATA_DIR", str(ROOT / "data")))
    store = Store(data_dir / "studentflow.sqlite3")
    try:
        api_key, model = read_ai_settings(ROOT)
    except ValueError as exc:
        api_key, model = "", DEFAULT_MODEL
        st.error(str(exc))
    tabs = st.tabs(["Inbox (Gmail)", "Tasks", "Study Plan", "Attachments", "Settings"])
    with tabs[0]:
        render_inbox(store, data_dir, api_key, model)
    tasks = store.tasks()
    with tabs[1]:
        render_tasks(tasks)
    with tabs[2]:
        render_planner(tasks, store, data_dir)
    with tabs[3]:
        render_attachments()
    with tabs[4]:
        render_ai_settings(ROOT)


if __name__ == "__main__":
    main()
