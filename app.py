"""StudentFlow: a local inbox, task and study-planning application."""

from __future__ import annotations

import os
import re
import sqlite3
from base64 import b64encode
from contextlib import closing
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

import streamlit as st
from streamlit import runtime

from studentflow.ai_settings import DEFAULT_MODEL, read_ai_settings, render_ai_settings
from studentflow.assistant_ui import _preserve_workspace_inputs, render_assistant
from studentflow.attachment_library import AttachmentLibrary
from studentflow.attachment_ui import render_attachments
from studentflow.batch_ui import (
    clear_batch_state,
    fetch_selected_messages,
    preserve_batch_review_inputs,
    render_batch_extraction,
)
from studentflow.daily_review import due_daily_reviews, render_daily_review
from studentflow.extraction import evidence_text, message_documents
from studentflow.gmail import GmailAuthError, GmailError, connect
from studentflow.inbox_triage_ui import clear_triage_state, render_inbox_triage
from studentflow.inbox_workspace import clear_workspace, render_mail_body, render_mail_list
from studentflow.overview_ui import render_overview_schedule, unfinished_tasks
from studentflow.page_heading import render_page_heading
from studentflow.planning_ui import preserve_planning_inputs, render_planner
from studentflow.storage import Store
from studentflow.tasks_ui import preserve_task_inputs, render_tasks
from studentflow.ui_shell import apply_theme, render_gmail_loader, render_navigation

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
    identifiers = list(mails)
    active = st.session_state.get("active_mail")
    mid = st.selectbox(
        "Opened messages", identifiers,
        index=identifiers.index(active) if active in identifiers else len(identifiers) - 1,
        format_func=lambda key: mails[key]["subject"] or "Untitled message",
        key="review_message", on_change=sync_reader_focus,
    )
    mail = mails[mid]
    render_mail_body(mail, display_time(mail["sent_at"]))
    show_warnings(mail["warnings"])
    st.caption(f"{len(mail['attachments'])} attachment(s)")
    with st.expander("Add a task manually"):
        render_manual_task(store, mail, mid)


def sync_reader_focus():
    mail = available_messages().get(st.session_state.get("review_message"), {})
    if mail.get("gmail_id"):
        st.session_state["sf_mail_focus"] = mail["gmail_id"]


def render_manual_task(store, mail, mid):
    documents = {document["id"]: document for document in message_documents(mail) if document.get("text")}
    source_id = None
    if documents:
        source_id = st.selectbox(
            "Task evidence", [None, *documents],
            format_func=lambda value: "Message text" if value is None else documents[value]["filename"],
            key="manual_source_" + mid,
        )
        if source_id:
            with st.expander("Attachment text", expanded=True):
                st.text(documents[source_id]["text"])
    source_text = evidence_text(mail, source_id)
    prefix = "task_" + mid + ("_" + source_id if source_id else "")
    st.caption("Confirmed tasks need a deadline and an estimated study time greater than 0 minutes.")
    with st.form("confirm_" + prefix):
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
            "Supporting text", help="Copy a passage from the selected message or attachment that describes the task.",
            height=90, key=prefix + "_quote",
        )
        reviewed = st.checkbox("I have reviewed this task and its supporting text.", key=prefix + "_reviewed")
        with st.container(key="sf_save_manual_" + prefix, horizontal=True, horizontal_alignment="center"):
            save_task = st.form_submit_button("Save task", type="primary")
        if save_task:
            try:
                if not reviewed or not title.strip():
                    raise ValueError("Enter a task name and confirm that the task has been reviewed.")
                if not quote.strip() or quote.strip() not in source_text:
                    raise ValueError("Supporting text must match a passage in the selected message or attachment.")
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
                    "source_attachment_id": source_id,
                    "source_filename": documents[source_id]["filename"] if source_id else None,
                })
                st.success("Task saved." if complete else "Task saved as Needs details. Add a deadline and estimated time before planning.")
                try:
                    AttachmentLibrary(store.path).save_for_message(mail)
                except Exception:  # noqa: BLE001 - The saved task survives any attachment failure.
                    st.warning("The task was saved, but its attachments could not be saved. Open Attachments and try saving them again.")
            except ValueError as exc:
                st.error(str(exc))


