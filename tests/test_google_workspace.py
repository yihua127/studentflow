"""Offline connector contract tests; every HTTP and OAuth interaction is fake."""

import json
import logging
import os
import sys
import types
from datetime import UTC, datetime

import pytest

from studentflow import google_workspace as workspace


class FakeResponse:
    def __init__(self, value=None, status=200, *, raw=None, headers=None):
        self.status_code = status
        self.raw = json.dumps(value).encode() if raw is None else raw
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size):
        for index in range(0, len(self.raw), chunk_size):
            yield self.raw[index:index + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


def client_config():
    return {"installed": {
        "client_id": "synthetic.apps.googleusercontent.com",
        "client_secret": "synthetic-client-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": workspace.TOKEN_URI,
    }}


def write_client(tmp_path, value=None):
    path = tmp_path / "client.json"
    path.write_text(json.dumps(value or client_config()), encoding="utf-8")
    return path


def token_info(scopes=None):
    return {
        **client_config()["installed"], "token": "synthetic-access",
        "refresh_token": "synthetic-refresh",
        "scopes": list(workspace.SCOPES if scopes is None else scopes),
        "expiry": "2099-01-01T00:00:00Z",
    }


def write_token(tmp_path, value=None, name="workspace_token.json"):
    path = tmp_path / name
    path.write_text(json.dumps(token_info() if value is None else value), encoding="utf-8")
    return path


def fake_google(monkeypatch):
    state = {"events": [], "valid": True, "refresh_error": None,
             "interactive_scopes": list(workspace.SCOPES), "refresh_scopes": None}

    class Credentials:
        def __init__(self, info=None):
            self.info = info or token_info()
            self.valid = state["valid"]
            self.refresh_token = self.info.get("refresh_token")
            self.scopes = self.info["scopes"]
            self.granted_scopes = None

        @classmethod
        def from_authorized_user_info(cls, info, scopes):
            state["events"].append(("cached", scopes))
            assert "universe_domain" not in info
            return cls(info)

        def to_json(self):
            return json.dumps(self.info)

        def refresh(self, request):
            state["events"].append(("refresh",))
            if state["refresh_error"] is not None:
                raise state["refresh_error"]
            request("https://oauth2.googleapis.com/token", method="POST")
            self.valid = True
            self.granted_scopes = state["refresh_scopes"]
            self.info["token"] = "synthetic-renewed-access"

    class Flow:
        @classmethod
        def from_client_config(cls, config, scopes):
            state["events"].append(("flow", scopes))
            instance = cls()
            instance.oauth2session = types.SimpleNamespace(
                request=lambda *args, **kwargs: state["events"].append(("oauth_request", kwargs)),
                token={"scope": " ".join(state["interactive_scopes"])},
            )
            return instance

        def run_local_server(self, **kwargs):
            state["events"].append(("browser", kwargs))
            logging.getLogger("google_auth_oauthlib.flow").warning(
                "synthetic-secret-callback?code=synthetic-code"
            )
            assert os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE") == "1"
            self.oauth2session.request("POST", workspace.TOKEN_URI)
            credentials = Credentials()
            credentials.granted_scopes = state["interactive_scopes"]
            return credentials

    def transport(*args, **kwargs):
        state["events"].append(("transport", kwargs))

    transport_module = types.ModuleType("google.auth.transport.requests")
    transport_module.AuthorizedSession = lambda *args, **kwargs: FakeSession()
    transport_module.Request = lambda: transport
    credentials_module = types.ModuleType("google.oauth2.credentials")
    credentials_module.Credentials = Credentials
    flow_module = types.ModuleType("google_auth_oauthlib.flow")
    flow_module.InstalledAppFlow = Flow
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", transport_module)
    monkeypatch.setitem(sys.modules, "google.oauth2.credentials", credentials_module)
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", flow_module)
    return state


def test_restore_approved_seven_scope_token_without_consent_or_modification(tmp_path, monkeypatch):
    state = fake_google(monkeypatch)
    token = write_token(tmp_path, token_info(workspace.APPROVED_SCOPES))
    original = token.read_bytes()
    client = workspace.connect_workspace(write_client(tmp_path), token)
    assert client.granted_scopes == workspace.APPROVED_SCOPES
    assert [event[0] for event in state["events"]] == ["cached"]
    assert token.read_bytes() == original
    assert not hasattr(client, "send")
    assert not hasattr(client, "send_message")
    client.close()
    assert client._session.closed


def test_noninteractive_missing_token_never_opens_browser(tmp_path, monkeypatch):
    state = fake_google(monkeypatch)
    with pytest.raises(workspace.GoogleWorkspaceAuthError, match="Click Connect Google"):
        workspace.connect_workspace(write_client(tmp_path), tmp_path / "absent.json")
    assert state["events"] == []


def test_explicit_sign_in_saves_actual_grants_and_keeps_callback_private(
    tmp_path, monkeypatch, caplog, capsys,
):
    state = fake_google(monkeypatch)
    state["interactive_scopes"] = sorted(workspace.APPROVED_SCOPES)
    monkeypatch.setenv("OAUTHLIB_RELAX_TOKEN_SCOPE", "old-setting")
    previous_log_level = logging.root.manager.disable
    token = tmp_path / "new" / "workspace.json"
    workspace.connect_workspace(write_client(tmp_path), token, interactive=True)
    assert set(json.loads(token.read_text())["scopes"]) == workspace.APPROVED_SCOPES
    options = next(event[1] for event in state["events"] if event[0] == "browser")
    assert options["host"] == "127.0.0.1"
    assert options["authorization_prompt_message"] is None
    assert options["include_granted_scopes"] == "false"
    request = next(event[1] for event in state["events"] if event[0] == "oauth_request")
    assert request == {"timeout": workspace.REQUEST_SECONDS, "allow_redirects": False}
    assert os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] == "old-setting"
    assert logging.root.manager.disable == previous_log_level
    assert "synthetic-secret" not in caplog.text + capsys.readouterr().out
    assert not list(token.parent.glob(".google-workspace-*"))


