"""Offline tests only: no Google connections and no actual mail sends."""

import base64
import json
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from email import message_from_bytes
from email.header import Header
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from studentflow import gmail_send as send

ACCOUNT = "student@example.test"
RECIPIENT = "teacher@example.test"


class Response:
    def __init__(self, value=None, status=200, raw=None):
        self.status_code = status
        self.raw = json.dumps(value).encode() if raw is None else raw
        self.closed = False

    def iter_content(self, chunk_size):
        yield self.raw

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _request(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def get(self, url, **kwargs):
        return self._request("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._request("POST", url, kwargs)

    def close(self):
        pass


def userinfo(account=ACCOUNT):
    return Response({"email": account, "email_verified": True, "sub": "12345"})


class Inbox:
    def __init__(self, account=ACCOUNT):
        self.address = account
        self.metadata = {"id": "abc123", "threadId": "thread123", "payload": {"headers": [
            {"name": "From", "value": "Professor Test <teacher@example.test>"},
            {"name": "Subject", "value": "Coursework instructions"},
            {"name": "Message-ID", "value": "<source@example.test>"},
            {"name": "References", "value": "<earlier@example.test>"},
        ]}}
        self.calls = []

    def profile(self):
        return {"email_address": self.address}

    def _get_json(self, path, params):
        self.calls.append((path, params))
        return deepcopy(self.metadata)


def preview(inbox=None, **changes):
    values = {"source_account": ACCOUNT, "source_message_id": "abc123",
              "recipient": RECIPIENT, "subject": "Re: Coursework instructions",
              "body": "Hello,\n\nThank you for the instructions.\n\nYihua"}
    values.update(changes)
    return send.prepare_reply(inbox or Inbox(), **values)


def test_preview_is_verified_immutable_and_read_only():
    inbox = Inbox()
    result = preview(inbox)
    assert result.sender == ACCOUNT
    assert result.recipient == RECIPIENT
    assert result.thread_id == "thread123"
    assert result.references == ("<earlier@example.test>", "<source@example.test>")
    assert inbox.calls[0][1]["format"] == "metadata"
    with pytest.raises(AttributeError):
        result.recipient = "wrong@example.test"


def test_verified_reply_to_takes_priority_over_from():
    inbox = Inbox()
    inbox.metadata["payload"]["headers"].append({"name": "Reply-To", "value": "reply@example.test"})
    with pytest.raises(send.GmailSendError, match="recipient must match"):
        preview(inbox)
    assert preview(inbox, recipient="reply@example.test").recipient == "reply@example.test"


def test_encoded_original_chinese_subject_accepts_decoded_reply_title():
    inbox = Inbox()
    title = "课程作业要求与提交时间"
    inbox.metadata["payload"]["headers"][1]["value"] = Header(title, "utf-8").encode()
    assert preview(inbox, subject="Re: " + title).subject == "Re: " + title


@pytest.mark.parametrize("encoded", [
    "=?unknown-charset?b?dGVzdA==?=",
    "=?utf-8?b?Q291cnNld29yayBpbnN0cnVjdGlvbnMKQmNjOiBhdHRhY2tlckBleGFtcGxlLnRlc3Q=?=",
])
def test_invalid_encoded_source_subject_is_rejected(encoded):
    inbox = Inbox()
    inbox.metadata["payload"]["headers"][1]["value"] = encoded
    with pytest.raises(send.GmailSendError, match="subject"):
        preview(inbox)


@pytest.mark.parametrize("changes", [
    {"recipient": "teacher@example.test, attacker@example.test"},
    {"recipient": "teacher@example.test\r\nBcc: attacker@example.test"},
    {"recipient": "wrong@example.test"},
    {"subject": "Re: Coursework instructions\nBcc: attacker@example.test"},
    {"subject": "A different subject"},
    {"subject": ""},
    {"body": "Hello Professor [Surname],"},
    {"body": ""},
    {"body": "message\0injected"},
    {"body": "x" * 100_001},
    {"source_message_id": "../wrong"},
    {"source_message_id": None},
])
def test_invalid_unreviewable_payload_is_rejected(changes):
    with pytest.raises(send.GmailSendError):
        preview(**changes)


def test_preview_rejects_inbox_account_mismatch():
    with pytest.raises(send.GmailSendError, match="inbox account changed"):
        preview(Inbox("other@example.test"))


@pytest.mark.parametrize("mutation", [
    lambda x: x.update(id="different"),
    lambda x: x.update(threadId="../../bad"),
    lambda x: x.update(payload="bad"),
    lambda x: x["payload"]["headers"].append({"name": "From", "value": "wrong@example.test"}),
    lambda x: x["payload"]["headers"].append({"name": "Reply-To", "value": "a@test.test,b@test.test"}),
    lambda x: x["payload"]["headers"][2].update(value="missing-message-id"),
    lambda x: x["payload"]["headers"][3].update(value="<a@test.test>\nBcc: wrong@example.test"),
])
def test_original_header_validation_is_fail_closed(mutation):
    inbox = Inbox()
    mutation(inbox.metadata)
    with pytest.raises(send.GmailSendError):
        preview(inbox)


def test_send_uses_thread_headers_and_exact_reviewed_content_once(tmp_path):
    session = Session([userinfo(), Response({"id": "sent123", "threadId": "thread123"})])
    client = send.GmailSendClient(session)
    payload = preview()
    result = send.send_reviewed_reply(client, Inbox(), payload, data_dir=tmp_path, confirmed=True)
    assert result == {"status": "sent", "id": "sent123"}
    method, url, options = session.calls[-1]
    assert method == "POST" and url.endswith("/messages/send")
    assert options["allow_redirects"] is False
    assert options["json"]["threadId"] == "thread123"
    message = message_from_bytes(base64.urlsafe_b64decode(options["json"]["raw"]))
    assert message["From"] == ACCOUNT
    assert message["To"] == RECIPIENT
    assert message["Cc"] is None and message["Bcc"] is None
    assert message["In-Reply-To"] == "<source@example.test>"
    assert message["References"] == "<earlier@example.test> <source@example.test>"
    assert message["Subject"] == payload.subject
    assert message.get_payload(decode=True).decode().replace("\r\n", "\n").rstrip() == payload.body
    assert send.reply_send_status(tmp_path, payload.fingerprint) == result
    ledger = (tmp_path / "gmail_sends.sqlite3").read_bytes()
    assert payload.body.encode() not in ledger and ACCOUNT.encode() not in ledger


def test_confirmation_required_before_any_network_or_journal(tmp_path):
    session = Session([])
    with pytest.raises(send.GmailSendError, match="confirm"):
        send.send_reviewed_reply(send.GmailSendClient(session), Inbox(), preview(), data_dir=tmp_path)
    assert session.calls == []
    assert list(tmp_path.iterdir()) == []


def test_changed_account_or_tampered_preview_blocks_sending(tmp_path):
    session = Session([userinfo("other@example.test")])
    client = send.GmailSendClient(session)
    with pytest.raises(send.GmailSendError, match="same account"):
        send.send_reviewed_reply(client, Inbox(), preview(), data_dir=tmp_path, confirmed=True)
    assert len(session.calls) == 1
    with pytest.raises(send.GmailSendError, match="changed"):
        send.send_reviewed_reply(client, Inbox(), replace(preview(), fingerprint="a" * 64),
                                 data_dir=tmp_path, confirmed=True)
    assert len(session.calls) == 1


def test_source_change_invalidates_preview(tmp_path):
    payload = preview()
    inbox = Inbox()
    inbox.metadata["threadId"] = "newthread"
    session = Session([])
    with pytest.raises(send.GmailSendError, match="changed"):
        send.send_reviewed_reply(send.GmailSendClient(session), inbox, payload,
                                 data_dir=tmp_path, confirmed=True)
    assert session.calls == []


def test_duplicate_after_restart_does_not_resend(tmp_path):
    payload = preview()
    first = Session([userinfo(), Response({"id": "sent123"})])
    send.send_reviewed_reply(send.GmailSendClient(first), Inbox(), payload,
                             data_dir=tmp_path, confirmed=True)
    second = Session([userinfo()])
    result = send.send_reviewed_reply(send.GmailSendClient(second), Inbox(), payload,
                                     data_dir=tmp_path, confirmed=True)
    assert result == {"status": "already_sent", "id": "sent123"}
    assert [item[0] for item in second.calls] == ["GET"]


@pytest.mark.parametrize("failure", [
    TimeoutError("private token in error"),
    Response(status=500),
    Response(status=302),
    Response(raw=b"not json"),
    Response({"id": "bad/id"}),
])
def test_ambiguous_send_blocks_retries_after_restart(tmp_path, failure):
    payload = preview()
    session = Session([userinfo(), failure])
    with pytest.raises(send.GmailSendUncertain) as error:
        send.send_reviewed_reply(send.GmailSendClient(session), Inbox(), payload,
                                 data_dir=tmp_path, confirmed=True)
    assert "private token" not in str(error.value)
    assert send.reply_send_status(tmp_path, payload.fingerprint)["status"] == "uncertain"
    second = Session([userinfo()])
    with pytest.raises(send.GmailSendUncertain):
        send.send_reviewed_reply(send.GmailSendClient(second), Inbox(), payload,
                                 data_dir=tmp_path, confirmed=True)
    assert [item[0] for item in second.calls] == ["GET"]


def test_pending_interrupted_send_blocks_duplicate(tmp_path):
    payload = preview()
    assert send._reserve(tmp_path, payload.fingerprint) is None
    session = Session([userinfo()])
    with pytest.raises(send.GmailSendUncertain):
        send.send_reviewed_reply(send.GmailSendClient(session), Inbox(), payload,
                                 data_dir=tmp_path, confirmed=True)
    assert [item[0] for item in session.calls] == ["GET"]


def test_explicit_rejection_can_be_retried_by_new_confirmed_action(tmp_path):
    payload = preview()
    failed = Session([userinfo(), Response(status=403)])
    with pytest.raises(send.GmailSendError, match="rejected"):
        send.send_reviewed_reply(send.GmailSendClient(failed), Inbox(), payload,
                                 data_dir=tmp_path, confirmed=True)
    assert send.reply_send_status(tmp_path, payload.fingerprint)["status"] == "rejected"
    successful = Session([userinfo(), Response({"id": "sent123"})])
    assert send.send_reviewed_reply(send.GmailSendClient(successful), Inbox(), payload,
                                    data_dir=tmp_path, confirmed=True)["status"] == "sent"


@pytest.mark.parametrize("value", [
    {"email": ACCOUNT, "email_verified": False, "sub": "123"},
    {"email": ACCOUNT, "email_verified": True},
    {"email": "bad\naddress", "email_verified": True, "sub": "123"},
])
def test_sender_identity_requires_verified_email(value):
    response = Response(value)
    with pytest.raises(send.GmailSendAuthError):
        send.GmailSendClient(Session([response])).account()
    assert response.closed


def test_sending_permission_is_minimal_and_separate():
    assert set(send.SEND_SCOPES) == {
        "https://www.googleapis.com/auth/gmail.send", "openid",
        "https://www.googleapis.com/auth/userinfo.email",
    }
    assert send._scope_set(["openid", "email", send.SEND_SCOPE]) == set(send.SEND_SCOPES)
    with pytest.raises(send.GmailSendError, match="separate"):
        send.connect_send(Path("irrelevant"), Path("gmail_token.json"))


@pytest.mark.parametrize("scopes", [
    [send.SEND_SCOPE],
    [*send.SEND_SCOPES, "https://www.googleapis.com/auth/gmail.modify"],
    ["https://www.googleapis.com/auth/gmail.readonly"],
])
def test_scope_expansion_or_missing_identity_grants_rejected(scopes):
    with pytest.raises(send.GmailSendAuthError):
        send._check_credentials(SimpleNamespace(scopes=scopes, granted_scopes=scopes, valid=True))


def fake_google(monkeypatch):
    state = {"events": [], "valid": True, "refresh_error": None,
             "interactive_scopes": list(send.SEND_SCOPES)}
    config = {"installed": {
        "client_id": "synthetic.apps.googleusercontent.com", "client_secret": "synthetic-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }}

    class Credentials:
        def __init__(self, info=None):
            self.info = info or {**config["installed"], "scopes": state["interactive_scopes"]}
            self.scopes = self.info["scopes"]
            self.granted_scopes = self.scopes
            self.refresh_token = "synthetic-refresh-token"
            self.valid = state["valid"]

        @classmethod
        def from_authorized_user_info(cls, info, scopes):
            state["events"].append("cached")
            return cls(info)

        def to_json(self):
            return json.dumps(self.info)

        def refresh(self, request):
            state["events"].append("refresh")
            if state["refresh_error"]:
                raise state["refresh_error"]
            self.valid = True

    class Flow:
        @classmethod
        def from_client_config(cls, config, scopes):
            assert scopes == send.SEND_SCOPES
            state["events"].append("flow")
            return cls()

        def run_local_server(self, **kwargs):
            state["events"].append(kwargs)
            return Credentials()

    transport = types.ModuleType("google.auth.transport.requests")

    def session(credentials, **kwargs):
        assert kwargs == {"max_refresh_attempts": 0}
        return Session([])

    transport.AuthorizedSession = session
    transport.Request = lambda: lambda *args, **kwargs: None
    oauth = types.ModuleType("google.oauth2.credentials")
    oauth.Credentials = Credentials
    flow_module = types.ModuleType("google_auth_oauthlib.flow")
    flow_module.InstalledAppFlow = Flow
    for name, module in [
        ("google.auth.transport.requests", transport),
        ("google.oauth2.credentials", oauth),
        ("google_auth_oauthlib.flow", flow_module),
    ]:
        monkeypatch.setitem(sys.modules, name, module)
    return state, config, Credentials


def write_client(tmp_path, config):
    path = tmp_path / "client.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_connect_without_saved_token_never_opens_browser(tmp_path, monkeypatch):
    state, config, _ = fake_google(monkeypatch)
    with pytest.raises(send.GmailSendAuthError, match="Connect Gmail sending"):
        send.connect_send(write_client(tmp_path, config), tmp_path / send.TOKEN_FILENAME)
    assert state["events"] == []


def test_explicit_sending_consent_is_isolated_and_reopens_silently(tmp_path, monkeypatch):
    state, config, _ = fake_google(monkeypatch)
    original = tmp_path / "gmail_token.json"
    original.write_text("existing-read-only-token", encoding="utf-8")
    token = tmp_path / send.TOKEN_FILENAME
    client = write_client(tmp_path, config)
    send.connect_send(client, token, interactive=True).close()
    assert state["events"][0] == "flow"
    options = state["events"][1]
    assert options["host"] == "127.0.0.1"
    assert options["timeout_seconds"] == 180
    assert options["include_granted_scopes"] == "false"
    assert original.read_text() == "existing-read-only-token"
    assert json.loads(token.read_text())["scopes"] == send.SEND_SCOPES
    state["events"].clear()
    send.connect_send(client, token).close()
    assert state["events"] == ["cached"]


@pytest.mark.parametrize("change", [
    {"client_id": "different-client"},
    {"token_uri": "https://wrong.example/token"},
    {"scopes": ["https://www.googleapis.com/auth/gmail.readonly"]},
    {"scopes": [*send.SEND_SCOPES, "https://www.googleapis.com/auth/gmail.modify"]},
])
def test_invalid_cached_send_token_does_not_refresh_or_reauthorize(tmp_path, monkeypatch, change):
    state, config, credentials = fake_google(monkeypatch)
    token = tmp_path / send.TOKEN_FILENAME
    info = json.loads(credentials().to_json())
    info.update(change)
    token.write_text(json.dumps(info))
    before = token.read_bytes()
    with pytest.raises(send.GmailSendAuthError):
        send.connect_send(write_client(tmp_path, config), token)
    assert state["events"] == []
    assert token.read_bytes() == before


def test_broader_grant_rejected_before_overwriting_existing_send_token(tmp_path, monkeypatch):
    state, config, _ = fake_google(monkeypatch)
    state["interactive_scopes"].append("https://www.googleapis.com/auth/gmail.modify")
    token = tmp_path / send.TOKEN_FILENAME
    token.write_text("existing-token")
    with pytest.raises(send.GmailSendAuthError):
        send.connect_send(write_client(tmp_path, config), token, interactive=True)
    assert token.read_text() == "existing-token"


def test_sending_token_refresh_has_no_interactive_flow(tmp_path, monkeypatch):
    state, config, credentials = fake_google(monkeypatch)
    token = tmp_path / send.TOKEN_FILENAME
    token.write_text(credentials().to_json())
    state["valid"] = False
    send.connect_send(write_client(tmp_path, config), token)
    assert state["events"] == ["cached", "refresh"]


def test_temporary_refresh_failure_preserves_token_and_hides_private_error(tmp_path, monkeypatch):
    state, config, credentials = fake_google(monkeypatch)
    token = tmp_path / send.TOKEN_FILENAME
    token.write_text(credentials().to_json())
    previous = token.read_bytes()
    state.update(valid=False, refresh_error=TimeoutError("secret-token-in-error"))
    with pytest.raises(send.GmailSendError) as error:
        send.connect_send(write_client(tmp_path, config), token)
    assert "secret-token" not in str(error.value)
    assert token.read_bytes() == previous
    assert state["events"] == ["cached", "refresh"]


def test_journal_failure_before_send_blocks_request(tmp_path, monkeypatch):
    payload = preview()
    session = Session([userinfo()])

    def no_journal(*args, **kwargs):
        raise send.sqlite3.OperationalError("private filesystem path")

    monkeypatch.setattr(send.sqlite3, "connect", no_journal)
    with pytest.raises(send.GmailSendError, match="Sending is blocked") as error:
        send.send_reviewed_reply(send.GmailSendClient(session), Inbox(), payload,
                                 data_dir=tmp_path, confirmed=True)
    assert "private filesystem" not in str(error.value)
    assert [item[0] for item in session.calls] == ["GET"]


def test_journal_failure_after_success_never_retries_send(tmp_path, monkeypatch):
    payload = preview()
    session = Session([userinfo(), Response({"id": "sent123"})])

    def no_finish(*args, **kwargs):
        raise send.GmailSendError("disk unavailable")

    monkeypatch.setattr(send, "_finish", no_finish)
    with pytest.raises(send.GmailSendUncertain, match="Gmail accepted"):
        send.send_reviewed_reply(send.GmailSendClient(session), Inbox(), payload,
                                 data_dir=tmp_path, confirmed=True)
    assert send.reply_send_status(tmp_path, payload.fingerprint)["status"] == "pending"
    assert [item[0] for item in session.calls] == ["GET", "POST"]


def test_concurrent_send_clicks_cannot_submit_twice(tmp_path):
    started, release = Event(), Event()
    calls = []

    class SlowClient:
        def account(self):
            return ACCOUNT

        def _send_once(self, payload):
            calls.append(payload.fingerprint)
            started.set()
            assert release.wait(5)
            return "sent123"

    payload = preview()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(send.send_reviewed_reply, SlowClient(), Inbox(), payload,
                                 data_dir=tmp_path, confirmed=True)
        assert started.wait(5)
        try:
            with pytest.raises(send.GmailSendUncertain):
                send.send_reviewed_reply(SlowClient(), Inbox(), payload,
                                         data_dir=tmp_path, confirmed=True)
        finally:
            release.set()
        assert future.result()["status"] == "sent"
    assert calls == [payload.fingerprint]
