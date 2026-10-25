"""Focused Google panels keep account state separate from email reply work."""

from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from studentflow import workspace_ui
from studentflow.assistant_context import source_key
from studentflow.google_workspace import GoogleWorkspaceAuthError, GoogleWorkspaceError

SCRIPT = """
import os
from pathlib import Path
import streamlit as st
from studentflow.workspace_ui import render_workspace_sources
root = Path(os.environ['SF_WORKSPACE_PANEL_ROOT'])
# The containing Assistant page keeps hidden workspace widgets alive on mode changes.
for key in list(st.session_state):
    if key.startswith('sf_workspace_') and key not in {
        'sf_workspace_connect', 'sf_workspace_switch', 'sf_workspace_disconnect',
        'sf_workspace_load_spaces', 'sf_workspace_read_chat',
        'sf_workspace_load_calendars', 'sf_workspace_read_calendars',
    }:
        st.session_state[key] = st.session_state[key]
mode = st.radio('Panel', ['both', 'chat', 'calendar', 'none'], key='test_panel')
messages, events, coverage = render_workspace_sources(
    root, root / 'data', show_chat=mode in {'both', 'chat'},
    show_calendar=mode in {'both', 'calendar'},
)
st.session_state['panel_result'] = (messages, events, coverage)
"""


def checked(app):
    app.run(timeout=30)
    assert not app.exception
    return app


@pytest.fixture
def panel(tmp_path, monkeypatch):
    monkeypatch.setenv("SF_WORKSPACE_PANEL_ROOT", str(tmp_path))
    client_path = tmp_path / "client.json"
    client_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("STUDENTFLOW_GMAIL_CLIENT", str(client_path))
    token_path = tmp_path / "data" / "auth" / "google_workspace_token.json"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("{}", encoding="utf-8")
    calls = []

    class Client:
        def close(self):
            pass

        def profile(self):
            return {"email_address": "student@example.invalid"}

        def list_spaces(self):
            calls.append("spaces")
            return [{"name": "spaces/test", "display_name": "StudentFlow Test"}]

        def list_messages(self, space):
            calls.append(("messages", space))
            return [{"name": "spaces/test/messages/one", "account": "student@example.invalid",
                     "text": "Alex will review the report."}]

        def list_calendars(self):
            calls.append("calendars")
            return [{"id": "primary", "title": "Personal calendar"}]

        def list_events(self, calendar_id, time_min, time_max):
            calls.append(("events", calendar_id))
            return [{"id": "one", "title": "Project meeting",
                     "start": "2030-01-02T10:00:00+08:00", "end": "2030-01-02T11:00:00+08:00"}]

    def connect(*args, **kwargs):
        calls.append("connect")
        return Client()

    monkeypatch.setattr(workspace_ui, "connect_workspace", connect)
    return calls, token_path


def test_chat_panel_reads_discussion_without_showing_calendar_controls(panel):
    calls, _ = panel
    app = AppTest.from_string(SCRIPT)
    app.session_state["test_panel"] = "chat"
    checked(app)
    assert [item.label for item in app.expander] == ["Google Chat discussions"]
    assert not any(item.label == "Load Google calendars" for item in app.button)
    assert not calls
    app.checkbox(key="sf_workspace_consent").check()
    checked(app)
    app.button(key="sf_workspace_load_spaces").click()
    checked(app)
    assert app.selectbox(key="sf_workspace_space").value == "spaces/test"
    app.button(key="sf_workspace_read_chat").click()
    checked(app)
    messages, events, coverage = app.session_state["panel_result"]
    assert messages[0]["title"] == "StudentFlow Test"
    assert events == [] and coverage == {}
    assert calls == ["connect", "spaces", "connect", ("messages", "spaces/test")]


def test_default_panel_still_has_chat_and_calendar(panel):
    calls, _ = panel
    app = checked(AppTest.from_string(SCRIPT))
    assert [item.label for item in app.expander] == ["Google Chat and Calendar"]
    assert any(item.label == "Load Google Chat spaces" for item in app.button)
    assert any(item.label == "Load Google calendars" for item in app.button)
    assert not calls