@pytest.mark.parametrize("grants", [
    [], [workspace.SCOPES[0]], [*workspace.SCOPES, workspace.SCOPE_PREFIX + "drive"],
])
def test_incomplete_or_unapproved_interactive_grants_do_not_overwrite_saved_token(
    tmp_path, monkeypatch, grants,
):
    state = fake_google(monkeypatch)
    state["interactive_scopes"] = grants
    token = write_token(tmp_path)
    original = token.read_bytes()
    with pytest.raises(workspace.GoogleWorkspaceAuthError):
        workspace.connect_workspace(write_client(tmp_path), token, interactive=True)
    assert token.read_bytes() == original


@pytest.mark.parametrize("change", [
    {"client_id": "different-client"}, {"client_secret": "different-secret"},
    {"token_uri": "https://untrusted.example/token"},
    {"scopes": [*workspace.SCOPES, workspace.SCOPE_PREFIX + "drive"]},
    {"scopes": [workspace.SCOPES[0]]},
])
def test_restore_rejects_client_mismatch_missing_or_extra_permissions(tmp_path, monkeypatch, change):
    state = fake_google(monkeypatch)
    token = write_token(tmp_path, {**token_info(), **change})
    with pytest.raises(workspace.GoogleWorkspaceAuthError):
        workspace.connect_workspace(write_client(tmp_path), token)
    assert state["events"] == []


def test_calendar_sync_requires_new_consent_and_keeps_readonly_token_usable(tmp_path, monkeypatch):
    state = fake_google(monkeypatch)
    config = write_client(tmp_path)
    token = write_token(tmp_path)
    original = token.read_bytes()
    with pytest.raises(workspace.GoogleWorkspaceAuthError):
        workspace.connect_workspace(config, token, required_scopes=workspace.CALENDAR_SYNC_SCOPES)
    assert token.read_bytes() == original
    assert workspace.connect_workspace(config, token).granted_scopes == frozenset(workspace.SCOPES)
    state["interactive_scopes"] = workspace.CALENDAR_SYNC_SCOPES
    separate = tmp_path / "calendar_sync_token.json"
    connection = workspace.connect_workspace(
        config, separate, interactive=True, required_scopes=workspace.CALENDAR_SYNC_SCOPES,
    )
    assert connection.granted_scopes == frozenset(workspace.CALENDAR_SYNC_SCOPES)
    assert token.read_bytes() == original
    assert separate.is_file()