def reset_inbox(clear_opened=False):
    for key in (
        "gmail_headers", "gmail_next_page_token", "gmail_page_tokens",
        "gmail_selected_message", "gmail_notice",
    ):
        st.session_state.pop(key, None)
    if clear_opened:
        clear_workspace()
        clear_batch_state()
        clear_triage_state()
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


def render_gmail_connection(data_dir):
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
    completed_in = st.session_state.pop("sf_gmail_completed_in", None)
    with st.sidebar.container(key="sf_gmail_connection"):
        connection_state = "connected" if profile else "disconnected"
        st.html(f'<template id="sf-welcome-gmail-state" data-status="{connection_state}"></template>')
        if profile:
            with st.container(key="sf_gmail_success"):
                illustration, message = st.columns([1, 1.4], vertical_alignment="center")
                with illustration:
                    st.image(str(ROOT / "assets" / "welcome" / "gmail-connected.jpg"))
                with message:
                    if completed_in is not None:
                        render_gmail_loader("done", completed_in)
                    else:
                        st.markdown("**Gmail connected**")
                    st.html('<button type="button" class="sf-gmail-exit" data-enter-overview>Enter StudentFlow</button>')
        with st.expander(
            "Gmail connection",
            expanded=True,
        ):
            if profile:
                if completed_in is not None:
                    render_gmail_loader("done", completed_in)
                st.caption("Connected as")
                st.text(profile.get("email_address", "Gmail"))
            elif saved_sign_in:
                st.caption("A sign-in is saved on this device.")
            if not credential_path.is_file():
                st.info("Gmail has not been configured on this device. Complete the setup before connecting.")
                st.link_button("Gmail setup guide", "https://github.com/yihua127/studentflow/blob/main/docs/GMAIL_SETUP.md")
            if not saved_sign_in:
                st.checkbox("Allow StudentFlow to read my Gmail messages.", key="gmail_consent")
                st.caption("Stay signed in on this device until Disconnect Gmail is selected.")
            consent = saved_sign_in or st.session_state.get("gmail_consent", False)
            st.caption("Read-only inbox access. Sending and labels require separate approval.")
            connection_error = st.session_state.get("gmail_connection_error")
            if connection_error:
                st.warning(connection_error)
            if (
                saved_sign_in and not profile and not st.session_state.get("gmail_auth_required")
                and st.button("Retry connection", disabled=not credential_path.is_file())
            ):
                restore_gmail_connection(credential_path, token_path)
                st.rerun()
            connect_label = "Switch account" if profile else "Reconnect Gmail" if saved_sign_in else "Connect Gmail"
            connect_action, disconnect_action = (
                st.container(key="sf_gmail_account_actions").columns(2)
                if saved_sign_in else (st, st)
            )
            if connect_action.button(
                connect_label, disabled=not credential_path.is_file() or not consent, width="stretch", wrap=True,
            ):
                reset_inbox(clear_opened=True)
                st.session_state.pop("gmail_profile", None)
                st.session_state.pop("gmail_connection_error", None)
                st.session_state.pop("gmail_auth_required", None)
                st.session_state.gmail_restore_attempted = True
                started_at = monotonic()
                progress = st.empty()
                with progress.container():
                    render_gmail_loader("working")
                    st.caption("Open Google sign-in, then return here after approving access.")
                    login_link = st.empty()
                try:
                    with closing(connect(
                        credential_path, token_path, interactive=True,
                        show_login_link=lambda url: login_link.link_button("Open Google sign-in", url),
                    )) as mailbox:
                        st.session_state.gmail_profile = mailbox.profile()
                except GmailError as exc:
                    st.session_state.gmail_connection_error = str(exc)
                    st.session_state.gmail_auth_required = isinstance(exc, GmailAuthError)
                    with progress.container():
                        render_gmail_loader("error", monotonic() - started_at)
                        st.warning(str(exc))
                else:
                    st.session_state.sf_gmail_completed_in = monotonic() - started_at
                    st.rerun()
            if token_path.is_file() and disconnect_action.button("Disconnect Gmail", width="stretch", wrap=True):
                token_path.unlink(missing_ok=True)
                reset_inbox(clear_opened=True)
                for key in ("gmail_profile", "gmail_restore_attempted", "gmail_auth_required", "gmail_connection_error"):
                    st.session_state.pop(key, None)
                st.session_state.pop("gmail_consent", None)
                st.session_state.gmail_signed_out = True
                st.rerun()
            if not profile:
                st.html('<button type="button" class="sf-gmail-exit">Skip for now</button>')
            if token_path.is_file():
                st.caption("Disconnecting keeps your saved tasks.")
    if st.session_state.pop("gmail_signed_out", False):
        st.sidebar.success("Gmail disconnected from this device.")
    return credential_path, token_path, consent


