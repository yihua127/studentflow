"""One-selection Inbox replies stay scoped and require a separate model run."""

from email.message import EmailMessage
from pathlib import Path

import dotenv
import pytest
from streamlit.testing.v1 import AppTest

from studentflow import assistant_ui, batch_ui, gmail
from studentflow.assistant_context import selectable_sources
from studentflow.reply_handoff import (
    HANDOFF_KEY,
    clear_reply_handoff,
    consume_reply_handoff,
    queue_reply_handoff,
)

ACCOUNT = "student@example.com"
APP = Path(__file__).resolve().parents[1] / "app.py"


def mail_record(account=ACCOUNT):
    return {
        "provider": "gmail", "gmail_account": account, "gmail_id": "remote-a",
        "subject": "Assignment A", "body": "Please review the brief.",
        "sender": "teacher@example.com",
    }


def test_explicit_handoff_replaces_only_sources_preserves_request_and_style():
    mails = {"local-a": mail_record(), "local-b": mail_record()}
    state = {
        "sf_assistant_sources": ["old-selection"], "sf_assistant_result": {"old": True},
        "sf_assistant_request": "My preferred reply request",
        "sf_assistant_style": "My saved style", "sf_assistant_consent_previous": True,
    }
    handoff = queue_reply_handoff(state, ACCOUNT.upper(), "local-a", mails)
    assert state["sf_assistant_sources"] == ["old-selection"]
    assert state["sf_app_pending_tab"] == "Assistant"
    assert state["sf_assistant_workspace"] == "Email replies"
    assert consume_reply_handoff(state, selectable_sources(mails, [], []), ACCOUNT) == handoff
    assert state["sf_assistant_sources"] == [handoff["source_id"]]
    assert "sf_assistant_result" not in state
    assert "sf_assistant_consent_previous" not in state
    assert state["sf_assistant_request"] == "My preferred reply request"
    assert state["sf_assistant_style"] == "My saved style"
    assert consume_reply_handoff(state, selectable_sources(mails, [], []), ACCOUNT) is None


@pytest.mark.parametrize("account,available", [
    ("other@example.com", True), ("", True), (ACCOUNT, False),
])
def test_stale_handoff_cannot_restore_mail_or_previous_reply(account, available):
    mails = {"local-a": mail_record()}
    state = {"sf_assistant_sources": ["old"], "sf_assistant_result": {"old": True}}
    queue_reply_handoff(state, ACCOUNT, "local-a", mails)
    sources = selectable_sources(mails, [], []) if available else []
    assert consume_reply_handoff(state, sources, account) is None
    assert HANDOFF_KEY not in state
    assert "sf_assistant_sources" not in state
    assert "sf_assistant_result" not in state
    assert "sf_app_pending_tab" not in state


@pytest.mark.parametrize("account,identifier,record", [
    ("other@example.com", "local-a", mail_record()),
    ("", "local-a", mail_record()),
    (ACCOUNT, "missing", mail_record()),
    (ACCOUNT, "local-a", {**mail_record(), "provider": "attachment"}),
])
def test_queue_rejects_missing_or_wrong_account_mail(account, identifier, record):
    state = {}
    with pytest.raises(ValueError, match="no longer available"):
        queue_reply_handoff(state, account, identifier, {"local-a": record})
    assert state == {}


def test_disconnect_clears_transfer_without_erasing_user_writing_preferences():
    state = {
        HANDOFF_KEY: {}, "sf_app_pending_tab": "Assistant", "sf_assistant_sources": ["source"],
        "sf_assistant_result": {}, "sf_assistant_consent_abc": True,
        "sf_assistant_request": "Reply politely", "sf_assistant_style": "Best regards",
    }
    clear_reply_handoff(state)
    assert state == {"sf_assistant_request": "Reply politely", "sf_assistant_style": "Best regards"}


def test_reply_shortcut_from_meetings_preserves_meeting_inputs_and_results():
    mails = {"local-a": mail_record()}
    meeting_state = {
        "sf_meeting_request": "Summarize our discussion",
        "sf_meeting_sources": ["chat-source"],
        "sf_meeting_result": {"summary": "Existing discussion summary"},
        "sf_meeting_consent_current": True,
    }
    state = {
        **meeting_state,
        "sf_assistant_workspace": "Meetings & discussions",
        "sf_assistant_request": "Draft my preferred reply",
        "sf_assistant_style": "Best regards, Student",
    }
    handoff = queue_reply_handoff(state, ACCOUNT, "local-a", mails)
    assert state["sf_assistant_workspace"] == "Email replies"
    assert state["sf_app_pending_tab"] == "Assistant"
    assert consume_reply_handoff(state, selectable_sources(mails, [], []), ACCOUNT) == handoff
    assert state["sf_assistant_sources"] == [handoff["source_id"]]
    assert state["sf_assistant_request"] == "Draft my preferred reply"
    assert state["sf_assistant_style"] == "Best regards, Student"
    assert {key: state[key] for key in meeting_state} == meeting_state


def test_gmail_change_invalidates_general_mail_context_without_erasing_meetings():
    retained = {
        "sf_assistant_workspace": "Meetings & discussions",
        "sf_meeting_request": "Summarize our discussion",
        "sf_meeting_sources": ["chat-source"],
        "sf_meeting_result": {"summary": "Existing discussion summary"},
        "sf_meeting_consent_current": True,
        "sf_general_request": "My reusable request",
        "sf_assistant_style": "Best regards, Student",
    }
    state = {
        **retained,
        HANDOFF_KEY: {}, "sf_app_pending_tab": "Assistant",
        "sf_assistant_sources": ["old-mail"], "sf_assistant_result": {"old": True},
        "sf_assistant_consent_previous": True,
        "sf_general_sources": ["old-mail"], "sf_general_result": {"old": True},
        "sf_general_consent_previous": True,
    }
    clear_reply_handoff(state)
    assert state == retained