def test_calendar_panel_excludes_cached_chat_data_and_hidden_panel_returns_nothing(panel):
    calls, _ = panel
    app = AppTest.from_string(SCRIPT)
    app.session_state["test_panel"] = "calendar"
    app.session_state["sf_workspace_messages"] = [{"text": "Do not include hidden chat."}]
    checked(app)
    assert [item.label for item in app.expander] == ["Google Calendar commitments"]
    assert not any(item.label == "Load Google Chat spaces" for item in app.button)
    assert app.session_state["panel_result"] == ([], [], {})
    app.radio(key="test_panel").set_value("none")
    checked(app)
    assert not app.expander
    assert app.session_state["panel_result"] == ([], [], {})
    assert not calls


def test_changing_panels_keeps_calendar_choices_and_consent_without_leaking_events(panel):
    calls, _ = panel
    app = checked(AppTest.from_string(SCRIPT))
    app.checkbox(key="sf_workspace_consent").check()
    checked(app)
    app.button(key="sf_workspace_load_calendars").click()
    checked(app)
    app.multiselect(key="sf_workspace_calendar_selection").set_value(["primary"])
    checked(app)
    app.button(key="sf_workspace_read_calendars").click()
    checked(app)
    old_events = app.session_state["sf_workspace_events"]
    old_coverage = app.session_state["sf_workspace_coverage"]
    assert len(old_events) == 1 and old_coverage
    old_calls = list(calls)
    app.radio(key="test_panel").set_value("chat")
    checked(app)
    assert app.checkbox(key="sf_workspace_consent").value
    assert app.session_state["panel_result"] == ([], [], {})
    assert app.session_state["sf_workspace_events"] == old_events
    assert app.session_state["sf_workspace_coverage"] == old_coverage
    app.radio(key="test_panel").set_value("both")
    checked(app)
    assert app.multiselect(key="sf_workspace_calendar_selection").value == ["primary"]
    assert app.session_state["panel_result"][1:] == (old_events, old_coverage)
    assert calls == old_calls


def request_state():
    return {
        "sf_workspace_profile": {"email_address": "old@example.invalid"},
        "sf_workspace_messages": [{"name": "spaces/test/messages/one", "account": "old@example.invalid"}],
        "sf_workspace_events": [{"id": "private"}],
        "sf_workspace_coverage": {"account": "old@example.invalid"},
        "sf_meeting_sources": ["chat"], "sf_meeting_result": {"private": "discussion"},
        "sf_meeting_consent_123": True, "sf_meeting_request": "Summarize my discussion.",
        "sf_meeting_attachments": ["independent-document"],
        "sf_general_sources": ["independent-general-document"], "sf_general_result": {"private": "calendar"},
        "sf_general_messages": ["chat"],
        "sf_general_consent_456": True, "sf_general_calendar": True,
        "sf_general_request": "Help with my schedule.",
        "sf_assistant_sources": ["email"],
        "sf_assistant_result": {"sources": {"email": {"provider": "gmail"}}, "draft": "Keep my reply"},
        "sf_assistant_request": "Draft a reply.", "sf_assistant_style": "Best regards, Student",
        "sf_assistant_consent_789": True,
    }


def test_disconnect_clears_workspace_results_but_preserves_email_and_mode_requests(monkeypatch):
    state = request_state()
    monkeypatch.setattr(workspace_ui, "st", SimpleNamespace(session_state=state))
    workspace_ui.clear_workspace_state()
    assert not any(key.startswith("sf_workspace_") for key in state)
    assert not any(key.endswith("result") for key in state if key.startswith(("sf_meeting_", "sf_general_")))
    assert not any("consent_" in key for key in state if key.startswith(("sf_meeting_", "sf_general_")))
    assert state["sf_meeting_request"] == "Summarize my discussion."
    assert state["sf_meeting_attachments"] == ["independent-document"]
    assert "sf_general_messages" not in state
    assert state["sf_general_request"] == "Help with my schedule."
    assert state["sf_assistant_result"]["draft"] == "Keep my reply"
    assert state["sf_assistant_sources"] == ["email"]
    assert state["sf_assistant_consent_789"]


