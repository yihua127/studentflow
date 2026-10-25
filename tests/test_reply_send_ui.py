"""Offline human-reviewed sending journeys with real validation and a fake mailbox."""

from base64 import urlsafe_b64decode
from email import message_from_bytes

import pytest
from streamlit.testing.v1 import AppTest
from test_gmail_send import ACCOUNT, RECIPIENT, Inbox, Response, Session, userinfo

from studentflow import gmail_send, reply_send_ui

SCRIPT = '''
import os
from pathlib import Path
import streamlit as st
from studentflow.reply_send_ui import render_reply_send
root = Path(os.environ["SF_SEND_TEST_ROOT"])
st.session_state.setdefault("gmail_profile", {"email_address": "student@example.test"})
subject = st.text_input("Draft subject", "Re: Coursework instructions", key="subject")
body = st.text_area("Draft message", "Hello,\\nThank you.\\nALEX TAN", key="body")
render_reply_send(root, root / "data", {"id":"draft1", "recipient":"teacher@example.test"},
                  {"provider":"gmail", "account":"student@example.test", "gmail_id":"abc123"},
                  subject, body, "report1")
'''


def element(elements, label):
    return next(item for item in elements if item.label == label)


def run(app):
    app = app.run(timeout=30)
    assert not app.exception
    return app


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.setenv("SF_SEND_TEST_ROOT", str(tmp_path))
    client = tmp_path / "credentials" / "gmail_client.json"
    client.parent.mkdir()
    client.write_text("{}")
    monkeypatch.setenv("STUDENTFLOW_GMAIL_CLIENT", str(client))
    token = tmp_path / "data" / "auth" / "gmail_send_token.json"
    token.parent.mkdir(parents=True)
    token.write_text("{}")
    inbox = Inbox()
    inbox.close = lambda: None
    calls = []
    sessions = []

    def read(*args, **kwargs):
        calls.append("read")
        return inbox

    def sending(*args, **kwargs):
        calls.append("send-connect")
        session = Session([userinfo(), Response({"id": "sent123", "threadId": "thread123"})])
        sessions.append(session)
        return gmail_send.GmailSendClient(session)

    monkeypatch.setattr(reply_send_ui, "connect", read)
    monkeypatch.setattr(reply_send_ui, "connect_send", sending)
    return tmp_path, inbox, calls, sessions


def prepare(app):
    run(element(app.button, "Review for sending").click())
    return app


def test_render_and_preview_do_not_send_and_confirmed_send_is_persistent(ui):
    _root, _inbox, calls, sessions = ui
    app = run(AppTest.from_string(SCRIPT))
    assert not calls
    prepare(app)
    assert calls == ["read"] and not sessions
    assert element(app.button, "Send with Gmail").disabled
    run(element(app.checkbox, "I have reviewed the recipient and this exact message.").check())
    assert not sessions
    run(element(app.button, "Send with Gmail").click())
    assert any("Sent with Gmail" in item.value for item in app.success)
    posts = [call for session in sessions for call in session.calls if call[0] == "POST"]
    assert len(posts) == 1
    message = message_from_bytes(urlsafe_b64decode(posts[0][2]["json"]["raw"]))
    assert message["To"] == RECIPIENT and message["From"] == ACCOUNT
    assert "ALEX TAN" in message.get_payload(decode=True).decode()
    assert not [button for button in app.button if button.label == "Send with Gmail"]
    reopened = run(AppTest.from_string(SCRIPT))
    prepare(reopened)
    assert any("already been sent" in item.value for item in reopened.success)
    assert len(sessions) == 1


def test_body_changes_clear_review_and_send_exact_current_body(ui):
    _root, _inbox, _calls, sessions = ui
    app = prepare(run(AppTest.from_string(SCRIPT)))
    run(element(app.checkbox, "I have reviewed the recipient and this exact message.").check())
    run(element(app.text_area, "Draft message").set_value("Hello,\nThis is the final edited message.\nALEX TAN"))
    assert not [b for b in app.button if b.label == "Send with Gmail"]
    prepare(app)
    assert not element(app.checkbox, "I have reviewed the recipient and this exact message.").value
    assert element(app.button, "Send with Gmail").disabled
    run(element(app.checkbox, "I have reviewed the recipient and this exact message.").check())
    run(element(app.button, "Send with Gmail").click())
    post = next(call for call in sessions[0].calls if call[0] == "POST")
    message = message_from_bytes(urlsafe_b64decode(post[2]["json"]["raw"]))
    assert message.get_payload(decode=True).decode().replace("\r\n", "\n").strip() == "Hello,\nThis is the final edited message.\nALEX TAN"


def test_placeholder_edits_block_review_without_any_provider_call(ui):
    _root, _inbox, calls, _sessions = ui
    app = run(AppTest.from_string(SCRIPT))
    run(element(app.text_area, "Draft message").set_value("Dear Professor [Surname],"))
    assert element(app.button, "Review for sending").disabled
    assert any("[Surname]" in warning.value for warning in app.warning)
    assert not calls


def test_account_switch_hides_send_controls(ui):
    _root, _inbox, _calls, sessions = ui
    app = prepare(run(AppTest.from_string(SCRIPT)))
    app.session_state["gmail_profile"] = {"email_address": "other@example.test"}
    run(app)
    assert not [b for b in app.button if b.label == "Send with Gmail"]
    assert any("original Gmail inbox" in info.value for info in app.info)
    assert not sessions


def test_timeout_is_not_retried_on_rerender_or_reopen(ui, monkeypatch):
    _root, _inbox, _calls, sessions = ui

    def sending(*args, **kwargs):
        session = Session([userinfo(), TimeoutError("ambiguous")])
        sessions.append(session)
        return gmail_send.GmailSendClient(session)

    monkeypatch.setattr(reply_send_ui, "connect_send", sending)
    app = prepare(run(AppTest.from_string(SCRIPT)))
    run(element(app.checkbox, "I have reviewed the recipient and this exact message.").check())
    run(element(app.button, "Send with Gmail").click())
    assert any("confirmed" in error.value for error in app.error)
    run(app)
    assert any("Delivery is not confirmed" in warning.value for warning in app.warning)
    assert not [b for b in app.button if b.label == "Send with Gmail"]
    reopened = prepare(run(AppTest.from_string(SCRIPT)))
    assert any("Delivery is not confirmed" in warning.value for warning in reopened.warning)
    assert len(sessions) == 1


def test_missing_sending_authorization_does_not_open_browser_on_review(ui):
    root, _inbox, _calls, sessions = ui
    (root / "data" / "auth" / "gmail_send_token.json").unlink()
    app = prepare(run(AppTest.from_string(SCRIPT)))
    run(element(app.checkbox, "I have reviewed the recipient and this exact message.").check())
    assert element(app.button, "Send with Gmail").disabled
    assert element(app.button, "Connect Gmail sending")
    assert not sessions
