"""User-initiated Google Chat and Calendar reads for the assistant."""

from __future__ import annotations

import os
from contextlib import closing
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from studentflow.assistant_context import source_key
from studentflow.google_workspace import (
    GoogleWorkspaceAuthError,
    GoogleWorkspaceError,
    connect_workspace,
    import_workspace_token,
)

ZONE = ZoneInfo("Asia/Singapore")
PREFIX = "sf_workspace_"


def _invalidate_workspace_requests() -> None:
    """Forget account-scoped work without erasing an unrelated email reply."""
    for prefix in ("sf_meeting_", "sf_general_"):
        for key in list(st.session_state):
            if key in {prefix + "result", prefix + "sources", prefix + "messages", prefix + "calendar"} or key.startswith(
                prefix + "consent_"
            ):
                st.session_state.pop(key, None)

    report = st.session_state.get("sf_assistant_result", {})
    sources = report.get("sources", {}) if isinstance(report, dict) else {}
    if isinstance(sources, dict):
        sources = list(sources.values())
    workspace_ids = {
        source_key("google_chat", str(message.get("account") or ""),
                   str(message.get("name") or message.get("source_id") or ""))
        for message in st.session_state.get(PREFIX + "messages", [])
    }
    workspace_ids.update(
        item.get("source_id") for item in sources if item.get("provider") == "google_chat"
    )
    selected = st.session_state.get("sf_assistant_sources", [])
    had_workspace_sources = any(identifier in workspace_ids for identifier in selected)
    used_workspace = any(item.get("provider") == "google_chat" for item in sources)
    if used_workspace or had_workspace_sources or st.session_state.get("sf_assistant_calendar"):
        st.session_state.pop("sf_assistant_result", None)
        st.session_state.pop("sf_assistant_calendar", None)
        st.session_state["sf_assistant_sources"] = [
            identifier for identifier in selected if identifier not in workspace_ids
        ]
        for key in list(st.session_state):
            if key.startswith("sf_assistant_consent_"):
                st.session_state.pop(key, None)


def clear_workspace_state() -> None:
    _invalidate_workspace_requests()
    for key in list(st.session_state):
        if key.startswith(PREFIX):
            del st.session_state[key]


def _clear_account_sources() -> None:
    _invalidate_workspace_requests()
    for suffix in ("profile", "spaces", "messages", "loaded_space", "calendars", "events", "coverage"):
        st.session_state.pop(PREFIX + suffix, None)


def _clear_loaded_discussion() -> None:
    """Never offer a previous room's messages after a switch or failed refresh."""
    st.session_state.pop(PREFIX + "messages", None)
    st.session_state.pop(PREFIX + "loaded_space", None)
    for prefix, selection in (("sf_meeting_", "sf_meeting_sources"), ("sf_general_", "sf_general_messages")):
        had_selection = bool(st.session_state.pop(selection, None))
        report = st.session_state.get(prefix + "result", {})
        sources = report.get("sources", {}) if isinstance(report, dict) else {}
        if isinstance(sources, dict):
            sources = sources.values()
        used_chat = any(item.get("provider") == "google_chat" for item in sources)
        if had_selection or used_chat:
            st.session_state.pop(prefix + "result", None)
            for key in list(st.session_state):
                if key.startswith(prefix + "consent_"):
                    st.session_state.pop(key, None)


def _retain_available_message_choices(messages: list[dict]) -> None:
    available = {
        source_key("google_chat", str(message.get("account") or ""),
                   str(message.get("name") or message.get("source_id") or ""))
        for message in messages if message.get("text")
    }
    for key in ("sf_meeting_sources", "sf_general_messages"):
        if key in st.session_state:
            st.session_state[key] = [identifier for identifier in st.session_state[key] if identifier in available]


def _account(client) -> dict:
    profile = client.profile()
    previous = st.session_state.get(PREFIX + "profile")
    if previous and previous.get("email_address") != profile.get("email_address"):
        _clear_account_sources()
    st.session_state[PREFIX + "profile"] = profile
    return profile