def test_required_readonly_subset_is_allowed_and_write_request_is_rejected(tmp_path, monkeypatch):
    state = fake_google(monkeypatch)
    subset = [workspace.SCOPE_PREFIX + "calendar.events.readonly"]
    token = write_token(tmp_path, token_info(subset))
    config = write_client(tmp_path)
    client = workspace.connect_workspace(config, token, required_scopes=subset)
    with pytest.raises(workspace.GoogleWorkspaceAuthError):
        client.profile()
    assert not client._session.calls
    with pytest.raises(workspace.GoogleWorkspaceError, match="read-only"):
        workspace.connect_workspace(config, token, required_scopes=[workspace.SCOPE_PREFIX + "gmail.send"])
    assert len(state["events"]) == 1


def test_refresh_is_bounded_and_persists_renewal_without_browser(tmp_path, monkeypatch):
    state = fake_google(monkeypatch)
    state["valid"] = False
    token = write_token(tmp_path)
    workspace.connect_workspace(write_client(tmp_path), token)
    assert [event[0] for event in state["events"]] == ["cached", "refresh", "transport"]
    assert state["events"][-1][1]["timeout"] == workspace.REQUEST_SECONDS
    assert state["events"][-1][1]["allow_redirects"] is False
    assert json.loads(token.read_text())["token"] == "synthetic-renewed-access"


@pytest.mark.parametrize("revoked", [True, False])
def test_refresh_failure_is_safe_and_retains_existing_token(tmp_path, monkeypatch, revoked):
    from google.auth.exceptions import RefreshError, TransportError

    state = fake_google(monkeypatch)
    state["valid"] = False
    state["refresh_error"] = (
        RefreshError("synthetic-secret-response", {"error": "invalid_grant"}) if revoked
        else TransportError("synthetic-secret-request")
    )
    token = write_token(tmp_path)
    original = token.read_bytes()
    with pytest.raises(workspace.GoogleWorkspaceError) as error:
        workspace.connect_workspace(write_client(tmp_path), token)
    assert isinstance(error.value, workspace.GoogleWorkspaceAuthError) is revoked
    assert "synthetic-secret" not in str(error.value)
    assert token.read_bytes() == original
    assert all(event[0] != "browser" for event in state["events"])


def test_refresh_rechecks_actual_scope_grants_before_save(tmp_path, monkeypatch):
    state = fake_google(monkeypatch)
    state["valid"] = False
    state["refresh_scopes"] = [workspace.SCOPES[0]]
    token = write_token(tmp_path)
    original = token.read_bytes()
    with pytest.raises(workspace.GoogleWorkspaceAuthError):
        workspace.connect_workspace(write_client(tmp_path), token)
    assert token.read_bytes() == original


def test_explicit_import_is_local_preserves_source_and_strips_unused_token_fields(tmp_path, monkeypatch):
    state = fake_google(monkeypatch)
    source = write_token(tmp_path, {
        **token_info(workspace.APPROVED_SCOPES), "universe_domain": "untrusted.example",
    }, name="probe.json")
    original = source.read_bytes()
    target = tmp_path / "data" / "google_workspace_token.json"
    granted = workspace.import_workspace_token(write_client(tmp_path), source, target)
    assert granted == workspace.APPROVED_SCOPES
    assert source.read_bytes() == original
    assert "universe_domain" not in json.loads(target.read_text())
    assert [event[0] for event in state["events"]] == ["cached"]
    saved = target.read_bytes()
    with pytest.raises(workspace.GoogleWorkspaceError, match="already exists"):
        workspace.import_workspace_token(tmp_path / "client.json", source, target)
    assert target.read_bytes() == saved


@pytest.mark.parametrize("field", ["auth_uri", "token_uri"])
def test_untrusted_oauth_endpoints_never_reach_google_sdk(tmp_path, monkeypatch, field):
    state = fake_google(monkeypatch)
    config = client_config()
    config["installed"][field] = "https://untrusted.example/oauth"
    with pytest.raises(workspace.GoogleWorkspaceError):
        workspace.connect_workspace(write_client(tmp_path, config), tmp_path / "token.json", True)
    assert state["events"] == []


def test_migration_rejects_invalid_expiry_using_real_credentials_parser(tmp_path):
    source = write_token(tmp_path, {**token_info(), "expiry": "not-a-time"}, name="probe.json")
    target = tmp_path / "new-token.json"
    with pytest.raises(workspace.GoogleWorkspaceAuthError):
        workspace.import_workspace_token(write_client(tmp_path), source, target)
    assert not target.exists()