def render_inbox(store, data_dir, api_key, model, gmail_connection):
    credential_path, token_path, consent = gmail_connection
    render_page_heading(
        "YOUR WORKSPACE",
        "Inbox",
        "A clear next step for every message.",
    )
    notice = st.session_state.pop("gmail_notice", None)
    if notice:
        st.error(notice)
    ready = bool(st.session_state.get("gmail_profile") and consent and token_path.is_file())
    listing, workspace = st.columns([0.9, 1.5], gap="medium")
    messages_panel = listing.container(border=True, key="sf_mail_browser")
    with messages_panel, st.container(key="sf_inbox_scroll_messages"):
        with st.container(horizontal=True, horizontal_alignment="distribute",
                          vertical_alignment="center", key="sf_inbox_toolbar"):
            st.html('<h2 class="sf-inbox-panel-title">Messages</h2>')
            if st.button("Refresh inbox", key="sf_inbox_refresh", disabled=not ready):
                loading = messages_panel.empty()
                mailbox_art = ROOT / "assets" / "inbox" / "loading-mailbox.png"
                mailbox_image = b64encode(mailbox_art.read_bytes()).decode("ascii")
                loading.html(
                    '<div class="sf-inbox-loading" role="status">'
                    f'<img src="data:image/png;base64,{mailbox_image}" alt="">'
                    '<span class="sf-inbox-loading-label">'
                    '<span class="sf-inbox-loading-mark" aria-hidden="true"></span>'
                    'Loading inbox…</span></div>'
                )
                try:
                    load_inbox_page(credential_path, token_path, INBOX_PAGE_SIZE)
                except GmailError as exc:
                    show_gmail_error(exc)
                finally:
                    loading.empty()
        if (
            st.session_state.get("gmail_next_page_token")
            and st.button("Load more", disabled=not ready)
        ):
            try:
                with st.spinner("Loading more messages..."):
                    load_inbox_page(credential_path, token_path, INBOX_PAGE_SIZE, append=True)
                st.rerun()
            except GmailError as exc:
                show_gmail_error(exc)
        if "gmail_headers" in st.session_state:
            st.caption(f"{len(st.session_state['gmail_headers'])} messages loaded")
        headers = st.session_state.get("gmail_headers", [])
        if not token_path.is_file():
            st.info("Connect Gmail to view your inbox.")
        elif "gmail_headers" in st.session_state and not headers:
            st.info("No messages in the inbox. Refresh after a new message arrives.")
        ready = bool(st.session_state.get("gmail_profile") and consent and token_path.is_file())
        account = st.session_state.get("gmail_profile", {}).get("email_address", "")
        selected = render_mail_list(headers, store, account)
    pending = st.session_state.pop("sf_mail_pending_open", None)
    if pending and ready and pending in {header["id"] for header in headers}:
        open_workspace_messages([pending], credential_path, token_path)
    with workspace, st.container(border=True, key="sf_mail_workspace"), st.container(key="sf_inbox_scroll_workspace"):
        st.html('<h2 class="sf-inbox-panel-title">Email workspace</h2>')
        st.caption(f"{len(selected)} selected · Read, organize or turn requirements into a plan.")
        st.session_state.setdefault("sf_inbox_workflow", "Read email")
        workflow = st.radio(
            "Inbox workflow", ["Read email", "Tasks & events", "Email categories"],
            horizontal=True, key="sf_inbox_workflow", label_visibility="collapsed", width="stretch",
        )
        if workflow == "Email categories":
            render_inbox_triage(
                headers, credential_path, token_path, store, data_dir, api_key, model, ready,
                selected_ids=selected,
            )
        elif workflow == "Tasks & events":
            render_batch_extraction(headers, credential_path, token_path, store, api_key, model, ready,
                                    selected_ids=selected)
            if available_messages():
                with st.expander("Opened message text"):
                    active = st.session_state.get("active_mail")
                    if active in available_messages():
                        mail = available_messages()[active]
                        render_mail_body(mail, display_time(mail["sent_at"]))
        else:
            read, reply = st.columns(2)
            if read.button("View selected messages", key="sf_mail_view", disabled=not (ready and selected), width="stretch"):
                open_workspace_messages(selected, credential_path, token_path)
            focus = st.session_state.get("sf_mail_focus")
            opened_ids = {mail.get("gmail_id") for mail in available_messages().values()}
            reply_target = focus if focus in opened_ids else (selected[0] if len(selected) == 1 else None)
            if reply.button("Reply to this email", key="sf_mail_reply", disabled=not (ready and reply_target),
                            type="primary", width="stretch"):
                opened = open_workspace_messages([reply_target], credential_path, token_path)
                if opened:
                    from studentflow.reply_handoff import queue_reply_handoff
                    queue_reply_handoff(st.session_state, account, opened[0]["mail_id"], st.session_state.mails)
                    st.rerun()
            if available_messages():
                render_message_review(store)
            else:
                reading_art = ROOT / "assets" / "inbox" / "reading-usagi.png"
                image = b64encode(reading_art.read_bytes()).decode("ascii")
                st.html(
                    '<div class="sf-empty-state sf-reading-space"><strong>Your reading space</strong>'
                    '<p>Click a subject to read an email here.<br>'
                    'Select messages on the left to classify them or extract tasks.</p>'
                    f'<img class="sf-reading-space-art" src="data:image/png;base64,{image}" alt=""></div>'
                )