def app_fixture(tmp_path, monkeypatch, *, fetch_error=None, initial_state=None):
    client = tmp_path / "client.json"
    client.write_text("{}", encoding="utf-8")
    data = tmp_path / "records"
    (data / "auth").mkdir(parents=True)
    (data / "auth" / "gmail_token.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("STUDENTFLOW_DATA_DIR", str(data))
    monkeypatch.setenv("STUDENTFLOW_GMAIL_CLIENT", str(client))
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("OPENAI_MODEL", "")
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *_args, **_kwargs: {})
    calls = []

    class Mailbox:
        def profile(self):
            return {"email_address": ACCOUNT}

        def fetch_eml(self, identifier):
            calls.append(identifier)
            if fetch_error:
                raise fetch_error
            message = EmailMessage()
            message["Subject"] = "Assignment " + identifier
            message["From"] = "teacher@example.com"
            message["Message-ID"] = f"<{identifier}@example.com>"
            message.set_content("Please review the brief.")
            return message.as_bytes()

        def close(self):
            pass

    def no_model(*_args, **_kwargs):
        raise AssertionError("Selecting a reply must not run an agent.")

    monkeypatch.setattr(gmail, "connect", lambda *_args, **_kwargs: Mailbox())
    monkeypatch.setattr(batch_ui, "connect", lambda *_args, **_kwargs: Mailbox())
    monkeypatch.setattr(assistant_ui, "run_assistant", no_model)
    app = AppTest.from_file(str(APP))
    app.session_state["gmail_restore_attempted"] = True
    app.session_state["gmail_profile"] = {"email_address": ACCOUNT}
    app.session_state["gmail_headers"] = [
        {"id": identifier, "subject": "Assignment " + identifier, "sender": "teacher@example.com"}
        for identifier in ("a", "b")
    ]
    for key, value in (initial_state or {}).items():
        app.session_state[key] = value
    app.run(timeout=30)
    assert not app.exception
    return app, calls


def test_inbox_reply_opens_selected_message_and_selects_it_without_second_selection(tmp_path, monkeypatch):
    app, calls = app_fixture(tmp_path, monkeypatch)
    assert app.button(key="batch_reply").disabled
    app.multiselect(key="batch_selected").set_value(["a"]).run(timeout=30)
    assert app.multiselect(key="sf_assistant_sources").value == []
    assert calls == []
    assert not app.button(key="batch_reply").disabled
    app.button(key="batch_reply").click().run(timeout=30)
    assert not app.exception
    assert calls == ["a"]
    assert app.session_state["sf_app_tab"] == "Assistant"
    chosen = app.multiselect(key="sf_assistant_sources").value
    assert len(chosen) == 1
    assert app.session_state["sf_assistant_request"]
    assert not any(item.value for item in app.checkbox if (item.key or "").startswith("sf_assistant_consent_"))
    app.run(timeout=30)
    assert calls == ["a"]
    assert app.multiselect(key="sf_assistant_sources").value == chosen


def test_inbox_reply_switches_meeting_workspace_to_email_without_running_agent(tmp_path, monkeypatch):
    app, calls = app_fixture(tmp_path, monkeypatch, initial_state={
        "sf_assistant_workspace": "Meetings & discussions",
        "sf_meeting_request": "Keep this discussion request",
    })
    app.multiselect(key="batch_selected").set_value(["a"]).run(timeout=30)
    app.button(key="batch_reply").click().run(timeout=30)
    assert not app.exception
    assert calls == ["a"]
    assert app.session_state["sf_app_tab"] == "Assistant"
    assert app.session_state["sf_assistant_workspace"] == "Email replies"
    assert app.session_state["sf_meeting_request"] == "Keep this discussion request"
    assert len(app.multiselect(key="sf_assistant_sources").value) == 1


def test_reply_requires_one_email_and_fetch_failure_does_not_navigate(tmp_path, monkeypatch):
    app, calls = app_fixture(tmp_path, monkeypatch, fetch_error=RuntimeError("private-provider-details"))
    app.multiselect(key="batch_selected").set_value(["a", "b"]).run(timeout=30)
    assert app.button(key="batch_reply").disabled
    app.multiselect(key="batch_selected").set_value(["a"]).run(timeout=30)
    app.button(key="batch_reply").click().run(timeout=30)
    assert not app.exception
    assert calls == ["a"]
    assert app.session_state["sf_app_tab"] == "Inbox (Gmail)"
    assert app.multiselect(key="sf_assistant_sources").value == []
    assert any("could not be opened" in item.value for item in app.error)
    assert all("private-provider-details" not in item.value for item in app.error)


def test_disconnect_removes_handoff_sources_and_old_reply(tmp_path, monkeypatch):
    app, _calls = app_fixture(tmp_path, monkeypatch)
    app.multiselect(key="batch_selected").set_value(["a"]).run(timeout=30)
    app.button(key="batch_reply").click().run(timeout=30)
    assert app.multiselect(key="sf_assistant_sources").value
    app.session_state[HANDOFF_KEY] = {"account": ACCOUNT, "source_id": "old", "mail_id": "old"}
    next(item for item in app.button if item.label == "Disconnect Gmail").click().run(timeout=30)
    assert not app.exception
    assert app.multiselect(key="sf_assistant_sources").value == []
    assert HANDOFF_KEY not in app.session_state
    assert "sf_assistant_result" not in app.session_state
