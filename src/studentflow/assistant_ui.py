"""A scoped assistant workspace with explicitly reviewed study-time updates."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from studentflow.assistant_context import (
    run_metadata,
    select_records,
    selectable_sources,
    snapshot_signature,
    source_key,
)
from studentflow.attachment_library import AttachmentLibrary, available_documents
from studentflow.fixed_events_ui import render_fixed_event_review
from studentflow.meeting_docx import meeting_minutes_docx
from studentflow.meeting_export import (
    meeting_minutes_filename,
    meeting_minutes_markdown,
    meeting_minutes_text,
)
from studentflow.planning_ui import read_preferences, schedule_rows
from studentflow.reply_preferences import (
    DEFAULT_REPLY_REQUEST,
    clear_reply_preferences,
    preference_account,
    read_reply_preferences,
    save_reply_preferences,
)
from studentflow.response_display import render_response
from studentflow.run_diagnostics import diagnostic_events
from studentflow.source_labels import chronological_sources, source_labels, source_preview_heading
from studentflow.workspace_ui import render_workspace_sources

ZONE = ZoneInfo("Asia/Singapore")

WORKSPACES = {
    "Email replies": "sf_assistant_",
    "Meetings & discussions": "sf_meeting_",
    "Other assistance": "sf_general_",
}
DEFAULT_MEETING_REQUEST = (
    "Analyze the selected discussion in English and provide a structured discussion summary. "
    "Include a concise summary, confirmed decisions, action items with owners and deadlines, "
    "open questions, a suggested agenda and a short follow-up message draft. Support decisions "
    "and action items with quotations from the selected sources. Separate suggestions from "
    "agreed decisions. Leave unstated owners and deadlines unspecified; do not invent them. "
    "If the discussion is incomplete, explain what is missing. Do not create tasks, schedule "
    "events or send messages."
)


def _preserve_workspace_inputs() -> None:
    """Keep inactive workspace inputs through Streamlit's widget cleanup.

    Only ordinary input widgets are detached from cleanup. Buttons, request
    approvals and sending confirmations must never be restored this way.
    """
    keys = {
        prefix + suffix for prefix in WORKSPACES.values()
        for suffix in ("sources", "messages", "attachments", "tasks", "preferences", "calendar", "fixed_events",
                       "request", "style", "style_consent")
    }
    keys.update("sf_workspace_" + suffix for suffix in (
        "consent", "space", "calendar_selection", "from", "through",
    ))
    for key in list(st.session_state):
        is_draft = key.startswith(("sf_assistant_subject_", "sf_assistant_body_"))
        is_recipient = key.startswith("sf_reply_send_") and key.endswith("_recipient")
        if key in keys or is_draft or is_recipient:
            st.session_state[key] = st.session_state[key]


def run_assistant(*args, **kwargs):
    # Keep the SDK lazy so opening the existing pages has no provider side effects.
    from studentflow.agent_runtime import run_assistant as run

    return run(*args, **kwargs)


def academic_dates(rows: list[dict]) -> list[dict]:
    result = []
    for row in rows:
        name = str(row.get("Name") or "").strip()
        start, end = row.get("Start"), row.get("End")
        reference = str(row.get("Reference") or "").strip()
        if not name and not start and not end and not reference:
            continue
        if not name or not start or not end:
            raise ValueError("Each academic reference needs a name, start date and end date.")
        try:
            start_date = date.fromisoformat(str(start)[:10])
            end_date = date.fromisoformat(str(end)[:10])
        except ValueError:
            raise ValueError("Check the dates in the academic calendar references.") from None
        if end_date < start_date:
            raise ValueError("An academic reference must end on or after its start date.")
        result.append({
            "name": name, "title": name, "start": start_date.isoformat(),
            "end": end_date.isoformat(), "source": reference or "User-provided academic calendar",
        })
    return result


def _choose(label: str, records: list[dict], field: str, key: str, label_for, limit: int | None):
    labels = {str(item[field]): label_for(item) for item in records}
    st.session_state[key] = [item for item in st.session_state.get(key, []) if item in labels]
    selected = st.multiselect(
        label, list(labels), format_func=labels.get, key=key, max_selections=limit,
    )
    return select_records(records, selected, field)


def _choose_sources(label: str, records: list[dict], key: str, limit: int | None = 20):
    labels = source_labels(records)
    return _choose(
        label, records, "source_id", key, lambda item: labels[item["source_id"]], limit,
    )


def _set_message_selection(key: str, identifiers: list[str]) -> None:
    st.session_state[key] = list(identifiers)


def _choose_discussion_messages(records: list[dict], key: str, prefix: str) -> list[dict]:
    records = chronological_sources(records)
    identifiers = [item["source_id"] for item in records]
    st.session_state[key] = [sid for sid in st.session_state.get(key, []) if sid in identifiers]
    st.write("**Discussion messages**")
    if records:
        st.caption(
            f"{len(records)} readable message(s) in the selected discussion. "
            "Choose one or more messages below. Times are shown in Singapore time."
        )
    else:
        st.caption("Choose a discussion space above and select Read selected discussion to load its messages.")
    all_column, clear_column = st.columns(2)
    all_column.button(
        "Select all messages", key=prefix + "select_all_messages", disabled=not records,
        on_click=_set_message_selection, args=(key, identifiers),
    )
    clear_column.button(
        "Clear message selection", key=prefix + "clear_messages", disabled=not st.session_state[key],
        on_click=_set_message_selection, args=(key, []),
    )
    selected = _choose_sources("Messages to analyze", records, key, limit=None)
    st.caption(f"{len(selected)} of {len(records)} discussion message(s) selected.")
    return chronological_sources(selected)


def _reset_request_approval() -> None:
    for key in list(st.session_state):
        if key.startswith("sf_assistant_consent_"):
            st.session_state.pop(key, None)
    st.session_state.pop("sf_assistant_result", None)


def _apply_reply_preferences(value: dict | None) -> None:
    value = value or {}
    st.session_state["sf_assistant_style"] = value.get("style", "")
    st.session_state["sf_assistant_style_consent"] = value.get("style_approved", False)
    st.session_state["sf_assistant_request"] = value.get("request", "")
    _reset_request_approval()


def _initialize_reply_preferences(data_dir: Path, account: str) -> None:
    scope = str(data_dir.resolve()) + ":" + account
    previous_scope = st.session_state.get("sf_reply_preferences_scope")
    if previous_scope == scope and all(key in st.session_state for key in (
        "sf_assistant_style", "sf_assistant_style_consent", "sf_assistant_request",
    )):
        return
    st.session_state["sf_reply_preferences_scope"] = scope
    st.session_state.pop("sf_reply_preferences_notice", None)
    st.session_state.pop("sf_reply_preferences_error", None)
    try:
        saved = read_reply_preferences(data_dir, account)
    except (OSError, ValueError):
        saved = None
        st.session_state["sf_reply_preferences_error"] = (
            "Saved reply preferences could not be loaded. The saved file has been kept. "
            "You can still enter the fields below; clear the saved preferences before saving a replacement."
        )
    if saved is not None or previous_scope is not None:
        _apply_reply_preferences(saved)
    if previous_scope is not None and previous_scope != scope:
        from studentflow.reply_handoff import HANDOFF_KEY, NAVIGATION_KEY, clear_reply_handoff

        pending = st.session_state.get(HANDOFF_KEY)
        # The Inbox may already have handed off a message for the new account.
        # Clear old state without discarding that explicit, separately verified action.
        keep_pending = (isinstance(pending, dict) and bool(account)
                        and preference_account(pending.get("account", "")) == account)
        clear_reply_handoff(st.session_state)
        if keep_pending:
            st.session_state[HANDOFF_KEY] = pending
            st.session_state[NAVIGATION_KEY] = "Assistant"
    # These may be hidden while another workspace is open. Keep their values
    # independent of whether Streamlit rendered the corresponding widgets.
    st.session_state.setdefault("sf_assistant_style", "")
    st.session_state.setdefault("sf_assistant_style_consent", False)
    st.session_state.setdefault("sf_assistant_request", "")


def _change_reply_preferences(action: str, data_dir: Path, account: str) -> None:
    """Callbacks run before widgets so loading/clearing can replace their values safely."""
    st.session_state.pop("sf_reply_preferences_notice", None)
    st.session_state.pop("sf_reply_preferences_error", None)
    try:
        if action == "save":
            save_reply_preferences(
                data_dir, account, style=st.session_state.get("sf_assistant_style", ""),
                request=st.session_state.get("sf_assistant_request", ""),
                style_approved=bool(st.session_state.get("sf_assistant_style_consent", False)),
            )
            notice = "Reply preferences saved on this device. They will be loaded when you return."
        elif action == "load":
            saved = read_reply_preferences(data_dir, account)
            if saved is None:
                st.session_state["sf_reply_preferences_notice"] = "There are no saved reply preferences for this account."
                return
            _apply_reply_preferences(saved)
            notice = "Saved reply preferences loaded. Review the selected email before running the assistant."
        else:
            clear_reply_preferences(data_dir, account)
            _apply_reply_preferences(None)
            notice = "Saved reply preferences cleared for this account."
    except (OSError, ValueError):
        st.session_state["sf_reply_preferences_error"] = (
            "Reply preferences could not be " + {"save": "saved", "load": "loaded", "clear": "cleared"}[action]
            + ". Existing saved data has been kept. Check local storage or clear unreadable preferences before saving."
        )
    else:
        st.session_state["sf_reply_preferences_notice"] = notice


def _reply_preferences_controls(data_dir: Path, account: str) -> None:
    st.caption(
        "Save your writing sample and reusable request on this device for "
        + (account if account else "disconnected use") + ". Saving also remembers whether your writing sample "
        "may be used. Each request to OpenAI still needs your approval."
    )
    for column, action, label in zip(st.columns(3), ("save", "load", "clear"), (
        "Save reply preferences", "Load saved reply preferences", "Clear saved reply preferences",
    ), strict=True):
        column.button(
            label, key="sf_reply_preferences_" + action, on_click=_change_reply_preferences,
            args=(action, data_dir, account),
        )
    if error := st.session_state.get("sf_reply_preferences_error"):
        st.warning(error)
    if notice := st.session_state.get("sf_reply_preferences_notice"):
        st.success(notice)


def _evidence(citations: list[dict], sources: dict) -> None:
    if not citations:
        return
    with st.expander("Supporting text"):
        for citation in citations:
            source = sources.get(citation.get("source_id"), {})
            st.text(source_preview_heading(source))
            st.text(citation.get("quote", ""))


def _review_study_time(
    item: dict, report_id: str, store, model: str, selected_task_ids,
    result_key: str = "sf_assistant_result",
) -> None:
    """Save only a selected task's reviewed total, against its original version."""
    previous = item.get("previous")
    task_id = item.get("existing_task_id")
    if not task_id or not isinstance(previous, dict):
        st.caption("New tasks remain suggestions here. Use the Inbox task review and study-time estimate flow to save them.")
        return
    version = previous.get("updated_at")
    eligible = (store is not None and task_id in (selected_task_ids or ())
                and previous.get("id") == task_id and isinstance(version, str) and bool(version.strip()))
    # A changed proposal or source version must never inherit an earlier review.
    identity = json.dumps(
        {"report": report_id, "proposal": item, "model": model}, sort_keys=True, ensure_ascii=False,
    )
    prefix = "sf_assistant_effort_" + hashlib.sha256(identity.encode()).hexdigest()[:20]
    suggested = item.get("suggested_minutes")
    current = previous.get("estimated_minutes")
    default = suggested if suggested is not None else current
    if isinstance(default, bool) or not isinstance(default, int) or not 1 <= default <= 10000:
        default = None
    completed = previous.get("completed_minutes", 0)
    if isinstance(completed, bool) or not isinstance(completed, int) or completed < 0:
        completed = 0
    st.write("**Review study time**")
    st.caption(
        f"Current total: {current if current is not None else 'Not set'} min. "
        f"Already completed: {completed} min. The total includes completed work."
    )
    final_minutes = st.number_input(
        "Total study time (min)", min_value=1, max_value=10000, step=5,
        value=default, key=prefix + "_minutes", disabled=not eligible,
        help="Accept or edit the suggested total. Completed study time will not change.",
    )
    valid_minutes = final_minutes is not None and final_minutes >= completed
    if final_minutes is not None:
        st.caption(f"Remaining after this change: {max(0, final_minutes - completed)} min.")
        if not valid_minutes:
            st.warning("The total cannot be less than the time already completed.")
    else:
        st.caption("Enter the total study time to calculate the remaining minutes.")
    if not eligible:
        st.caption("Select this saved task and run the assistant again to review its latest study time.")
    review_key = prefix + "_review_" + str(final_minutes)
    reviewed = st.checkbox(
        "I have reviewed this total study time and its reasoning.",
        key=review_key, disabled=not eligible or not valid_minutes,
    )
    st.caption("Saving changes only this task's total study time. Its title, deadline and completed minutes stay the same.")
    if st.button(
        "Save study time", key=prefix + "_save",
        disabled=not eligible or not valid_minutes or not reviewed,
    ):
        # Re-check even if a client submits a disabled control.
        if not eligible or not valid_minutes or not reviewed:
            st.error("Review the selected task and its total study time before saving.")
            return
        suggestion = {
            "suggested_minutes": suggested,
            "estimate_reason": item.get("estimate_reason") or "",
            "assumptions": item.get("assumptions") or [],
            "stated_minutes": item.get("estimated_minutes"),
            "model": model,
        }
        try:
            store.review_task_effort(
                task_id, int(final_minutes), suggestion, expected_updated_at=version,
            )
        except ValueError:
            st.error("This study time could not be saved. The task may have changed; run the assistant again and review its latest details.")
        except (OSError, sqlite3.Error):
            st.error("The study time could not be saved. Check the local task storage and try again.")
        else:
            st.session_state.pop(result_key, None)
            st.session_state.pop("planner_draft", None)
            st.session_state[result_key.removesuffix("result") + "effort_notice"] = (
                "Study time saved. Completed progress is unchanged. Open Study Plan to plan with the reviewed total."
            )
            st.rerun()


