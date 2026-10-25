"""Human-reviewed Calendar sync controls; rendering never writes to Google."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
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


def render_calendar_sync(store, root: Path, data_dir: Path, *, fixed_events: list[dict],
                         current_fingerprint: Callable[[], str],
                         saved_plan_is_fresh: Callable[[dict], bool]) -> None:
    """Show a separately authorized calendar, preview, and explicit sync action.

    Callbacks must reload current local state, not close over cached snapshots.
    The fingerprint must cover saved plan, tasks, preferences and fixed events.
    """
    with st.expander("Google Calendar sync", expanded=False):
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
        if st.button("Connect or change Calendar sync account", key=PREFIX + "connect"):
            st.session_state.pop(PREFIX + "preview", None)
            try:
                with closing(connect_workspace(
                    client_path, token_path, interactive=True,
                    required_scopes=CALENDAR_SYNC_SCOPES,
                )) as client:
                    st.session_state[PREFIX + "account"] = client.profile()["email_address"]
                st.success("Calendar sync permission is ready. Preview the changes below.")
            except GoogleWorkspaceError as error:
                st.error(str(error))
        if not token_path.is_file():
            st.session_state.pop(PREFIX + "preview", None)
            return
        account = st.session_state.get(PREFIX + "account")
        if account:
            st.caption("Calendar account: " + account)
        if st.button("Disconnect Calendar sync", key=PREFIX + "disconnect"):
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
            if st.button("Preview Calendar changes", key=PREFIX + "preview_button"):
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
            st.write("Sync to: **" + preview["account"] + "** · StudentFlow calendar")
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
            st.caption(
                f"{counts['create']} to add · {counts['update']} to update · "
                f"{counts['remove']} future events to remove"
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
            st.dataframe(rows, hide_index=True, width="stretch")
            confirmed = st.checkbox(
                "I have reviewed these changes to my StudentFlow calendar.",
                key=PREFIX + "review_" + preview["signature"],
            )
            if st.button("Sync reviewed changes", disabled=not confirmed, type="primary",
                         key=PREFIX + "apply_" + preview["signature"]):
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