def test_account_switch_invalidates_meeting_and_general_requests_but_keeps_email(monkeypatch):
    state = request_state()
    monkeypatch.setattr(workspace_ui, "st", SimpleNamespace(session_state=state))
    workspace_ui._account(SimpleNamespace(profile=lambda: {"email_address": "new@example.invalid"}))
    assert state["sf_workspace_profile"] == {"email_address": "new@example.invalid"}
    assert "sf_workspace_messages" not in state
    assert "sf_workspace_coverage" not in state
    assert "sf_meeting_sources" not in state and "sf_general_sources" not in state
    assert "sf_general_messages" not in state
    assert state["sf_meeting_attachments"] == ["independent-document"]
    assert "sf_meeting_result" not in state and "sf_general_result" not in state
    assert state["sf_assistant_result"]["draft"] == "Keep my reply"


@pytest.mark.parametrize("legacy_workspace", ["chat", "calendar", "selected_chat"])
def test_disconnect_also_invalidates_legacy_mixed_assistant_reports(monkeypatch, legacy_workspace):
    state = request_state()
    chat_id = source_key("google_chat", "old@example.invalid", "spaces/test/messages/one")
    if legacy_workspace == "chat":
        state["sf_assistant_result"]["sources"][chat_id] = {"provider": "google_chat", "source_id": chat_id}
    elif legacy_workspace == "selected_chat":
        state["sf_assistant_sources"].append(chat_id)
    else:
        state["sf_assistant_calendar"] = True
    monkeypatch.setattr(workspace_ui, "st", SimpleNamespace(session_state=state))
    workspace_ui.clear_workspace_state()
    assert "sf_assistant_result" not in state
    assert "sf_assistant_consent_789" not in state
    assert state["sf_assistant_sources"] == ["email"]
    assert state["sf_assistant_request"] == "Draft a reply."
    assert state["sf_assistant_style"] == "Best regards, Student"


def test_same_account_read_does_not_clear_previously_reviewed_reports(monkeypatch):
    state = request_state()
    monkeypatch.setattr(workspace_ui, "st", SimpleNamespace(session_state=state))
    workspace_ui._account(SimpleNamespace(profile=lambda: {"email_address": "old@example.invalid"}))
    assert state["sf_meeting_result"] and state["sf_general_result"] and state["sf_assistant_result"]


def loaded_room_app():
    app = AppTest.from_string(SCRIPT)
    values = request_state()
    values.update({
        "test_panel": "chat", "sf_workspace_consent": True,
        "sf_workspace_profile": {"email_address": "student@example.invalid"},
        "sf_workspace_spaces": [{"name": "spaces/one", "display_name": "First room"},
                                {"name": "spaces/two", "display_name": "Second room"}],
        "sf_workspace_space": "spaces/one", "sf_workspace_loaded_space": "spaces/one",
        "sf_workspace_messages": [{"name": "spaces/one/messages/old", "text": "First-room private message",
                                   "account": "student@example.invalid"}],
    })
    for key, value in values.items():
        app.session_state[key] = value
    checked(app)
    assert app.session_state["panel_result"][0]
    return app


def assert_old_discussion_removed(app):
    assert app.session_state["panel_result"][0] == []
    for key in ("sf_workspace_messages", "sf_workspace_loaded_space", "sf_meeting_sources",
                "sf_general_messages", "sf_meeting_result", "sf_general_result"):
        assert key not in app.session_state
    assert app.session_state["sf_meeting_attachments"] == ["independent-document"]
    assert app.session_state["sf_assistant_result"]["draft"] == "Keep my reply"


def test_room_switch_clears_messages_and_selections_before_reading_new_room(panel):
    calls, _ = panel
    app = loaded_room_app()
    app.selectbox(key="sf_workspace_space").set_value("spaces/two")
    checked(app)
    assert_old_discussion_removed(app)
    assert not calls
    assert app.session_state["sf_general_sources"] == ["independent-general-document"]