def _render_artifacts(
    artifacts: dict, sources: dict, report_id: str, store=None, model: str = "", selected_task_ids=None,
    root: Path | None = None, data_dir: Path | None = None,
    result_key: str = "sf_assistant_result",
    analysis_status: str = "completed",
) -> None:
    triage = artifacts.get("triage", [])
    if triage:
        st.write("**Suggested email categories**")
        st.dataframe([
            {"Message": sources.get(item["source_id"], {}).get("title", "Selected message"),
             "Category": item["category"].replace("_", " ").capitalize(),
             "Reason": item["reason"]} for item in triage
        ], hide_index=True)
    for item in artifacts.get("reply_drafts", []):
        with st.container(border=True):
            st.write("**Reply draft**")
            st.caption("Review the recipient and content before sending this reply.")
            st.text("To: " + (item.get("recipient") or "Recipient needs confirmation"))
            subject = st.text_input(
                "Draft subject", value=item["subject"],
                key=f"sf_assistant_subject_{report_id}_{item['id']}",
            )
            body = st.text_area(
                "Draft message", value=item["body"], height=220,
                key=f"sf_assistant_body_{report_id}_{item['id']}",
            )
            st.download_button(
                "Download draft", f"To: {item.get('recipient') or ''}\nSubject: {subject}\n\n{body}",
                file_name="reply-draft.txt", mime="text/plain",
                key=f"sf_assistant_download_{report_id}_{item['id']}",
            )
            if root is not None and data_dir is not None:
                from studentflow.reply_send_ui import render_reply_send

                render_reply_send(root, data_dir, item, sources.get(item.get("source_id"), {}), subject, body, report_id)
            _evidence(item.get("citations", []), sources)
    proposals = artifacts.get("task_proposals", [])
    if proposals:
        st.write("**Task suggestions**")
        st.caption("Review suggestions and their sources. Only an explicitly reviewed study-time update can be saved here; other task details remain previews.")
        st.dataframe([
            {"Task": item["title"], "Suggestion": item["relation"].capitalize(),
             "Deadline": item.get("due_at") or "Needs details",
             "Stated minutes": item.get("estimated_minutes"),
             "Suggested minutes": item.get("suggested_minutes")} for item in proposals
        ], hide_index=True)
        for index, item in enumerate(proposals, 1):
            with st.expander(f"Suggestion {index}: details and evidence"):
                st.text(item["title"])
                previous = item.get("previous")
                if previous:
                    st.dataframe([
                        {"Details": "Current task", "Task": previous.get("title"),
                         "Deadline": previous.get("due_at"),
                         "Estimated minutes": previous.get("estimated_minutes")},
                        {"Details": "Proposed details", "Task": item["title"],
                         "Deadline": item.get("due_at"),
                         "Estimated minutes": item.get("estimated_minutes")},
                    ], hide_index=True)
                    st.caption("Recorded progress is unchanged. Missing proposed values need review.")
                if item.get("estimate_reason"):
                    st.text(item["estimate_reason"])
                for note in item.get("notes", []):
                    st.text(note)
                _evidence(item.get("citations", []), sources)
                _review_study_time(item, report_id, store, model, selected_task_ids, result_key)
    fixed_proposals = artifacts.get("fixed_event_proposals", [])
    for account in sorted({item.get("account", "") for item in fixed_proposals}):
        if account and store is not None:
            render_fixed_event_review(
                [item for item in fixed_proposals if item.get("account") == account],
                store, account=account, key="assistant_" + report_id + "_" + account,
            )
    for item in artifacts.get("meeting_summaries", []):
        with st.container(border=True):
            st.write("**Discussion summary**")
            st.text(item["title"])
            st.text(item["summary"])
            if item.get("decisions"):
                st.write("**Decisions**")
                for decision in item["decisions"]:
                    st.text(decision["text"])
                    _evidence(decision.get("citations", []), sources)
            if item.get("action_items"):
                st.write("**Action items for review**")
                st.dataframe([
                    {"Action": action["title"], "Owner": action.get("owner") or "Not specified",
                     "Deadline": action.get("due_at") or "Not specified"}
                    for action in item["action_items"]
                ], hide_index=True)
                for index, action in enumerate(item["action_items"], 1):
                    with st.expander(f"Action {index}: evidence"):
                        st.text(action["title"])
                        _evidence(action.get("citations", []), sources)
            if item.get("open_questions"):
                st.write("**Open questions**")
                for question in item["open_questions"]:
                    st.text(question)
            if item.get("missing_information"):
                st.write("**Details still to confirm**")
                for detail in item["missing_information"]:
                    st.text(detail)
            if item.get("agenda"):
                st.write("**Suggested agenda**")
                st.text("\n".join(f"{index}. {line}" for index, line in enumerate(item["agenda"], 1)))
            if item.get("follow_up"):
                st.write("**Follow-up draft**")
                st.text(item["follow_up"])
            _evidence(item.get("citations", []), sources)
            incomplete = analysis_status != "completed"
            formats = (
                ("Word", "docx", meeting_minutes_docx,
                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                ("TXT", "txt", meeting_minutes_text, "text/plain; charset=utf-8"),
                ("Markdown", "md", meeting_minutes_markdown, "text/markdown; charset=utf-8"),
            )
            for column, (label, extension, export, mime) in zip(st.columns(3), formats):
                with column:
                    st.download_button(
                        f"Download minutes ({label})",
                        export(item, sources, incomplete=incomplete),
                        file_name=meeting_minutes_filename(item, extension), mime=mime,
                        key=f"sf_meeting_download_{report_id}_{item['id']}_{extension}",
                        on_click="ignore",
                        type="primary" if extension == "docx" else "secondary",
                    )
    for item in artifacts.get("meeting_clarifications", []):
        with st.container(border=True):
            st.write("**Discussion details needed**")
            for question in item.get("questions", []):
                st.text(question)
    for item in artifacts.get("plans", []):
        st.write("**Suggested study plan**")
        st.caption("This is a preview based on the selected tasks and commitments. Your saved plan has not changed.")
        result = item["result"]
        if result.get("blocks"):
            st.dataframe(schedule_rows(result["blocks"]), hide_index=True)
        if result.get("warnings"):
            st.warning("Review these scheduling notes.")
            for warning in result["warnings"]:
                st.text(str(warning))
        if result.get("unscheduled"):
            st.write("**Work that could not be scheduled**")
            st.dataframe(result["unscheduled"], hide_index=True)
        if item.get("validation_errors"):
            st.error("The proposed schedule has validation errors.")
            for error in item["validation_errors"]:
                st.text(str(error))
    for item in artifacts.get("risks", []):
        st.write("**Deadline concentration**")
        st.text(item.get("note", "Based on selected tasks and calendar events."))
        for cluster in item.get("clusters", []):
            st.warning(f"{len(cluster['items'])} deadlines or events within {item['window_hours']} hours.")
            st.dataframe([
                {"Item": entry.get("title", ""), "When": entry["at"], "Type": entry["kind"]}
                for entry in cluster["items"]
            ], hide_index=True)
        if not item.get("clusters"):
            st.info("No cluster matching the selected threshold was found in the selected information.")


def render_assistant(store, data_dir: Path, root: Path, mails: dict, api_key: str, model: str) -> None:
    _preserve_workspace_inputs()
    account = preference_account(st.session_state.get("gmail_profile", {}).get("email_address", ""))
    _initialize_reply_preferences(data_dir, account)
    st.subheader("Assistant")
    if st.session_state.get("sf_reply_handoff"):
        st.session_state["sf_assistant_workspace"] = "Email replies"
    mode = st.radio(
        "Assistant workspace", list(WORKSPACES), horizontal=True, key="sf_assistant_workspace",
    )
    previous_mode = st.session_state.get("sf_assistant_previous_workspace")
    if previous_mode and previous_mode != mode:
        for selected_mode in (previous_mode, mode):
            prefix = WORKSPACES[selected_mode]
            for key in list(st.session_state):
                if key.startswith(prefix + "consent_"):
                    st.session_state.pop(key, None)
            # A data editor cannot have its widget state assigned. Preserve its
            # edited rows as data and recreate the editor when returning instead.
            if prefix + "academic_rows" in st.session_state:
                st.session_state[prefix + "academic_base"] = st.session_state[prefix + "academic_rows"]
            st.session_state.pop(prefix + "academic_dates", None)
    st.session_state["sf_assistant_previous_workspace"] = mode
    _render_workspace(store, data_dir, root, mails, api_key, model, account, mode)


def _render_workspace(store, data_dir, root, mails, api_key, model, account, mode) -> None:
    prefix = WORKSPACES[mode]
    is_email = mode == "Email replies"
    is_meeting = mode == "Meetings & discussions"
    is_general = mode == "Other assistance"
    st.subheader(mode)
    if is_email:
        st.caption("Draft and review a reply using your saved writing style and reply instructions.")
    elif is_meeting:
        st.caption("Summarize discussions, identify decisions and action items, and prepare a follow-up draft.")
        st.session_state.setdefault(prefix + "request", DEFAULT_MEETING_REQUEST)
    else:
        st.caption("Ask about saved tasks, study planning or other selected information.")
        st.info("Review and save study-time updates and fixed appointments below. Study plans remain previews; Calendar sync is reviewed separately in Study Plan.")
    notice = st.session_state.pop(prefix + "effort_notice", None)
    if notice:
        st.success(notice)
    messages, events, coverage = ([], [], {}) if is_email else render_workspace_sources(
        root, data_dir, show_calendar=is_general,
    )
    documents = available_documents(AttachmentLibrary(store.path), mails)
    available = selectable_sources({} if is_meeting else mails, documents, messages)
    from studentflow.reply_handoff import consume_reply_handoff

    reply_handoff = consume_reply_handoff(st.session_state, available, account) if is_email else None
    if reply_handoff and not st.session_state.get(prefix + "request", "").strip():
        st.session_state["sf_assistant_request"] = DEFAULT_REPLY_REQUEST
    if is_email:
        selected_sources = _choose_sources("Information to use", available, prefix + "sources")
    else:
        chat_sources = [item for item in available if item["provider"] == "google_chat"]
        other_sources = [item for item in available if item["provider"] != "google_chat"]
        chat_key = prefix + ("sources" if is_meeting else "messages")
        other_key = prefix + ("attachments" if is_meeting else "sources")
        # Carry existing explicit selections into the new separate controls.
        old_selection = st.session_state.get(prefix + "sources", [])
        migrated_key = other_key if is_meeting else chat_key
        migrated_records = other_sources if is_meeting else chat_sources
        if migrated_key not in st.session_state:
            st.session_state[migrated_key] = [
                item["source_id"] for item in migrated_records if item["source_id"] in old_selection
            ]
        selected_sources = _choose_discussion_messages(chat_sources, chat_key, prefix)
        selected_sources += _choose_sources(
            "Supporting attachments (optional)" if is_meeting else "Information to use",
            other_sources, other_key,
        )
    if not available:
        st.caption(
            "Read a Google Chat discussion above, or keep readable meeting notes in Attachments."
            if is_meeting else "Open messages in Inbox or keep attachments to make sources available."
        )
    selected_email_count = sum(item.get("provider") == "gmail" for item in selected_sources)
    if is_email and not selected_email_count:
        st.caption(
            "To reply to an email, use Reply with assistant in Inbox to bring that email here. "
            "You can also open messages with View selected messages and choose a Gmail entry above. "
            "Saved attachments and writing samples do not include the email itself."
        )
    if selected_sources:
        with st.expander("Preview selected information"):
            for item in selected_sources:
                st.text(source_preview_heading(item))
                st.text(item["text"][:3000])
                if len(item["text"]) > 3000:
                    st.caption("Preview shortened. The assistant can read additional passages from this selected source.")
    selected_tasks = []
    include_preferences = False
    preferences, progress = {}, {}
    preference_error = None
    include_calendar = False
    selected_fixed_events = []
    references = []
    reference_error = None
    if is_general:
        selected_tasks = _choose(
            "Saved tasks to use", store.tasks(), "id", prefix + "tasks",
            lambda item: item["title"], 50,
        )
        include_preferences = st.checkbox(
            "Include my study preferences and recorded daily study minutes", key=prefix + "preferences",
        )
        if include_preferences:
            try:
                preferences = read_preferences(data_dir / "planning_preferences.json")
                progress = store.progress_by_day()
            except (OSError, ValueError, TypeError, KeyError):
                preference_error = "Study preferences could not be read. Check them in Study Plan."
                st.error(preference_error)
        include_calendar = st.checkbox(
            "Include the loaded calendar commitments", disabled=not coverage,
            key=prefix + "calendar",
        ) and bool(coverage)
        selected_fixed_events = _choose(
            "Confirmed fixed events to use", store.list_fixed_events(), "id", prefix + "fixed_events",
            lambda item: f"{item['title']} · {item['start_at']} · {item['account']}", 50,
        )
        with st.expander("Academic calendar references (optional)"):
            st.caption("Add dates from the relevant academic year to interpret phrases such as 'the Friday after reading week'.")
            rows = st.data_editor(
                st.session_state.get(prefix + "academic_base", [
                    {"Name": "", "Start": None, "End": None, "Reference": ""},
                ]),
                num_rows="dynamic", hide_index=True, key=prefix + "academic_dates",
                column_config={
                    "Name": st.column_config.TextColumn("Name", max_chars=100),
                    "Start": st.column_config.DateColumn("Start", format="YYYY-MM-DD"),
                    "End": st.column_config.DateColumn("End", format="YYYY-MM-DD"),
                    "Reference": st.column_config.TextColumn("Reference", max_chars=300),
                },
            )
            st.session_state[prefix + "academic_rows"] = rows
            try:
                references = academic_dates(rows)
            except ValueError as error:
                reference_error = str(error)
                st.warning(reference_error)
    style_samples = []
    sample, approved = "", False
    if is_email:
        with st.expander("My writing style (optional)"):
            sample = st.text_area(
                "Example from my sent mail", max_chars=6000, key="sf_assistant_style",
                help="Paste a message you wrote. Remove details that are not needed for the writing style.",
            )
            approved = st.checkbox(
                "This is my own writing and may be used for this request.", key="sf_assistant_style_consent",
            )
            if sample.strip() and approved:
                style_samples = [{
                    "source_id": source_key("writing_sample", "", sample),
                    "title": "User-selected sent-mail example", "text": sample.strip(),
                }]
    snapshot = {
        "sources": selected_sources, "tasks": selected_tasks,
        "preferences": preferences, "completed_by_day": progress,
        "calendar_events": events if include_calendar else [],
        "fixed_events": selected_fixed_events,
        "calendar_coverage": coverage if include_calendar else {},
        "academic_dates": references, "style_samples": style_samples,
        "now": datetime.now(ZONE).isoformat(),
    }
    signature = snapshot_signature(snapshot)
    request = st.text_area(
        "What would you like help with?", max_chars=4000, key=prefix + "request",
        height=180 if is_meeting else None,
        placeholder=("Draft a reply to the selected email." if is_email
                     else "Ask about the selected information."),
    )
    if is_email:
        _reply_preferences_controls(data_dir, account)
    elif is_meeting:
        st.caption("The discussion request and results stay separate from your email reply preferences.")
    consent_key = prefix + "consent_" + hashlib.sha256(
        (signature + request + model).encode()
    ).hexdigest()[:20]
    consent = st.checkbox(
        "Send this request and only the selected information to OpenAI.", key=consent_key,
    )
    if is_meeting:
        st.caption("Standard API usage charges apply. Review the summary and supporting text. Action items and follow-up messages remain suggestions.")
    elif is_email:
        st.caption("Standard API usage charges apply. Generating a reply does not send it; sending requires your separate review and Send action.")
    else:
        st.caption("Standard API usage charges apply. Review suggestions before using them. Study-time changes and fixed appointments are saved only after your review. Plans remain previews.")
    if not api_key:
        st.caption("Add an OpenAI API key in Settings to use the assistant.")
    selection_counts = ([
        f"{sum(item.get('provider') == 'google_chat' for item in selected_sources)} discussion message(s)",
        f"{sum(item.get('provider') == 'attachment' for item in selected_sources)} attachment(s)",
    ] if is_meeting else [
        f"{selected_email_count} email(s)",
        f"{len(selected_sources) - selected_email_count} other source(s)",
        f"{len(style_samples)} approved writing sample(s)",
    ])
    for count, label in (
        (len(selected_tasks), "saved task(s)"),
        (len(selected_fixed_events), "fixed event(s)"),
        (len(snapshot["calendar_events"]), "calendar commitment(s)"),
        (len(references), "academic reference(s)"),
    ):
        if count:
            selection_counts.append(f"{count} {label}")
    if include_preferences:
        selection_counts.append("study preferences and recorded daily minutes")
    st.caption("Selected for this request: " + " · ".join(selection_counts) + ".")
    if sample.strip() and not approved:
        st.warning("Your writing sample is not included. Check its permission above if you want to use it.")
    if style_samples and not selected_sources:
        st.info(
            "No email or other source is selected. Your writing sample can guide the tone, but to reply to an "
            "existing email, select that email above."
        )
    if st.button(
        "Run assistant", type="primary", key=prefix + "run",
        disabled=not api_key or not consent or not request.strip()
        or bool(preference_error or reference_error) or (is_meeting and not selected_sources),
    ):
        from studentflow.agent_tools import AssistantTools

        st.session_state.pop(prefix + "result", None)
        started = time.monotonic()
        try:
            with st.spinner("Reading the selected information and preparing suggestions..."):
                result = run_assistant(request.strip(), AssistantTools(snapshot), api_key=api_key, model=model)
            metadata = run_metadata(result, model, snapshot["now"], time.monotonic() - started)
            report_id = hashlib.sha256((prefix + signature + snapshot["now"]).encode()).hexdigest()[:12]
            st.session_state[prefix + "result"] = {
                "result": result, "signature": signature, "request": request,
                "sources": {item["source_id"]: item for item in selected_sources},
                "metadata": metadata, "id": report_id, "model": model,
            }
            try:
                data_dir.mkdir(parents=True, exist_ok=True)
                with (data_dir / "assistant_runs.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(metadata, ensure_ascii=False) + "\n")
            except OSError:
                st.warning("Suggestions are available, but their usage statistics could not be saved.")
        except Exception:  # noqa: BLE001 - provider exceptions can contain credentials.
            # SDK/provider errors may contain secrets or selected source content.
            st.error("The assistant could not complete this request. Check Settings or try a smaller selection. No records were changed.")
    report = st.session_state.get(prefix + "result")
    if report:
        stale = (report["signature"] != signature or report["request"] != request
                 or report.get("model") != model)
        if stale:
            st.warning("Your request, model or selected information changed. Run the assistant again for an up-to-date result.")
            return
        result = report["result"]
        if result.status != "completed":
            st.warning("This request was not fully completed. Review the available suggestions and any missing information.")
        st.write("**Assistant response**")
        render_response(result.answer, report["sources"])
        _render_artifacts(
            result.artifacts, report["sources"], report["id"], store=store,
            model=report["model"], selected_task_ids={item["id"] for item in selected_tasks},
            root=root, data_dir=data_dir,
            result_key=prefix + "result",
            analysis_status=result.status,
        )
        metadata = report["metadata"]
        st.caption(f"Prepared in {metadata['elapsed_seconds']} seconds. Results remain in this browser session.")
        with st.expander("Processing details"):
            st.caption("Tool names, status, safe error reasons and usage are shown. Source text and credentials are excluded from the usage log.")
            safe_events = diagnostic_events(result.events)
            if any(safe_events):
                st.dataframe(safe_events, hide_index=True)
            if metadata["usage"]:
                st.dataframe([metadata["usage"]], hide_index=True)
                if metadata["usage"].get("usage_complete") is False:
                    st.warning("Some requests did not return usage information. Reported tokens may be incomplete; check your API billing for actual usage.")
