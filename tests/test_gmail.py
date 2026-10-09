import base64
import json
import sys
import types

import pytest

from studentflow import gmail


class FakeResponse:
    def __init__(self, value=None, status=200, headers=None, raw=None):
        self.status_code = status
        self.headers = headers or {}
        self.raw = raw if raw is not None else json.dumps(value).encode()
        self.closed = False

    def iter_content(self, chunk_size):
        for index in range(0, len(self.raw), chunk_size):
            yield self.raw[index:index + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        pass


def valid_client():
    return {"installed": {
        "client_id": "fixture.apps.googleusercontent.com",
        "client_secret": "synthetic-client-value",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": gmail.TOKEN_URI,
    }}


def write_client(tmp_path, value=None):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(value or valid_client()), encoding="utf-8")
    return path


def test_only_readonly_gmail_scope():
    assert gmail.SCOPES == ["https://www.googleapis.com/auth/gmail.readonly"]


def test_profile_lists_headers_and_fetches_mime_without_writes():
    mime = b"Subject: test\r\n\r\nA test message"
    responses = [
        FakeResponse({"emailAddress": "demo@example.test"}),
        FakeResponse({"messages": [{"id": "18abcd"}]}),
        FakeResponse({"payload": {"headers": [
            {"name": "Subject", "value": "Test subject"},
            {"name": "From", "value": "teacher@example.test"},
            {"name": "Date", "value": "Fri, 09 Oct 2026 10:00:00 +0800"},
        ]}}),
        FakeResponse({"raw": base64.urlsafe_b64encode(mime).decode().rstrip("=")}),
    ]
    session = FakeSession(responses)
    mailbox = gmail.GmailMailbox(session)
    assert mailbox.profile() == {"email_address": "demo@example.test"}
    assert mailbox.list_messages(5) == [{
        "id": "18abcd", "subject": "Test subject", "sender": "teacher@example.test",
        "sent_at": "Fri, 09 Oct 2026 10:00:00 +0800",
    }]
    assert mailbox.fetch_eml("18abcd") == mime
    assert session.calls[1][1]["params"] == {
        "labelIds": "INBOX", "maxResults": 5, "includeSpamTrash": "false",
    }
    assert session.calls[2][1]["params"]["format"] == "metadata"
    assert session.calls[3][1]["params"] == {"format": "raw"}
    for url, options in session.calls:
        assert url.startswith("https://gmail.googleapis.com/gmail/v1/users/me/")
        assert options["allow_redirects"] is False
        assert options["timeout"] == options["max_allowed_time"] == 30
        assert options["stream"] is True
    assert all(response.closed for response in responses)


@pytest.mark.parametrize("identifier", ["../profile", "https://bad.test", "a?format=full", "", None, "a" * 257])
def test_rejects_invalid_message_id_before_network(identifier):
    session = FakeSession([])
    with pytest.raises(gmail.GmailError, match="邮件编号无效"):
        gmail.GmailMailbox(session).fetch_eml(identifier)
    assert not session.calls


@pytest.mark.parametrize("limit", [0, 21, True, "5", 1.5])
def test_list_limit_is_bounded_before_network(limit):
    session = FakeSession([])
    with pytest.raises(gmail.GmailError):
        gmail.GmailMailbox(session).list_messages(limit)
    assert not session.calls


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
def test_server_errors_are_sanitized_and_response_closed(status):
    response = FakeResponse({"token": "sensitive-server-detail"}, status=status)
    with pytest.raises(gmail.GmailError) as error:
        gmail.GmailMailbox(FakeSession([response])).profile()
    assert "sensitive" not in str(error.value)
    assert response.closed


def test_network_exception_details_are_not_exposed():
    session = FakeSession([RuntimeError("sensitive-request-authorization")])
    with pytest.raises(gmail.GmailError) as error:
        gmail.GmailMailbox(session).profile()
    assert "sensitive" not in str(error.value)


@pytest.mark.parametrize("headers", [{}, {"Content-Length": "90000"}])
def test_stream_and_declared_response_size_are_bounded(headers):
    response = FakeResponse(raw=b" " * 90_000, headers=headers)
    with pytest.raises(gmail.GmailError, match="数据过大"):
        gmail.GmailMailbox(FakeSession([response])).profile()
    assert response.closed


@pytest.mark.parametrize("raw", ["#invalid#", "a", "", None, "SGVsbG8=\n", "SGVs+bG8="])
def test_raw_mail_requires_valid_base64url(raw):
    with pytest.raises(gmail.GmailError):
        gmail.GmailMailbox(FakeSession([FakeResponse({"raw": raw})])).fetch_eml("abcdef")


def test_raw_mail_decoded_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(gmail, "MAX_EMAIL_BYTES", 3)
    encoded = base64.urlsafe_b64encode(b"abcd").decode()
    with pytest.raises(gmail.GmailError, match="超过 20 MB"):
        gmail.GmailMailbox(FakeSession([FakeResponse({"raw": encoded})])).fetch_eml("abcdef")