def open_workspace_messages(identifiers, credential_path, token_path):
    try:
        with st.spinner("Opening messages..."):
            opened = fetch_selected_messages(identifiers, credential_path, token_path,
                                             st.session_state.get("gmail_profile"), st.session_state.mails)
        successful = [entry for entry in opened if "mail_id" in entry]
        if successful:
            st.session_state["active_mail"] = successful[0]["mail_id"]
            st.session_state["review_message"] = successful[0]["mail_id"]
            st.session_state["sf_mail_focus"] = successful[0]["gmail_id"]
        for entry in opened:
            if "error" in entry:
                st.error(entry["error"])
        return successful
    except GmailError as exc:
        show_gmail_error(exc)
    return []




@st.cache_data(show_spinner=False)
def _welcome_file(filename: str, modified_at: int) -> bytes:
    return (ROOT / "assets" / "welcome" / filename).read_bytes()


def _welcome_image_url(filename: str, mime: str) -> str:
    path = ROOT / "assets" / "welcome" / filename
    data = _welcome_file(filename, path.stat().st_mtime_ns)
    if runtime.exists():
        # Register on every rerun: Streamlit removes media with no active session reference.
        # Only the fixed bundled artwork below is exposed, never account or uploaded files.
        url = runtime.get_instance().media_file_mgr.add(
            data, mime, f"studentflow.welcome.{filename}"
        )
        base_path = st.get_option("server.baseUrlPath").strip("/")
        return f"/{base_path}{url}" if base_path else url
    return f"data:{mime};base64,{b64encode(data).decode('ascii')}"


def welcome_intro() -> str:
    assets = ROOT / "assets" / "welcome"
    html = _welcome_file("intro.html", (assets / "intro.html").stat().st_mtime_ns).decode("utf-8")
    for name, filename, mime in (
        ("MEADOW", "meadow.jpg", "image/jpeg"),
        ("GMAIL_MEADOW", "gmail-meadow.jpg", "image/jpeg"),
        ("BUBBLE", "bubble.png", "image/png"),
        ("USAGI", "usagi.png", "image/png"),
    ):
        html = html.replace("{{" + name + "}}", _welcome_image_url(filename, mime))
    return html