@pytest.mark.parametrize("url", [
    "https://untrusted.example/profile",
    "https://gmail.googleapis.com.untrusted.example/gmail/v1/users/me/profile",
    workspace.CHAT_BASE + "/spaces?access_token=synthetic",
])
def test_internal_transport_never_sends_authorization_to_unapproved_endpoint(url):
    session = FakeSession()
    with pytest.raises(workspace.GoogleWorkspaceError):
        workspace.GoogleWorkspaceClient(session)._get_json(url, {}, label="test")
    assert session.calls == []


@pytest.mark.parametrize("status", [301, 400, 401, 403, 429, 500])
def test_api_errors_are_safe_and_closed_without_following_redirects(status):
    response = FakeResponse({"error": "synthetic-private-server-body"}, status=status)
    session = FakeSession([response])
    with pytest.raises(workspace.GoogleWorkspaceError) as error:
        workspace.GoogleWorkspaceClient(session).profile()
    assert "synthetic-private" not in str(error.value)
    assert isinstance(error.value, workspace.GoogleWorkspaceAuthError) is (status == 401)
    assert session.calls[0][1]["allow_redirects"] is False
    assert response.closed


@pytest.mark.parametrize("raw,headers", [
    (b" " * 65_000, {}), (b"{}", {"Content-Length": "65000"}),
    (b"synthetic-invalid-json", {}), (b"[]", {}),
], ids=["oversized_stream", "oversized_header", "invalid_json", "wrong_json_type"])
def test_profile_rejects_oversized_or_invalid_responses(raw, headers):
    response = FakeResponse(raw=raw, headers=headers)
    with pytest.raises(workspace.GoogleWorkspaceError):
        workspace.GoogleWorkspaceClient(FakeSession([response])).profile()
    assert response.closed


def test_profile_is_cached_only_within_this_connected_account():
    first = workspace.GoogleWorkspaceClient(FakeSession([
        FakeResponse({"emailAddress": "first@example.test"}),
    ]))
    second = workspace.GoogleWorkspaceClient(FakeSession([
        FakeResponse({"emailAddress": "second@example.test"}),
    ]))
    assert first.profile() == first.profile() == {"email_address": "first@example.test"}
    assert second.profile() == {"email_address": "second@example.test"}
    assert len(first._session.calls) == 1


def test_chat_lists_all_pages_and_retains_nontext_message_metadata():
    responses = [
        FakeResponse({"spaces": [{"name": "spaces/AAA", "displayName": "Study Group"}],
                      "nextPageToken": "space-page-2"}),
        FakeResponse({"spaces": [{"name": "spaces/BBB", "displayName": "Other"}]}),
        FakeResponse({"emailAddress": "student@example.test"}),
        FakeResponse({"messages": [{"name": "spaces/AAA/messages/msg1", "text": "Test task",
                                    "createTime": "2026-10-21T09:00:00Z",
                                    "sender": {"name": "users/123", "displayName": "Test Student",
                                               "type": "HUMAN"},
                                    "thread": {"name": "spaces/AAA/threads/thread1"}}],
                      "nextPageToken": "message-page-2"}),
        FakeResponse({"messages": [{"name": "spaces/AAA/messages/msg2",
                                    "createTime": "2026-10-21T09:01:00Z",
                                    "attachment": [{"name": "attachment"}], "cardsV2": [{}]}]}),
    ]
    session = FakeSession(responses)
    client = workspace.GoogleWorkspaceClient(session)
    assert [row["display_name"] for row in client.list_spaces()] == ["Study Group", "Other"]
    messages = client.list_messages("spaces/AAA")
    assert messages[0]["source_id"] == "google_chat:student@example.test:spaces/AAA/messages/msg1"
    assert messages[0]["thread_id"] == "spaces/AAA/threads/thread1"
    assert messages[0]["title"] == "Study Group"
    assert messages[0]["sender_id"] == "users/123"
    assert messages[0]["sender_name"] == "Test Student"
    assert messages[1]["sender_name"] == ""
    assert messages[1]["text"] == ""
    assert not messages[1]["text_available"]
    assert messages[1]["has_attachments"] and messages[1]["has_cards"]
    assert session.calls[1][1]["params"]["pageToken"] == "space-page-2"
    assert session.calls[4][1]["params"]["pageToken"] == "message-page-2"
    assert all(response.closed for response in responses)


