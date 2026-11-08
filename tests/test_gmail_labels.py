"""Gmail labeling is verified offline; these tests never access an account."""

import json
import sys
import types
from dataclasses import replace
from types import SimpleNamespace

import pytest

from studentflow import gmail_labels as labels
from studentflow import triage_sync as sync

ACCOUNT = "student@example.test"


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
        self.responses, self.calls = list(responses), []

    def _call(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url, **kwargs):
        return self._call("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._call("POST", url, kwargs)

    def close(self):
        pass


def label_list():
    return {"labels": [
        {"id": "INBOX", "name": "INBOX", "type": "system"},
        {"id": "Label_other", "name": "Personal", "type": "user"},
        *[{"id": "Label_" + category, "name": name, "type": "user"}
          for category, name in labels.MANAGED_LABELS.items()],
    ]}


def test_http_labels_only_exact_names_and_no_system_label_write():
    response = Response(raw=b"", status=204)
    session = Session([Response(label_list()), response])
    client = labels.GmailLabelClient(session)
    mapping = client.label_ids()
    client.set_category("abc123", "needs_reply", mapping)
    method, url, options = session.calls[-1]
    assert method == "POST" and url.endswith("/messages/batchModify")
    assert options["json"] == {"ids": ["abc123"], "addLabelIds": ["Label_needs_reply"],
                               "removeLabelIds": ["Label_needs_action", "Label_ignore"]}
    assert options["allow_redirects"] is False and options["timeout"] == 30
    assert response.closed


def test_unverified_label_ids_cannot_be_injected():
    client = labels.GmailLabelClient(Session([]))
    for mapping in [{"needs_reply": "INBOX"}, {"needs_reply": "Label_unverified"}]:
        with pytest.raises(labels.GmailLabelError, match="verified"):
            client.set_category("abc123", "needs_reply", mapping)
    assert client._session.calls == []


@pytest.mark.parametrize("rows", [
    [{"name": labels.MANAGED_LABELS["ignore"], "id": "SPAM", "type": "system"}],
    [{"name": labels.MANAGED_LABELS["ignore"], "id": "Label_x", "type": "user"}] * 2,
    [{"name": labels.MANAGED_LABELS["ignore"], "id": "../bad", "type": "user"}],
])
def test_ambiguous_or_nonuser_categories_rejected(rows):
    with pytest.raises(labels.GmailLabelError):
        labels.GmailLabelClient(Session([Response({"labels": rows})])).label_ids()


def test_category_creation_is_restricted_and_exact():
    row = {"id": "Label_new", "name": labels.MANAGED_LABELS["ignore"], "type": "user"}
    session = Session([Response(row)])
    client = labels.GmailLabelClient(session)
    with pytest.raises(labels.GmailLabelError):
        client.create_label("Trash")
    assert not session.calls
    assert client.create_label("ignore") == "Label_new"
    assert session.calls[0][2]["json"] == {
        "name": "StudentFlow/No action needed", "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }


def test_real_create_response_without_type_is_accepted_but_system_type_is_not():
    # Gmail's labels.create response omits "type" (labels.list includes it).
    real = {"id": "Label_2", "name": labels.MANAGED_LABELS["needs_action"],
            "labelListVisibility": "labelShow", "messageListVisibility": "show"}
    assert labels.GmailLabelClient(Session([Response(real)])).create_label("needs_action") == "Label_2"
    system = dict(real, type="system")
    with pytest.raises(labels.GmailLabelError):
        labels.GmailLabelClient(Session([Response(system)])).create_label("needs_action")


@pytest.mark.parametrize("value", [
    {"id": "different", "labelIds": ["INBOX"]},
    {"id": "abc123", "labelIds": ["DRAFT"]},
    {"id": "abc123", "labelIds": "INBOX"},
])
def test_message_identity_and_drafts_checked(value):
    with pytest.raises(labels.GmailLabelError):
        labels.GmailLabelClient(Session([Response(value)])).message_labels("abc123")


@pytest.mark.parametrize("response", [
    TimeoutError("secret access token"), Response(status=500), Response(raw=b"secret not JSON"),
])
def test_provider_errors_are_sanitized(response):
    with pytest.raises(labels.GmailLabelError) as error:
        labels.GmailLabelClient(Session([response])).account()
    assert "secret" not in str(error.value)


class Provider:
    def __init__(self, account=ACCOUNT, mapping=None):
        self.address = account
        self.mapping = dict(mapping or {})
        self.messages = {"m1": {"INBOX", "UNREAD", "Label_personal"}, "m2": {"INBOX"}}
        self.calls, self.writes = [], []
        self.fail_message = None
        self.apply_before_failure = False

    def account(self):
        self.calls.append("account")
        return self.address

    def label_ids(self):
        self.calls.append("labels")
        return dict(self.mapping)

    def message_labels(self, identifier):
        self.calls.append(identifier)
        if "DRAFT" in self.messages[identifier]:
            raise labels.GmailLabelError("Draft messages cannot be classified as received mail.")
        return set(self.messages[identifier])

    def create_label(self, category):
        self.writes.append(("create", category))
        self.mapping[category] = "Label_" + category
        return self.mapping[category]

    def set_category(self, identifier, category, mapping):
        self.writes.append((identifier, category))
        if identifier != self.fail_message or self.apply_before_failure:
            self.messages[identifier].difference_update(mapping.values())
            self.messages[identifier].add(mapping[category])
        if identifier == self.fail_message:
            raise labels.GmailLabelError("Gmail did not confirm this label operation.")


def items():
    return [
        {"gmail_id": "m1", "category": "needs_reply", "subject": "Confirm the meeting",
         "source_fingerprint": "bodyhash:version1"},
        {"gmail_id": "m2", "category": "ignore", "subject": "Informational update",
         "source_fingerprint": "secondhash:version1"},
    ]


def preview(provider):
    return sync.prepare_label_sync(provider, account=ACCOUNT, items=items())


def apply(provider, prepared, tmp_path, **changes):
    kwargs = {"account": ACCOUNT, "items": items(), "data_dir": tmp_path, "confirmed": True}
    kwargs.update(changes)
    return sync.apply_label_sync(provider, prepared, **kwargs)


def test_preview_read_only_shows_additions_removals_and_label_creation():
    provider = Provider(mapping={"needs_action": "Label_action"})
    provider.messages["m1"].add("Label_action")
    prepared = preview(provider)
    assert provider.writes == []
    first = prepared.items[0]
    assert first.add_label == "StudentFlow/Needs reply"
    assert first.remove_labels == ("StudentFlow/Needs action",)
    assert prepared.missing_labels == ("StudentFlow/No action needed", "StudentFlow/Needs reply")
    with pytest.raises(AttributeError):
        prepared.account = "someoneelse@example.test"


def test_confirmation_required_before_any_remote_or_local_action(tmp_path):
    provider = Provider()
    prepared = preview(provider)
    provider.calls.clear()
    with pytest.raises(labels.GmailLabelError, match="confirm"):
        apply(provider, prepared, tmp_path, confirmed=False)
    assert provider.calls == [] and provider.writes == []
    assert list(tmp_path.iterdir()) == []


def test_sync_and_repeated_sync_preserve_all_other_labels(tmp_path):
    provider = Provider(mapping={"needs_action": "Label_action"})
    provider.messages["m1"].add("Label_action")
    prepared = preview(provider)
    result = apply(provider, prepared, tmp_path)
    assert result["status"] == "synced" and result["completed_ids"] == ["m1", "m2"]
    assert provider.messages["m1"] == {"INBOX", "UNREAD", "Label_personal", "Label_needs_reply"}
    assert sync.label_sync_status(tmp_path, prepared.fingerprint) == result
    initial_writes = list(provider.writes)
    repeated = apply(provider, prepared, tmp_path)
    assert repeated["status"] == "synced" and repeated["unchanged_count"] == 2
    assert provider.writes == initial_writes
    journal = (tmp_path / "gmail_label_sync.sqlite3").read_bytes()
    assert ACCOUNT.encode() not in journal and b"Confirm the meeting" not in journal


@pytest.mark.parametrize("changed", ["account", "category", "fingerprint", "selected"])
def test_changed_preview_inputs_block_before_network(tmp_path, changed):
    provider = Provider()
    prepared = preview(provider)
    provider.calls.clear()
    selection, account = items(), ACCOUNT
    if changed == "account":
        account = "other@example.test"
    elif changed == "category":
        selection[0]["category"] = "ignore"
    elif changed == "fingerprint":
        selection[0]["source_fingerprint"] = "bodyhash:version2"
    else:
        selection.pop()
    with pytest.raises(labels.GmailLabelError, match="changed"):
        apply(provider, prepared, tmp_path, account=account, items=selection)
    assert provider.calls == [] and provider.writes == []


def test_switched_connected_account_never_writes(tmp_path):
    provider = Provider()
    prepared = preview(provider)
    provider.address = "other@example.test"
    with pytest.raises(labels.GmailLabelError, match="same account"):
        apply(provider, prepared, tmp_path)
    assert provider.writes == []


def test_tampered_preview_never_writes(tmp_path):
    provider = Provider()
    prepared = preview(provider)
    with pytest.raises(labels.GmailLabelError, match="changed"):
        apply(provider, replace(prepared, fingerprint="0" * 64), tmp_path)
    assert provider.writes == []


def test_gmail_category_change_requires_new_preview_before_any_write(tmp_path):
    provider = Provider(mapping={"needs_action": "Label_action"})
    prepared = preview(provider)
    provider.messages["m2"].add("Label_action")
    with pytest.raises(labels.GmailLabelError, match="category changed"):
        apply(provider, prepared, tmp_path)
    assert provider.writes == []


def test_renamed_or_deleted_managed_label_invalidates_review(tmp_path):
    provider = Provider(mapping={"needs_action": "Label_action"})
    prepared = preview(provider)
    provider.mapping["needs_action"] = "Label_recreated"
    with pytest.raises(labels.GmailLabelError, match="labels changed"):
        apply(provider, prepared, tmp_path)
    assert provider.writes == []


def test_draft_message_blocks_whole_selection_before_creation(tmp_path):
    provider = Provider()
    prepared = preview(provider)
    provider.messages["m2"].add("DRAFT")
    with pytest.raises(labels.GmailLabelError, match="Draft"):
        apply(provider, prepared, tmp_path)
    assert provider.writes == []


def test_partial_failure_keeps_success_and_can_retry_safely(tmp_path):
    provider = Provider()
    prepared = preview(provider)
    provider.fail_message = "m2"
    result = apply(provider, prepared, tmp_path)
    assert result["status"] == "partial"
    assert result["completed_ids"] == ["m1"] and result["failed_ids"] == ["m2"]
    assert sync.label_sync_status(tmp_path, prepared.fingerprint)["status"] == "partial"
    provider.fail_message = None
    result = apply(provider, prepared, tmp_path)
    assert result["status"] == "synced"
    assert provider.writes.count(("m1", "needs_reply")) == 1
    assert provider.writes.count(("m2", "ignore")) == 2


def test_timeout_after_remote_success_is_reconciled_without_duplicate_write(tmp_path):
    provider = Provider()
    prepared = preview(provider)
    provider.fail_message, provider.apply_before_failure = "m2", True
    assert apply(provider, prepared, tmp_path)["status"] == "partial"
    provider.fail_message = None
    result = apply(provider, prepared, tmp_path)
    assert result["status"] == "synced" and result["unchanged_count"] == 2
    assert provider.writes.count(("m2", "ignore")) == 1


def test_failed_label_creation_is_not_reported_as_synced(tmp_path, monkeypatch):
    provider = Provider()
    prepared = preview(provider)

    def fail(category):
        raise labels.GmailLabelError("Gmail did not confirm the new label.")

    monkeypatch.setattr(provider, "create_label", fail)
    result = apply(provider, prepared, tmp_path)
    assert result["status"] == "failed" and result["failed_ids"] == ["m1", "m2"]
    assert result["completed_ids"] == [] and provider.writes == []


def test_journal_failure_blocks_all_writes(tmp_path, monkeypatch):
    provider = Provider()
    prepared = preview(provider)

    def fail(*args, **kwargs):
        raise sync.sqlite3.OperationalError("secret path")

    monkeypatch.setattr(sync.sqlite3, "connect", fail)
    with pytest.raises(labels.GmailLabelError) as error:
        apply(provider, prepared, tmp_path)
    assert "secret" not in str(error.value) and provider.writes == []


def fake_google(monkeypatch):
    state = {"events": [], "valid": True, "refresh_error": None,
             "interactive_scopes": list(labels.LABEL_SCOPES)}
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
            assert scopes == labels.LABEL_SCOPES
            state["events"].append("flow")
            return cls()

        def run_local_server(self, **kwargs):
            state["events"].append(kwargs)
            return Credentials()

    transport = types.ModuleType("google.auth.transport.requests")
    transport.AuthorizedSession = lambda credentials, **kwargs: Session([])
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


def test_connect_without_token_never_opens_browser(tmp_path, monkeypatch):
    state, config, _ = fake_google(monkeypatch)
    with pytest.raises(labels.GmailLabelAuthError, match="Connect Gmail labels"):
        labels.connect_labels(write_client(tmp_path, config), tmp_path / labels.TOKEN_FILENAME)
    assert state["events"] == []


def test_explicit_label_authorization_is_isolated_and_restores_silently(tmp_path, monkeypatch):
    state, config, _ = fake_google(monkeypatch)
    original = tmp_path / "gmail_token.json"
    original.write_text("existing-read-only-token", encoding="utf-8")
    sending = tmp_path / "gmail_send_token.json"
    sending.write_text("existing-send-token", encoding="utf-8")
    token = tmp_path / labels.TOKEN_FILENAME
    client_path = write_client(tmp_path, config)
    labels.connect_labels(client_path, token, interactive=True).close()
    assert state["events"][0] == "flow"
    options = state["events"][1]
    assert options["host"] == "127.0.0.1" and options["include_granted_scopes"] == "false"
    assert original.read_text() == "existing-read-only-token"
    assert sending.read_text() == "existing-send-token"
    assert json.loads(token.read_text())["scopes"] == labels.LABEL_SCOPES
    state["events"].clear()
    labels.connect_labels(client_path, token).close()
    assert state["events"] == ["cached"]


@pytest.mark.parametrize("scopes", [[], labels.gmail.SCOPES,
                                     [*labels.LABEL_SCOPES, "https://mail.google.com/"]])
def test_missing_or_broader_permissions_are_rejected(scopes):
    with pytest.raises(labels.GmailLabelAuthError):
        labels._check_credentials(SimpleNamespace(scopes=scopes, granted_scopes=scopes, valid=True))


def test_invalid_cached_token_is_not_refreshed_or_reauthorized(tmp_path, monkeypatch):
    state, config, credentials = fake_google(monkeypatch)
    token = tmp_path / labels.TOKEN_FILENAME
    info = json.loads(credentials().to_json())
    info["scopes"] = labels.gmail.SCOPES
    token.write_text(json.dumps(info))
    original = token.read_bytes()
    with pytest.raises(labels.GmailLabelAuthError):
        labels.connect_labels(write_client(tmp_path, config), token)
    assert state["events"] == [] and token.read_bytes() == original


def test_existing_read_only_token_path_cannot_be_repurposed(tmp_path):
    with pytest.raises(labels.GmailLabelError, match="separate"):
        labels.connect_labels(tmp_path / "client.json", tmp_path / "gmail_token.json")


def test_refresh_failure_preserves_token_and_hides_private_error(tmp_path, monkeypatch):
    state, config, credentials = fake_google(monkeypatch)
    token = tmp_path / labels.TOKEN_FILENAME
    token.write_text(credentials().to_json())
    original = token.read_bytes()
    state.update(valid=False, refresh_error=TimeoutError("secret token"))
    with pytest.raises(labels.GmailLabelError) as error:
        labels.connect_labels(write_client(tmp_path, config), token)
    assert "secret" not in str(error.value) and token.read_bytes() == original
    assert state["events"] == ["cached", "refresh"]