def main():
    st.set_page_config(
        page_title="StudentFlow",
        page_icon=str(ROOT / "assets" / "studentflow-icon.png"),
        layout="wide",
        initial_sidebar_state="expanded",
    )
    # Show the intro before restoring Gmail or rendering any workspace controls.
    st.html(welcome_intro(), unsafe_allow_javascript=True)
    # These bundled scripts never interpolate email or user input.
    st.html(ROOT / "assets" / "browser-language.html", unsafe_allow_javascript=True)
    st.set_option("client.toolbarMode", "viewer")
    apply_theme(ROOT)
    st.html(ROOT / "assets" / "sidebar-motion.html", unsafe_allow_javascript=True)
    st.html(ROOT / "assets" / "overview" / "calendar-page-turn.html", unsafe_allow_javascript=True)
    st.html(ROOT / "assets" / "overview" / "holo-cards.html", unsafe_allow_javascript=True)
    st.html(ROOT / "assets" / "inbox-dock.html", unsafe_allow_javascript=True)
    st.html(ROOT / "assets" / "task-drag.html", unsafe_allow_javascript=True)
    st.html(ROOT / "assets" / "task-crumple.html", unsafe_allow_javascript=True)
    st.html(ROOT / "assets" / "inbox-glide-select.html", unsafe_allow_javascript=True)
    _preserve_workspace_inputs()
    preserve_batch_review_inputs()
    preserve_task_inputs()
    preserve_planning_inputs()
    st.session_state.setdefault("mails", {})
    st.session_state.setdefault("candidates", {})
    data_dir = Path(os.environ.get("STUDENTFLOW_DATA_DIR", str(ROOT / "data")))
    store = Store(data_dir / "studentflow.sqlite3")
    try:
        api_key, model = read_ai_settings(ROOT)
    except ValueError as exc:
        api_key, model = "", DEFAULT_MODEL
        st.error(str(exc))
    if st.session_state.get("sf_replan_requested"):
        st.session_state["sf_app_pending_tab"] = "Study Plan"
    page = render_navigation()
    gmail_connection = render_gmail_connection(data_dir)
    if page not in ("Overview", "Study Plan"):
        render_daily_progress_prompt(store)
    if page in ("Tasks", "Attachments"):
        download_art = ROOT / "assets" / "tasks" / "saving-usagi.png"
        download_image = b64encode(download_art.read_bytes()).decode("ascii")
        st.html(
            '<template id="sf-task-download-template">'
            '<div class="sf-inbox-loading sf-task-download-loading" role="status">'
            f'<img src="data:image/png;base64,{download_image}" alt="">'
            '<span class="sf-inbox-loading-label">'
            '<span class="sf-inbox-loading-mark" aria-hidden="true"></span>'
            'Starting download…</span></div></template>'
        )
        st.html(ROOT / "assets" / "task-download.html", unsafe_allow_javascript=True)
    if page == "Inbox (Gmail)":
        render_inbox(store, data_dir, api_key, model, gmail_connection)
    elif page == "Overview":
        render_overview(store, data_dir)
    elif page == "Tasks":
        note_art = []
        for name, filename in (
            ("mint", "note-mint-stars.jpg"),
            ("pink", "note-pink-stars.jpg"),
            ("blue", "note-blue-stars.jpg"),
            ("dots", "note-dot-art.jpg"),
        ):
            image = b64encode((ROOT / "assets" / "tasks" / filename).read_bytes()).decode("ascii")
            note_art.append(f'--sf-note-{name}:url("data:image/jpeg;base64,{image}");')
        st.html(ROOT / "assets" / "photo-hover.html", unsafe_allow_javascript=True)
        st.html('<style>[data-testid="stMain"]{' + "".join(note_art) + '}</style>')
        basket = ROOT / "assets" / "tasks" / "paper-bin.png"
        image = b64encode(basket.read_bytes()).decode("ascii")
        paper = ROOT / "assets" / "tasks" / "crumpled-paper.png"
        paper_image = b64encode(paper.read_bytes()).decode("ascii")
        st.html(
            '<template class="sf-task-disposal-template"><div class="sf-task-disposal">'
            '<p>Drag the crumpled task into the bin to delete it.</p>'
            '<button type="button" class="sf-crumpled-paper" draggable="true" '
            'aria-label="Pick up the crumpled task" aria-pressed="false">'
            f'<img src="data:image/png;base64,{paper_image}" alt="" draggable="false"></button>'
            '<button type="button" class="sf-task-trash">'
            f'<img src="data:image/png;base64,{image}" alt="">'
            '<span>Drop paper into the bin</span></button>'
            '<button type="button" class="sf-task-keep">Keep task</button></div></template>'
        )
        render_tasks(store)
    elif page == "Study Plan":
        folder_art = ROOT / "assets" / "usagi-folder.png"
        folder_image = b64encode(folder_art.read_bytes()).decode("ascii")
        progress_art = ROOT / "assets" / "plan-progress-stars.png"
        progress_image = b64encode(progress_art.read_bytes()).decode("ascii")
        st.html(ROOT / "assets" / "plan-folder.html", unsafe_allow_javascript=True)
        st.html(
            '<style>.st-key-sf_plan_tabs{'
            f'--sf-plan-folder:url("data:image/png;base64,{folder_image}");'
            '}.st-key-sf_plan_daily_progress{'
            f'--sf-plan-progress-art:url("data:image/png;base64,{progress_image}");'
            '}</style>'
        )
        with st.container(key="sf_plan_view"):
            render_planner(store.tasks(), store, data_dir, root=ROOT)
            st.html('<p class="sf-plan-progress-hint">Click the folder to close it and view Daily progress.</p>')
            with st.container(key="sf_plan_daily_progress"):
                st.html('<h2 class="sf-plan-progress-title">Daily progress</h2>')
                with st.container(key="sf_plan_progress_reading"):
                    render_daily_review(store, data_dir, show_heading=False, show_empty=True)
    elif page == "Attachments":
        render_attachments(store, available_messages())
        st.html(ROOT / "assets" / "photo-hover.html", unsafe_allow_javascript=True)
    elif page == "Settings":
        frames = (
            ("configuration", (736, 981), (134, 187, 449, 591), "57 44 146 48"),
            ("connection", (500, 500), (43, 0, 415, 500), "26 27 100 24"),
            ("help", (736, 736), (168, 125, 399, 485), "27 26 103 26"),
        )
        frame_styles = []
        for panel, (image_width, image_height), (x, y, width, height), slices in frames:
            image = b64encode((ROOT / "assets" / "settings" / f"{panel}-frame.jpg").read_bytes()).decode("ascii")
            # Clip the original image to the frame bounds before CSS slices its edges.
            frame = (
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
                f'viewBox="{x} {y} {width} {height}"><image width="{image_width}" height="{image_height}" '
                f'href="data:image/jpeg;base64,{image}"/></svg>'
            )
            encoded = b64encode(frame.encode()).decode("ascii")
            illustration = b64encode((ROOT / "assets" / "settings" / f"{panel}-usagi.png").read_bytes()).decode("ascii")
            frame_styles.append(
                f'.st-key-sf_panel_settings_{panel}{{'
                f'--sf-settings-frame:url("data:image/svg+xml;base64,{encoded}") {slices} stretch;'
                f'--sf-settings-illustration:url("data:image/png;base64,{illustration}");}}'
            )
        st.html("<style>" + "".join(frame_styles) + "</style>")
        render_ai_settings(ROOT)
        st.html(ROOT / "assets" / "settings-carousel.html", unsafe_allow_javascript=True)
        st.html(ROOT / "assets" / "photo-hover.html", unsafe_allow_javascript=True)
    elif page == "Discussions":
        st.html(ROOT / "assets" / "discussion-carousel.html", unsafe_allow_javascript=True)
        stars = b64encode((ROOT / "assets" / "discussion-stars.jpg").read_bytes()).decode("ascii")
        illustration = b64encode((ROOT / "assets" / "discussion-usagi.png").read_bytes()).decode("ascii")
        st.html(
            '<style>[data-testid="stMain"]{'
            f'--sf-discussion-stars:url("data:image/jpeg;base64,{stars}");'
            f'--sf-discussion-illustration:url("data:image/png;base64,{illustration}");'
            '}</style>'
        )
        render_assistant(store, data_dir, ROOT, available_messages(), api_key, model,
                         workspace_mode="Meetings & discussions")
    else:
        st.html(ROOT / "assets" / "assistant-card-swap.html", unsafe_allow_javascript=True)
        patterns = []
        for card in ("context", "request"):
            path = ROOT / "assets" / f"assistant-{card}-plaid.jpg"
            image = b64encode(path.read_bytes()).decode("ascii")
            patterns.append(f'--sf-assistant-{card}-plaid:url("data:image/jpeg;base64,{image}");')
        drag_art = ROOT / "assets" / "assistant-motorcycle-usagi.png"
        drag_image = b64encode(drag_art.read_bytes()).decode("ascii")
        patterns.append(f'--sf-assistant-drag-usagi:url("data:image/png;base64,{drag_image}");')
        st.html('<style>.st-key-sf_assistant_card_deck{' + "".join(patterns) + '}</style>')
        render_assistant(store, data_dir, ROOT, available_messages(), api_key, model)

    st.html(f'<template id="sf-workspace-page" data-page="{escape(page)}"></template>')