@pytest.mark.parametrize("refresh_result", ["empty", "failure"])
def test_space_refresh_cannot_reuse_previous_room_when_empty_or_failed(panel, monkeypatch, refresh_result):
    app = loaded_room_app()

    def list_spaces():
        if refresh_result == "failure":
            raise GoogleWorkspaceError("The spaces could not be read.")
        return []

    client = SimpleNamespace(
        close=lambda: None, profile=lambda: {"email_address": "student@example.invalid"},
        list_spaces=list_spaces,
    )
    monkeypatch.setattr(workspace_ui, "connect_workspace", lambda *args, **kwargs: client)
    app.button(key="sf_workspace_load_spaces").click()
    checked(app)
    assert_old_discussion_removed(app)
    assert not any(item.label == "Discussion space" for item in app.selectbox)
    if refresh_result == "empty":
        assert any("No accessible Chat spaces" in item.value for item in app.info)
    else:
        assert any("could not be read" in item.value for item in app.error)


def test_failed_message_refresh_removes_previous_messages_and_read_metadata(panel, monkeypatch):
    app = loaded_room_app()

    def list_messages(space):
        raise GoogleWorkspaceError("The discussion could not be read.")

    client = SimpleNamespace(
        close=lambda: None, profile=lambda: {"email_address": "student@example.invalid"},
        list_messages=list_messages,
    )
    monkeypatch.setattr(workspace_ui, "connect_workspace", lambda *args, **kwargs: client)
    app.button(key="sf_workspace_read_chat").click()
    checked(app)
    assert_old_discussion_removed(app)
    assert any("could not be read" in item.value for item in app.error)


def test_successful_same_room_refresh_preserves_existing_choices_and_updates_messages(panel, monkeypatch):
    app = loaded_room_app()
    old_id = source_key("google_chat", "student@example.invalid", "spaces/one/messages/old")
    removed_id = source_key("google_chat", "student@example.invalid", "spaces/one/messages/removed")
    app.session_state["sf_meeting_sources"] = [old_id, removed_id]
    app.session_state["sf_general_messages"] = [old_id]
    refreshed = [
        {"name": "spaces/one/messages/old", "text": "Updated action deadline.",
         "account": "student@example.invalid"},
        {"name": "spaces/one/messages/new", "text": "A new discussion message.",
         "account": "student@example.invalid"},
    ]
    client = SimpleNamespace(
        close=lambda: None, profile=lambda: {"email_address": "student@example.invalid"},
        list_messages=lambda space: refreshed,
    )
    monkeypatch.setattr(workspace_ui, "connect_workspace", lambda *args, **kwargs: client)
    app.button(key="sf_workspace_read_chat").click()
    checked(app)
    assert app.session_state["sf_meeting_sources"] == [old_id]
    assert app.session_state["sf_general_messages"] == [old_id]
    assert [item["text"] for item in app.session_state["panel_result"][0]] == [
        "Updated action deadline.", "A new discussion message.",
    ]
    assert app.session_state["sf_workspace_loaded_space"] == "spaces/one"
    assert app.session_state["sf_meeting_attachments"] == ["independent-document"]


def test_duplicate_room_titles_keep_second_room_selected_across_reruns(panel):
    app = loaded_room_app()
    app.session_state["sf_workspace_spaces"] = [
        {"name": "spaces/one", "display_name": "Project group"},
        {"name": "spaces/two", "display_name": "Project group"},
    ]
    checked(app)
    assert app.selectbox(key="sf_workspace_space").options == ["Project group · 1", "Project group · 2"]
    app.selectbox(key="sf_workspace_space").set_value("spaces/two")
    checked(app)
    assert app.selectbox(key="sf_workspace_space").value == "spaces/two"
    checked(app)
    assert app.selectbox(key="sf_workspace_space").value == "spaces/two"
    assert app.session_state["panel_result"][0] == []


def test_failed_account_switch_cannot_offer_previous_account_messages(panel, monkeypatch):
    app = loaded_room_app()

    def reject(*args, **kwargs):
        raise GoogleWorkspaceAuthError("The new account could not be verified.")

    monkeypatch.setattr(workspace_ui, "connect_workspace", reject)
    app.button(key="sf_workspace_switch").click()
    checked(app)
    assert_old_discussion_removed(app)
    assert "sf_workspace_profile" not in app.session_state
    assert "sf_workspace_events" not in app.session_state
    assert any("could not be verified" in item.value for item in app.error)