def render_workspace_sources(
    root: Path, data_dir: Path, *, show_chat: bool = True, show_calendar: bool = True,
) -> tuple[list[dict], list[dict], dict]:
    if not show_chat and not show_calendar:
        return [], [], {}
    client_path = Path(os.environ.get(
        "STUDENTFLOW_GMAIL_CLIENT", str(root / "credentials" / "gmail_client.json")
    ))
    token_path = data_dir / "auth" / "google_workspace_token.json"
    if not token_path.is_file() and st.session_state.get(PREFIX + "profile"):
        clear_workspace_state()
    heading = "Google Chat and Calendar" if show_chat and show_calendar else (
        "Google Chat discussions" if show_chat else "Google Calendar commitments"
    )
    description = "selected discussions and calendar commitments" if show_chat and show_calendar else (
        "selected discussions" if show_chat else "selected calendar commitments"
    )
    with st.expander(heading, expanded=False):
        st.caption(
            f"Read {description}. Loading information here "
            "does not send it to OpenAI. Choose what to include before running the assistant."
        )
        profile = st.session_state.get(PREFIX + "profile", {})
        if profile:
            st.success("Google connected: " + profile.get("email_address", "Google account"))
        elif token_path.is_file():
            st.caption("A Google sign-in is saved on this device. Load a discussion or calendar to use it.")
        st.checkbox(
            "Allow StudentFlow to read my Google Chat and Calendar.",
            key=PREFIX + "consent",
        )
        if st.button(
            "Connect Google", key=PREFIX + "connect",
            disabled=not client_path.is_file() or not st.session_state[PREFIX + "consent"],
        ):
            try:
                with st.spinner("Connecting to Google..."):
                    prior_token = root / "credentials" / "google_workspace_probe_token.json"
                    if not token_path.is_file() and prior_token.is_file():
                        try:
                            import_workspace_token(client_path, prior_token, token_path)
                        except GoogleWorkspaceError:
                            # An obsolete permission-check token must not prevent a fresh login.
                            pass
                    try:
                        with closing(connect_workspace(client_path, token_path)) as client:
                            _account(client)
                    except GoogleWorkspaceAuthError:
                        with closing(connect_workspace(
                            client_path, token_path, interactive=True,
                        )) as client:
                            _account(client)
                st.success("Google connection is ready.")
            except GoogleWorkspaceError as error:
                st.error(str(error))
            except (OSError, ValueError):
                st.error("Google sign-in could not be saved. Check the local setup and try again.")
        if token_path.is_file() and st.button(
            "Switch Google account", key=PREFIX + "switch",
            disabled=not client_path.is_file() or not st.session_state.get(PREFIX + "consent"),
        ):
            # Interactive authorization may replace the token before profile verification.
            _clear_account_sources()
            try:
                with closing(connect_workspace(client_path, token_path, interactive=True)) as client:
                    _account(client)
                st.rerun()
            except GoogleWorkspaceError as error:
                st.error(str(error))
        if token_path.is_file() and st.button("Disconnect Google", key=PREFIX + "disconnect"):
            try:
                token_path.unlink(missing_ok=True)
            except OSError:
                st.error("The saved Google sign-in could not be removed. Close other app windows and try again.")
            else:
                clear_workspace_state()
                st.rerun()
        ready = token_path.is_file() and bool(st.session_state.get(PREFIX + "consent"))
        if not client_path.is_file():
            st.info("Configure the Google desktop sign-in client in Gmail setup first.")
        if show_chat:
            if st.button("Load Google Chat spaces", disabled=not ready, key=PREFIX + "load_spaces"):
                _clear_loaded_discussion()
                st.session_state.pop(PREFIX + "spaces", None)
                try:
                    with closing(connect_workspace(client_path, token_path)) as client:
                        _account(client)
                        st.session_state[PREFIX + "spaces"] = client.list_spaces()
                except GoogleWorkspaceError as error:
                    st.error(str(error))
            spaces = st.session_state.get(PREFIX + "spaces", [])
            if spaces:
                titles = {item["name"]: item.get("display_name") or item["name"] for item in spaces}
                # Streamlit serializes selectbox choices by their displayed label.
                labels = titles if len(set(titles.values())) == len(titles) else {
                    name: f"{title} · {index}" for index, (name, title) in enumerate(titles.items(), 1)
                }
                old = st.session_state.get(PREFIX + "space")
                if old and old not in labels:
                    st.session_state.pop(PREFIX + "space", None)
                space = st.selectbox(
                    "Discussion space", list(labels), format_func=labels.get, key=PREFIX + "space",
                )
                if st.session_state.get(PREFIX + "loaded_space") != space:
                    _clear_loaded_discussion()
                if st.button("Read selected discussion", disabled=not ready, key=PREFIX + "read_chat"):
                    # A failed refresh must not leave old messages looking current.
                    st.session_state.pop(PREFIX + "messages", None)
                    try:
                        with closing(connect_workspace(client_path, token_path)) as client:
                            _account(client)
                            discussion = client.list_messages(space)
                            for message in discussion:
                                message["title"] = titles[space]
                            st.session_state[PREFIX + "messages"] = discussion
                        _retain_available_message_choices(discussion)
                        st.session_state[PREFIX + "loaded_space"] = space
                    except GoogleWorkspaceError as error:
                        _clear_loaded_discussion()
                        st.error(str(error))
            elif PREFIX + "spaces" in st.session_state:
                _clear_loaded_discussion()
                st.info("No accessible Chat spaces were found.")
            messages = st.session_state.get(PREFIX + "messages", [])
            if PREFIX + "messages" in st.session_state:
                readable = sum(bool(message.get("text")) for message in messages)
                st.caption(f"{readable} readable message(s) loaded.")
                if readable < len(messages):
                    st.warning("Some messages contain no readable text. Their attachments or cards are not analyzed.")

        if show_calendar:
            if show_chat:
                st.divider()
            if st.button("Load Google calendars", disabled=not ready, key=PREFIX + "load_calendars"):
                try:
                    with closing(connect_workspace(client_path, token_path)) as client:
                        _account(client)
                        st.session_state[PREFIX + "calendars"] = client.list_calendars()
                except GoogleWorkspaceError as error:
                    st.error(str(error))
            calendars = st.session_state.get(PREFIX + "calendars", [])
            if calendars:
                labels = {item["id"]: item.get("title") or item["id"] for item in calendars}
                st.session_state[PREFIX + "calendar_selection"] = [
                    key for key in st.session_state.get(PREFIX + "calendar_selection", []) if key in labels
                ]
                selected = st.multiselect(
                    "Calendars to read", list(labels), format_func=labels.get,
                    key=PREFIX + "calendar_selection", max_selections=5,
                )
                today = datetime.now(ZONE).date()
                left, right = st.columns(2)
                start = left.date_input("Calendar from", value=today, key=PREFIX + "from")
                end = right.date_input(
                    "Calendar through", value=today + timedelta(days=13), key=PREFIX + "through",
                )
                valid_range = 0 <= (end - start).days <= 59
                if not valid_range:
                    st.warning("Choose a calendar range of 1 to 60 days.")
                time_min = datetime.combine(start, time.min, ZONE).isoformat()
                time_max = datetime.combine(end + timedelta(days=1), time.min, ZONE).isoformat()
                selection = {"calendar_ids": selected, "time_min": time_min, "time_max": time_max}
                if st.button(
                    "Read selected calendars", disabled=not ready or not selected or not valid_range,
                    key=PREFIX + "read_calendars",
                ):
                    st.session_state.pop(PREFIX + "events", None)
                    st.session_state.pop(PREFIX + "coverage", None)
                    try:
                        with closing(connect_workspace(client_path, token_path)) as client:
                            account = _account(client)
                            events = []
                            for calendar_id in selected:
                                events.extend(client.list_events(calendar_id, time_min, time_max))
                        st.session_state[PREFIX + "events"] = events
                        st.session_state[PREFIX + "coverage"] = selection | {
                            "loaded_at": datetime.now(ZONE).isoformat(),
                            "account": account.get("email_address", ""),
                        }
                    except GoogleWorkspaceError as error:
                        st.error(str(error))
                coverage = st.session_state.get(PREFIX + "coverage", {})
                if coverage and any(coverage.get(key) != value for key, value in selection.items()):
                    st.warning("The calendar selection changed. Read it again before planning.")
                    st.session_state.pop(PREFIX + "events", None)
                    st.session_state.pop(PREFIX + "coverage", None)
            elif PREFIX + "calendars" in st.session_state:
                st.info("No accessible calendars were found.")
            events = st.session_state.get(PREFIX + "events", [])
            coverage = st.session_state.get(PREFIX + "coverage", {})
            if coverage:
                st.caption(f"{len(events)} calendar commitment(s) loaded. Times shown in Singapore time.")
                if events:
                    st.dataframe([
                        {"Event": event.get("title", "Busy"),
                         "Start": datetime.fromisoformat(event["start"]).astimezone(ZONE).strftime("%Y-%m-%d %H:%M"),
                         "End": datetime.fromisoformat(event["end"]).astimezone(ZONE).strftime("%Y-%m-%d %H:%M")}
                        for event in events
                    ], hide_index=True)
    # An account check in a later panel may have invalidated earlier panel data.
    return (
        st.session_state.get(PREFIX + "messages", []) if show_chat else [],
        st.session_state.get(PREFIX + "events", []) if show_calendar else [],
        st.session_state.get(PREFIX + "coverage", {}) if show_calendar else {},
    )