@pytest.mark.parametrize("space", [
    "https://untrusted.example", "spaces/../AAA", "spaces/AAA?access_token=x",
    "spaces/AAA/messages/x", "spaces/AAA\n", "spaces/%2e%2e", None,
])
def test_resource_ids_reject_url_or_path_injection_before_network(space):
    session = FakeSession()
    with pytest.raises(workspace.GoogleWorkspaceError):
        workspace.GoogleWorkspaceClient(session).list_messages(space)
    assert session.calls == []


def test_thread_filter_is_limited_to_selected_space_and_encoded_as_parameter():
    session = FakeSession([FakeResponse({"emailAddress": "student@example.test"}), FakeResponse({})])
    client = workspace.GoogleWorkspaceClient(session)
    assert client.list_messages("spaces/AAA", thread_id="spaces/AAA/threads/one") == []
    assert session.calls[-1][1]["params"]["filter"] == "thread.name = spaces/AAA/threads/one"
    with pytest.raises(workspace.GoogleWorkspaceError):
        client.list_messages("spaces/AAA", thread_id="spaces/BBB/threads/other")
    assert len(session.calls) == 2


@pytest.mark.parametrize("second", [
    FakeResponse({"error": "synthetic-private"}, status=503),
    RuntimeError("synthetic-private-request"),
    FakeResponse({"spaces": [], "nextPageToken": "page2"}),
    FakeResponse({"spaces": {}, "nextPageToken": "page3"}),
])
def test_failed_or_cyclic_pagination_never_returns_partial_spaces(second):
    session = FakeSession([
        FakeResponse({"spaces": [{"name": "spaces/AAA"}], "nextPageToken": "page2"}), second,
    ])
    with pytest.raises(workspace.GoogleWorkspaceError) as error:
        workspace.GoogleWorkspaceClient(session).list_spaces()
    assert "synthetic-private" not in str(error.value)


def test_page_limit_is_explicit_and_empty_page_still_continues(monkeypatch):
    monkeypatch.setattr(workspace, "MAX_PAGES", 2)
    session = FakeSession([
        FakeResponse({"nextPageToken": "page2"}),
        FakeResponse({"spaces": [{"name": "spaces/AAA"}], "nextPageToken": "page3"}),
    ])
    with pytest.raises(workspace.GoogleWorkspaceError, match="Too many"):
        workspace.GoogleWorkspaceClient(session).list_spaces()
    assert len(session.calls) == 2


def calendar_event(event_id, start="2026-10-21T10:00:00+08:00", end="2026-10-21T11:00:00+08:00", **extra):
    return {"id": event_id, "summary": "Synthetic class", "start": {"dateTime": start},
            "end": {"dateTime": end}, **extra}


LOWER, UPPER = "2026-10-20T00:00:00+08:00", "2026-10-27T00:00:00+08:00"


def test_calendar_normalizes_busy_instances_all_day_and_filters_nonblocking_events():
    event = calendar_event("series_20261021", recurringEventId="series")
    second_instance = calendar_event("series_20261022", "2026-10-22T10:00:00+08:00",
                                     "2026-10-22T11:00:00+08:00", recurringEventId="series")
    session = FakeSession([
        FakeResponse({"timeZone": "Asia/Singapore", "items": [
            event, {"id": "day", "start": {"date": "2026-10-23"},
                    "end": {"date": "2026-10-25"}},
            calendar_event("free", transparency="transparent"),
            {"id": "cancelled", "status": "cancelled"},
            calendar_event("declined", attendees=[{"self": True, "responseStatus": "declined"}]),
        ], "nextPageToken": "page2"}),
        FakeResponse({"timeZone": "Asia/Singapore", "items": [event, second_instance]}),
    ])
    result = workspace.GoogleWorkspaceClient(session).list_events("primary", LOWER, UPPER)
    assert [item["id"] for item in result] == ["series_20261021", "series_20261022", "day"]
    assert result[-1]["is_all_day"]
    assert result[-1]["start"] == "2026-10-23T00:00:00+08:00"
    assert result[-1]["end"] == "2026-10-25T00:00:00+08:00"
    assert session.calls[0][1]["params"]["singleEvents"] == "true"


