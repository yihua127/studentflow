"""Human-reviewed Calendar sync controls; rendering never writes to Google."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from studentflow.calendar_sync import CalendarProvider, apply_sync, prepare_sync
from studentflow.google_workspace import (
    CALENDAR_SYNC_SCOPES,
    GoogleWorkspaceError,
    connect_workspace,
)

PREFIX = "sf_calendar_sync_"
ZONE = ZoneInfo("Asia/Singapore")


def _display_time(value: dict) -> str:
    return datetime.fromisoformat(value["dateTime"]).astimezone(ZONE).strftime("%d %b %Y %H:%M")


def _keep_open() -> None:
    # Notices above this section come and go between reruns, which remounts the
    # expander collapsed and hides the preview or error the user just requested.
    st.session_state[PREFIX + "open"] = True


def render_calendar_sync(store, root: Path, data_dir: Path, *, fixed_events: list[dict],
                         current_fingerprint: Callable[[], str],
                         saved_plan_is_fresh: Callable[[dict], bool]) -> None:
    """Show a separately authorized calendar, preview, and explicit sync action.

    Callbacks must reload current local state, not close over cached snapshots.
    The fingerprint must cover saved plan, tasks, preferences and fixed events.
    """
    with st.expander("Google Calendar sync", expanded=st.session_state.get(PREFIX + "open", False)):
        st.caption(
            "Sync your saved study plan and confirmed fixed events to a separate StudentFlow "
            "calendar. Changes flow from StudentFlow to Google; edits in Google are not imported. "
            "Past events are kept; no invitations are sent."
        )
        client_path = Path(os.environ.get(
            "STUDENTFLOW_GMAIL_CLIENT", str(Path(root) / "credentials" / "gmail_client.json")
        ))
        token_path = Path(data_dir) / "auth" / "google_calendar_sync_token.json"
        st.caption(
            "Calendar sync needs permission to create and manage StudentFlow's calendar. "
            "Your existing read-only Google connection stays separate."
        )
        if not client_path.is_file():
            st.info("Configure the Google desktop sign-in client in Gmail setup first.")
            return
        if st.button(
            "Connect or change Calendar sync account", key=PREFIX + "connect", width="content",
            on_click=_keep_open,
        ):
            st.session_state.pop(PREFIX + "preview", None)
            login_link = st.empty()
            try:
                with closing(connect_workspace(
                    client_path, token_path, interactive=True,
                    required_scopes=CALENDAR_SYNC_SCOPES,
                    show_login_link=lambda url: login_link.link_button("Open Google sign-in", url),
                )) as client:
                    st.session_state[PREFIX + "account"] = client.profile()["email_address"]
                st.success("Calendar sync permission is ready. Preview the changes below.")
            except GoogleWorkspaceError as error:
                st.error(str(error))
            finally:
                login_link.empty()
        if not token_path.is_file():
            st.session_state.pop(PREFIX + "preview", None)
            return
        account = st.session_state.get(PREFIX + "account")
        if account:
            st.html(
                '<div class="sf-workspace-account">'
                '<span class="sf-pill sf-pill-neutral">Calendar account</span>'
                f'<span>{escape(account)}</span></div>'
            )
        else:
            st.caption("Calendar sign-in saved. Preview changes to check the connected account.")
        if st.button(
            "Disconnect Calendar sync", key=PREFIX + "disconnect", width="stretch",
            on_click=_keep_open,
        ):
            try:
                token_path.unlink(missing_ok=True)
                for key in list(st.session_state):
                    if key.startswith(PREFIX):
                        st.session_state.pop(key, None)
                st.rerun()
            except OSError:
                st.error("The saved Calendar connection could not be removed. Try again.")
            return
        try:
            st.divider()
            st.html('<div class="sf-action-step">2 · Prepare a preview</div>')
            plan = store.latest_plan()
            fingerprint = current_fingerprint()
            fresh = not plan or saved_plan_is_fresh(plan)
            if not fresh:
                st.info(
                    "The saved study plan is out of date. You can sync fixed events now; "
                    "save a fresh plan to sync study sessions."
                )
            if not fresh or not plan:
                st.session_state[PREFIX + "include_plan"] = False
            include_plan = st.checkbox(
                "Include saved study sessions", value=bool(plan) and fresh,
                disabled=not fresh or not plan, key=PREFIX + "include_plan",
            )
            if st.button("Preview Calendar changes", key=PREFIX + "preview_button", width="stretch",
                         on_click=_keep_open):
                st.session_state.pop(PREFIX + "preview", None)
                with (
                    st.spinner("Checking the StudentFlow calendar..."),
                    closing(connect_workspace(
                        client_path, token_path, required_scopes=CALENDAR_SYNC_SCOPES,
                    )) as client,
                ):
                    preview = prepare_sync(
                        CalendarProvider(client), data_dir, plan, fixed_events,
                        fingerprint=fingerprint, plan_is_fresh=fresh,
                        include_study_plan=include_plan,
                    )
                st.session_state[PREFIX + "preview"] = preview
                st.session_state[PREFIX + "account"] = preview["account"]
            preview = st.session_state.get(PREFIX + "preview")
            if not preview:
                return
            if (preview["fingerprint"] != fingerprint
                    or preview["include_study_plan"] != include_plan
                    or (include_plan and not fresh)):
                st.session_state.pop(PREFIX + "preview", None)
                st.info("The saved plan or events changed. Preview Calendar changes again.")
                return
            st.html('<div class="sf-action-step">3 · Review and sync</div>')
            st.html(
                '<dl class="sf-review-meta">'
                '<dt>Calendar</dt><dd>StudentFlow</dd>'
                f'<dt>Account</dt><dd>{escape(preview["account"])}</dd></dl>'
            )
            if preview.get("excluded_fixed_count"):
                st.caption(
                    f"{preview['excluded_fixed_count']} confirmed fixed event(s) belong to "
                    "another account and are not included."
                )
            if preview["create_calendar"]:
                st.caption("A separate StudentFlow calendar will be created when you sync.")
            operations = preview["operations"]
            counts = {action: sum(op["action"] == action for op in operations)
                      for action in ("create", "update", "remove")}
            st.html(
                '<div class="sf-artifact-counts">' + ''.join(
                    '<div class="sf-artifact-count">'
                    f'<strong>{counts[action]}</strong><span>{label}</span></div>'
                    for action, label in (
                        ("create", "to add"), ("update", "to update"),
                        ("remove", "future events to remove"),
                    )
                ) + '</div>'
            )
            if not operations:
                st.success("No calendar changes are needed.")
                return
            rows = []
            for operation in operations:
                event = operation["event"]
                previous = operation.get("previous") if operation["action"] == "update" else None
                rows.append({
                    "Action": operation["action"].capitalize(), "Event": event["summary"],
                    "Start (Singapore)": _display_time(event["start"]),
                    "End (Singapore)": _display_time(event["end"]),
                    "Previous start": _display_time(previous["start"]) if previous else "",
                    "Previous end": _display_time(previous["end"]) if previous else "",
                })
            with st.container(border=True, key="sf_panel_calendar_preview"):
                st.markdown("**Changes to review**")
                st.dataframe(rows, hide_index=True, width="stretch")
            review_column, sync_column = st.columns([2, 1], gap="medium", vertical_alignment="center")
            confirmed = review_column.checkbox(
                "I have reviewed these changes to my StudentFlow calendar.",
                key=PREFIX + "review_" + preview["signature"], on_change=_keep_open,
            )
            if sync_column.button("Sync reviewed changes", disabled=not confirmed, type="primary",
                                  key=PREFIX + "apply_" + preview["signature"], width="stretch",
                                  on_click=_keep_open):
                with (
                    st.spinner("Syncing the reviewed calendar changes..."),
                    closing(connect_workspace(
                        client_path, token_path, required_scopes=CALENDAR_SYNC_SCOPES,
                    )) as client,
                ):
                    apply_sync(
                        CalendarProvider(client), data_dir, preview,
                        current_fingerprint=current_fingerprint, load_plan=store.latest_plan,
                        saved_plan_is_fresh=saved_plan_is_fresh,
                    )
                st.session_state.pop(PREFIX + "preview", None)
                st.success("Calendar sync finished. Open Google Calendar to view StudentFlow.")
                st.link_button("Open Google Calendar", "https://calendar.google.com/calendar/u/0/r")
        except GoogleWorkspaceError as error:
            st.session_state.pop(PREFIX + "preview", None)
            st.error(str(error))
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            st.session_state.pop(PREFIX + "preview", None)
            st.error("Calendar sync could not finish. Check local settings and preview again.")