def _go_to(page):
    st.session_state["sf_app_pending_tab"] = page


@st.fragment(run_every="60s")
def render_daily_progress_prompt(store):
    try:
        due = due_daily_reviews(store.plans(), store.daily_reviews(), datetime.now(UTC))
    except (ValueError, KeyError, sqlite3.Error):
        st.warning("Daily progress could not be loaded. Reopen StudentFlow to try again.")
        return
    if due:
        with st.container(horizontal=True, vertical_alignment="center"):
            st.caption(f"{len(due)} study day(s) ready for your progress check-in.")
            if st.button("Review daily progress", key="sf_daily_prompt"):
                _go_to("Overview")
                st.rerun()


def render_overview(store, data_dir):
    render_page_heading(
        "YOUR STUDY, IN FOCUS",
        "A clearer day starts here.",
        "Keep track of what matters, then pick up where you left off.",
    )
    tasks = store.tasks()
    unfinished = unfinished_tasks(tasks)
    remaining = sum(max(0, (task.get("estimated_minutes") or 0) - (task.get("completed_minutes") or 0)) for task in unfinished)
    summaries = (
        ("Open tasks", str(len(unfinished))),
        ("Study time remaining", f"{remaining // 60}h {remaining % 60}m"),
        ("Needs details", str(sum(task.get("status") != "confirmed" for task in unfinished))),
    )
    cards = []
    for label, value in summaries:
        cards.append(
            '<div class="sf-overview-stat">'
            f'<div class="sf-overview-stat-label">{label}</div>'
            f'<div class="sf-overview-stat-value">{value}</div>'
            '</div>'
        )
    st.html('<div class="sf-overview-stats">' + "".join(cards) + '</div>')
    render_overview_schedule(store, data_dir)
    cue_label = "Pick up your work"
    cue_letters = "".join(
        '<span class="sf-shuffle-space"> </span>' if letter == " "
        else f'<span class="sf-shuffle-cell"><span>{letter}</span></span>'
        for letter in cue_label
    )
    st.html(
        '<div class="sf-shortcut-cue"><a href="#pick-up-your-work" aria-label="Pick up your work">'
        '<span class="sf-shortcut-arrow" aria-hidden="true">↓ ↓ ↓</span>'
        f'<span class="sf-shuffle-label" aria-hidden="true">{cue_letters}</span>'
        '<span class="sf-shortcut-arrow" aria-hidden="true">↓ ↓ ↓</span></a></div>'
    )
    with st.container(key="sf_shortcut_section"):
        st.html('<div id="pick-up-your-work"></div>')
        with st.container(key="sf_shortcut_cards"):
            left, middle, right = st.columns(3)
            for column, title, description, destination, route_key in (
                (left, "Process your inbox", "Review messages, organize labels and prepare replies.", "Inbox (Gmail)", "inbox"),
                (middle, "Make room to study", "Review tasks and build a plan around your availability.", "Study Plan", "study_plan"),
                (right, "Bring a discussion together", "Turn selected messages into decisions and meeting notes.", "Discussions", "discussions"),
            ):
                with column, st.container(key="sf_panel_overview_route_" + route_key):
                    artwork = ROOT / "assets" / "overview" / f"shortcut-{route_key}.png"
                    encoded = b64encode(artwork.read_bytes()).decode("ascii")
                    white_x, white_width = (117, 252) if route_key == "inbox" else (135, 215)
                    frame = (
                        '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="320">'
                        f'<image href="data:image/png;base64,{encoded}" width="480" height="320"/>'
                        f'<rect x="{white_x}" y="134" width="{white_width}" height="166" fill="white"/>'
                        '</svg>'
                    )
                    encoded_frame = b64encode(frame.encode()).decode("ascii")
                    st.html(
                        f'<style>.st-key-sf_panel_overview_route_{route_key}'
                        f'{{--sf-shortcut-art:url("data:image/svg+xml;base64,{encoded_frame}");}}</style>'
                    )
                    st.html(f'<h3 class="sf-shortcut-title">{title}</h3>')
                    st.caption(description)
                    st.button("Open " + destination.replace(" (Gmail)", ""), on_click=_go_to, args=(destination,), width="stretch")
        with st.container(key="sf_overview_daily_progress"):
            st.html('<h3 class="sf-daily-progress-title">Daily progress</h3>')
            st.caption("Record completed study time and review any missed check-ins.")
            render_daily_review(store, data_dir, show_heading=False)


if __name__ == "__main__":
    main()