def test_all_day_blocks_follow_calendar_timezone_across_dst_change():
    session = FakeSession([FakeResponse({"timeZone": "America/New_York", "items": [
        {"id": "allday", "start": {"date": "2026-11-01"}, "end": {"date": "2026-11-02"}},
    ]})])
    event = workspace.GoogleWorkspaceClient(session).list_events(
        "primary", "2026-11-01T00:00:00-04:00", "2026-11-03T00:00:00-05:00",
    )[0]
    start, end = map(datetime.fromisoformat, (event["start"], event["end"]))
    assert (end - start).total_seconds() == 25 * 3600
    assert event["is_all_day"]


def test_duplicate_instance_uses_newer_update_and_cancelled_update_removes_old_block():
    old = calendar_event("event", updated="2026-10-20T12:00:00Z")
    removed = {"id": "event", "status": "cancelled", "updated": "2026-10-20T13:00:00Z"}
    session = FakeSession([FakeResponse({"timeZone": "Asia/Singapore", "items": [old, removed, old]})])
    assert workspace.GoogleWorkspaceClient(session).list_events("primary", LOWER, UPPER) == []


@pytest.mark.parametrize("event,zone", [
    ({"id": "day", "start": {"date": "2026-10-21"}, "end": {"date": "2026-10-22"}}, None),
    (calendar_event("bad", end="2026-10-21T09:00:00+08:00"), "Asia/Singapore"),
    (calendar_event("gap", "2026-03-08T02:30:00", "2026-03-08T03:30:00"), "America/New_York"),
    (calendar_event("fold", "2026-11-01T01:10:00", "2026-11-01T02:30:00"), "America/New_York"),
])
def test_invalid_or_ambiguous_events_fail_instead_of_silently_freeing_time(event, zone):
    session = FakeSession([FakeResponse({"timeZone": zone, "items": [event]})])
    start = event["start"].get("dateTime", "2026-10-21")[:10]
    with pytest.raises(workspace.GoogleWorkspaceError):
        workspace.GoogleWorkspaceClient(session).list_events(
            "primary", start + "T00:00:00Z", start + "T23:59:00Z",
        )


def test_calendar_id_is_escaped_and_timezone_naive_query_is_rejected():
    session = FakeSession([FakeResponse({})])
    client = workspace.GoogleWorkspaceClient(session)
    assert client.list_events("en.singapore#holiday@group.v.calendar.google.com", LOWER, UPPER) == []
    assert "%23holiday%40group.v.calendar.google.com/events" in session.calls[0][0]
    for calendar_id in ("https://untrusted.example", "../primary", "primary?fields=all", ".."):
        with pytest.raises(workspace.GoogleWorkspaceError):
            client.list_events(calendar_id, LOWER, UPPER)
    with pytest.raises(workspace.GoogleWorkspaceError):
        client.list_events("primary", "2026-10-21T00:00:00", UPPER)
    assert len(session.calls) == 1


def test_calendar_list_returns_titles_timezones_and_ignores_missing_optional_fields():
    session = FakeSession([
        FakeResponse({"items": [{"id": "student@example.test", "summary": "Original",
                                  "summaryOverride": "My calendar", "timeZone": "Asia/Singapore",
                                  "primary": True, "accessRole": "owner"}],
                      "nextPageToken": "next"}),
        FakeResponse({"items": [{"id": "shared@example.test"}]}),
    ])
    result = workspace.GoogleWorkspaceClient(session).list_calendars()
    assert result[0] == {"id": "student@example.test", "title": "My calendar",
                         "time_zone": "Asia/Singapore", "primary": True, "access_role": "owner"}
    assert result[1]["title"] == "shared@example.test"


def test_calendar_pagination_failure_does_not_return_busy_subset():
    session = FakeSession([
        FakeResponse({"items": [calendar_event("event")], "nextPageToken": "next"}),
        FakeResponse({"error": "private"}, status=500),
    ])
    with pytest.raises(workspace.GoogleWorkspaceError):
        workspace.GoogleWorkspaceClient(session).list_events("primary", LOWER, UPPER)


def test_aware_datetime_query_accepts_datetime_instances():
    session = FakeSession([FakeResponse({})])
    workspace.GoogleWorkspaceClient(session).list_events(
        "primary", datetime(2026, 10, 20, tzinfo=UTC),
        datetime(2026, 10, 21, tzinfo=UTC),
    )
    assert session.calls[0][1]["params"]["timeMin"] == "2026-10-20T00:00:00+00:00"