def test_empty_inbox_is_valid():
    mailbox = gmail.GmailMailbox(FakeSession([FakeResponse({"resultSizeEstimate": 0})]))
    assert mailbox.list_messages() == []


def test_server_cannot_add_more_messages_than_requested():
    result = {"messages": [{"id": "a"}, {"id": "b"}]}
    with pytest.raises(gmail.GmailError):
        gmail.GmailMailbox(FakeSession([FakeResponse(result)])).list_messages(1)


@pytest.mark.parametrize("field,value", [
    ("auth_uri", "https://malicious.test/auth"),
    ("token_uri", "https://malicious.test/token"),
    ("client_id", ""),
    ("client_secret", None),
])
def test_rejects_non_google_or_incomplete_oauth_config(tmp_path, field, value):
    config = valid_client()
    config["installed"][field] = value
    with pytest.raises(gmail.GmailError):
        gmail._client_config(write_client(tmp_path, config))


def test_requires_desktop_oauth_client(tmp_path):
    with pytest.raises(gmail.GmailError, match="桌面应用"):
        gmail._client_config(write_client(tmp_path, {"web": valid_client()["installed"]}))


@pytest.mark.parametrize("change", [
    {"scopes": ["https://mail.google.com/"]},
    {"scopes": gmail.SCOPES + ["https://www.googleapis.com/auth/gmail.modify"]},
    {"client_id": "another-app"}, {"token_uri": "https://bad.test/token"},
])
def test_rejects_cached_tokens_for_other_clients_or_permissions(change):
    client = valid_client()["installed"]
    info = {**client, "scopes": gmail.SCOPES}
    info.update(change)
    with pytest.raises(gmail.GmailError, match="只读权限"):
        gmail._check_token_info(info, client)


def fake_google(monkeypatch):
    events = []

    class Credentials:
        valid = True
        refresh_token = "synthetic-refresh-token"
        scopes = gmail.SCOPES
        granted_scopes = gmail.SCOPES

        @classmethod
        def from_authorized_user_info(cls, info, scopes):
            events.append("cached")
            return cls()

        def to_json(self):
            return json.dumps({**valid_client()["installed"], "scopes": self.scopes})

        def refresh(self, request):
            events.append("refresh")
            self.valid = True

    class Flow:
        @classmethod
        def from_client_config(cls, config, scopes):
            assert scopes == gmail.SCOPES
            events.append("flow")
            return cls()

        def run_local_server(self, **kwargs):
            events.append(kwargs)
            return Credentials()

    transport = types.ModuleType("google.auth.transport.requests")
    transport.AuthorizedSession = lambda credentials, **kwargs: FakeSession([])
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
    return events, Credentials


def test_noninteractive_connect_never_opens_browser(tmp_path, monkeypatch):
    events, _ = fake_google(monkeypatch)
    with pytest.raises(gmail.GmailError, match="点击连接"):
        gmail.connect(write_client(tmp_path), tmp_path / "token.json")
    assert events == []


def test_explicit_connect_opens_loopback_flow_and_saves_token(tmp_path, monkeypatch):
    events, _ = fake_google(monkeypatch)
    token = tmp_path / "auth" / "token.json"
    mailbox = gmail.connect(write_client(tmp_path), token, interactive=True)
    assert isinstance(mailbox, gmail.GmailMailbox)
    options = events[1]
    assert options["host"] == "127.0.0.1"
    assert options["port"] == 0
    assert options["timeout_seconds"] == 180
    assert options["prompt"] == "select_account consent"
    assert "{url}" not in options["authorization_prompt_message"]
    assert json.loads(token.read_text())["scopes"] == gmail.SCOPES
    assert not list(token.parent.glob(".gmail-*"))


def test_valid_cached_token_does_not_trigger_new_consent(tmp_path, monkeypatch):
    events, credentials_type = fake_google(monkeypatch)
    token = tmp_path / "token.json"
    token.write_text(credentials_type().to_json())
    gmail.connect(write_client(tmp_path), token)
    assert events == ["cached"]


def test_expired_cached_token_refreshes_without_browser(tmp_path, monkeypatch):
    events, credentials_type = fake_google(monkeypatch)
    credentials_type.valid = False
    token = tmp_path / "token.json"
    token.write_text(credentials_type().to_json())
    gmail.connect(write_client(tmp_path), token)
    assert events == ["cached", "refresh"]


@pytest.mark.parametrize("returned_scopes", [
    [], gmail.SCOPES + ["https://www.googleapis.com/auth/gmail.modify"],
])
def test_refresh_rechecks_granted_permissions_before_saving(tmp_path, monkeypatch, returned_scopes):
    events, credentials_type = fake_google(monkeypatch)
    credentials_type.valid = False
    token = tmp_path / "token.json"
    original = credentials_type().to_json()
    token.write_text(original)

    def refresh(self, request):
        events.append("refresh")
        self.valid = True
        self.granted_scopes = returned_scopes

    monkeypatch.setattr(credentials_type, "refresh", refresh)
    with pytest.raises(gmail.GmailError, match="只读权限"):
        gmail.connect(write_client(tmp_path), token)
    assert events == ["cached", "refresh"]
    assert token.read_text() == original
