"""Isolated Gmail label permission; only configured StudentFlow labels are writable.

The OAuth grant is gmail.modify because Gmail requires it to label messages.
This adapter deliberately exposes no sending, archiving, deleting or read-state
operation. Model tools never receive this client or its credentials.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from studentflow import gmail
from studentflow.email_label_catalog import gmail_label_names, validate_catalog
from studentflow.google_workspace import _quiet_google

LABEL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
TOKEN_FILENAME = "gmail_labels_token.json"
MANAGED_LABELS = {
    "needs_reply": "StudentFlow/Needs reply",
    "needs_action": "StudentFlow/Needs action",
    "ignore": "StudentFlow/No action needed",
}
_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_ACCOUNT = re.compile(r"[^\s<>@,;:\"\\]+@[^\s<>@,;:\"\\]+\.[^\s<>@,;:\"\\]+\Z")


def _created_user_label(row: dict) -> bool:
    # Gmail's labels.create response carries id and name but omits "type"; only
    # labels.list reports it. A present type must still be a user label.
    return row.get("type", "user") == "user"


class GmailLabelError(gmail.GmailError):
    """Safe error without credentials or private response content."""


class GmailLabelAuthError(GmailLabelError):
    """The user must explicitly reconnect label permission."""


def normalized_account(value: str) -> str:
    if not isinstance(value, str) or len(value) > 320 or not _ACCOUNT.fullmatch(value.strip()):
        raise GmailLabelError("The Gmail account could not be verified. Reconnect Gmail.")
    return value.strip().casefold()


def message_id(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise GmailLabelError("Reload the selected Gmail messages before reviewing labels.")
    return value


def _check_credentials(credentials: Any) -> None:
    granted = getattr(credentials, "granted_scopes", None)
    if (gmail._scope_set(credentials.scopes) != set(LABEL_SCOPES)
            or (granted is not None and gmail._scope_set(granted) != set(LABEL_SCOPES))
            or not credentials.valid):
        raise GmailLabelAuthError("Gmail label permission was not granted. Reconnect label syncing.")


def connect_labels(credentials_path: Path, token_path: Path, interactive: bool = False):
    """Use a separate token; Google login occurs only after an explicit UI click."""
    token_path = Path(token_path)
    if token_path.name != TOKEN_FILENAME:
        raise GmailLabelError("Use the separate Gmail label connection file.")
    try:
        config = gmail._client_config(Path(credentials_path))
        from google.auth.transport.requests import AuthorizedSession, Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except gmail.GmailError as error:
        raise GmailLabelError(str(error)) from None
    except ImportError:
        raise GmailLabelError("The Google connection components are unavailable.") from None

    credentials = None
    if token_path.is_file() and not interactive:
        try:
            info = gmail._load_json(token_path, "saved Gmail label connection")
            client = config["installed"]
            if (any(info.get(key) != client[key] for key in (
                    "client_id", "client_secret", "token_uri"))
                    or gmail._scope_set(info.get("scopes")) != set(LABEL_SCOPES)):
                raise ValueError
            credentials = Credentials.from_authorized_user_info(info, LABEL_SCOPES)
        except (gmail.GmailError, ValueError, TypeError):
            raise GmailLabelAuthError("The Gmail label connection is invalid. Reconnect label syncing.") from None
    if interactive:
        try:
            with _quiet_google(allow_partial_scopes=True):
                flow = InstalledAppFlow.from_client_config(config, LABEL_SCOPES)
                credentials = flow.run_local_server(
                    host="127.0.0.1", port=0, timeout_seconds=180,
                    prompt="select_account consent", access_type="offline",
                    include_granted_scopes="false",
                    authorization_prompt_message="Complete Google sign-in in your browser.",
                    success_message="Gmail labels are connected. Return to StudentFlow.",
                )
        except Exception:  # noqa: BLE001 - OAuth errors may contain credentials.
            raise GmailLabelError("Google sign-in did not finish. Try connecting Gmail labels again.") from None
        _check_credentials(credentials)
        try:
            gmail._save_token(token_path, credentials)
        except gmail.GmailError:
            raise GmailLabelError("The Gmail label connection could not be saved.") from None
    else:
        if credentials is None:
            raise GmailLabelAuthError("Connect Gmail labels before syncing reviewed categories.")
        if not credentials.valid:
            if not credentials.refresh_token:
                raise GmailLabelAuthError("Gmail label authorization has expired. Reconnect label syncing.")
            try:
                request = Request()

                def limited_request(*args: Any, **kwargs: Any):
                    kwargs["timeout"] = 30
                    return request(*args, **kwargs)

                with _quiet_google():
                    credentials.refresh(limited_request)
            except Exception as error:  # noqa: BLE001 - Never expose token details.
                if gmail._requires_sign_in(error):
                    raise GmailLabelAuthError("Gmail label permission has expired. Reconnect label syncing.") from None
                raise GmailLabelError("Could not refresh Gmail labels. The saved connection is retained.") from None
            _check_credentials(credentials)
            try:
                gmail._save_token(token_path, credentials)
            except gmail.GmailError:
                raise GmailLabelError("The refreshed Gmail label connection could not be saved.") from None
    _check_credentials(credentials)
    return GmailLabelClient(AuthorizedSession(credentials, max_refresh_attempts=0))


class GmailLabelClient:
    """Small HTTP adapter with an injectable session for offline verification."""

    def __init__(self, session):
        self._session = session
        self._verified_label_ids = {}
        self._catalog_names = {}
        self._allowed_names = set()
        self._verified_named_ids = {}

    def configure_catalog(self, catalog):
        """Use only the validated account-local catalog supplied by the application."""
        try:
            checked = validate_catalog(catalog)
            self._catalog_names, self._allowed_names = gmail_label_names(checked)
        except ValueError:
            raise GmailLabelError("Label settings changed. Reload and review them again.") from None
        self._verified_named_ids = {}

    def managed_label_ids(self):
        """Read exact current and historical app label names; ignore unrelated names."""
        if not self._allowed_names:
            raise GmailLabelError("Load the saved label settings before syncing.")
        rows = self._request("GET", "/labels").get("labels", [])
        if not isinstance(rows, list) or len(rows) > 10_000:
            raise GmailLabelError("Gmail returned an invalid label list. Review labels again.")
        result = {}
        for row in rows:
            if not isinstance(row, dict):
                raise GmailLabelError("Gmail returned an invalid label list. Review labels again.")
            name = row.get("name")
            if not isinstance(name, str) or name not in self._allowed_names:
                continue
            identifier = row.get("id")
            if (row.get("type") != "user" or not isinstance(identifier, str)
                    or not _ID.fullmatch(identifier) or identifier in result.values() or name in result):
                raise GmailLabelError("StudentFlow labels are ambiguous. Check their names in Gmail.")
            result[name] = identifier
        self._verified_named_ids = dict(result)
        return result

    def create_named_label(self, name):
        if not isinstance(name, str) or name not in self._catalog_names.values():
            raise GmailLabelError("Only your current StudentFlow label names can be created.")
        row = self._request("POST", "/labels", body={
            "name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show",
        })
        identifier = row.get("id")
        if (row.get("name") != name or not _created_user_label(row)
                or not isinstance(identifier, str) or not _ID.fullmatch(identifier)):
            raise GmailLabelError("Gmail did not confirm the new label. Preview labels again.")
        self._verified_named_ids[name] = identifier
        return identifier

    def set_named_labels(self, gmail_id, desired_names, label_ids):
        """Set this message's app labels; system and unrelated labels are untouched."""
        identifier = message_id(gmail_id)
        if (not isinstance(desired_names, (list, tuple, set)) or not desired_names
                or any(not isinstance(name, str) or name not in self._catalog_names.values()
                       for name in desired_names)
                or not isinstance(label_ids, dict)
                or any(name not in label_ids for name in desired_names)
                or any(not isinstance(name, str) or name not in self._allowed_names
                       or not isinstance(value, str) or not _ID.fullmatch(value)
                       or self._verified_named_ids.get(name) != value
                       for name, value in label_ids.items())
                or len(set(label_ids.values())) != len(label_ids)):
            raise GmailLabelError("Only verified StudentFlow labels can be changed.")
        desired = set(desired_names)
        self._request("POST", "/messages/batchModify", body={
            "ids": [identifier], "addLabelIds": [label_ids[name] for name in sorted(desired)],
            "removeLabelIds": [value for name, value in label_ids.items() if name not in desired],
        }, empty=True)

    def close(self):
        self._session.close()

    def _request(self, method, endpoint, *, params=None, body=None, empty=False):
        response = None
        try:
            options = {"timeout": 30, "max_allowed_time": 30, "allow_redirects": False, "stream": True}
            with _quiet_google():
                if method == "GET":
                    response = self._session.get(gmail.API_BASE + endpoint, params=params, **options)
                else:
                    response = self._session.post(gmail.API_BASE + endpoint, json=body, **options)
            if response.status_code == 401:
                raise GmailLabelAuthError("Gmail label authorization expired. Reconnect label syncing.")
            if response.status_code == 403:
                raise GmailLabelError("Gmail denied label access. Check the Gmail API and label permission.")
            if response.status_code not in ({200, 204} if empty else {200}):
                raise GmailLabelError("Gmail did not confirm this label operation. Review the latest labels and retry.")
            if empty:
                return {}
            raw = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                raw.extend(chunk)
                if len(raw) > 2_000_000:
                    raise ValueError
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise TypeError
            return result
        except GmailLabelError:
            raise
        except Exception:  # noqa: BLE001 - Provider responses may contain private content.
            raise GmailLabelError("Gmail labels could not be verified. Review the latest labels and retry.") from None
        finally:
            if response is not None:
                response.close()

    def account(self):
        return normalized_account(self._request("GET", "/profile").get("emailAddress"))

    def label_ids(self) -> dict[str, str]:
        """Return only exact, user-owned labels in the StudentFlow namespace."""
        rows = self._request("GET", "/labels").get("labels", [])
        if not isinstance(rows, list) or len(rows) > 10_000:
            raise GmailLabelError("Gmail returned an invalid label list. Review labels again.")
        by_name = {value: key for key, value in MANAGED_LABELS.items()}
        result = {}
        for row in rows:
            if not isinstance(row, dict):
                raise GmailLabelError("Gmail returned an invalid label list. Review labels again.")
            name = row.get("name")
            category = by_name.get(name) if isinstance(name, str) else None
            if category is None:
                continue
            identifier = row.get("id")
            if (row.get("type") != "user" or not isinstance(identifier, str)
                    or not _ID.fullmatch(identifier) or identifier in result.values()
                    or category in result):
                raise GmailLabelError("StudentFlow labels are ambiguous. Check their names in Gmail.")
            result[category] = identifier
        self._verified_label_ids = dict(result)
        return result

    def message_labels(self, gmail_id: str) -> set[str]:
        identifier = message_id(gmail_id)
        row = self._request("GET", f"/messages/{identifier}", params={"format": "minimal"})
        labels = row.get("labelIds", [])
        if (row.get("id") != identifier or not isinstance(labels, list)
                or any(not isinstance(label, str) or not _ID.fullmatch(label) for label in labels)):
            raise GmailLabelError("The selected Gmail message could not be verified. Reload the inbox.")
        if "DRAFT" in labels:
            raise GmailLabelError("Draft messages cannot be classified as received mail.")
        return set(labels)

    def create_label(self, category: str) -> str:
        if not isinstance(category, str) or category not in MANAGED_LABELS:
            raise GmailLabelError("Only StudentFlow email categories can be synced.")
        row = self._request("POST", "/labels", body={
            "name": MANAGED_LABELS[category], "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        })
        identifier = row.get("id")
        if (row.get("name") != MANAGED_LABELS[category] or not _created_user_label(row)
                or not isinstance(identifier, str) or not _ID.fullmatch(identifier)):
            raise GmailLabelError("Gmail did not confirm the new StudentFlow label. Review labels again.")
        self._verified_label_ids[category] = identifier
        return identifier

    def set_category(self, gmail_id: str, category: str, label_ids: dict[str, str]):
        """Only managed user labels may be added/removed, never system labels."""
        identifier = message_id(gmail_id)
        if (not isinstance(category, str) or category not in MANAGED_LABELS
                or not isinstance(label_ids, dict) or category not in label_ids):
            raise GmailLabelError("The reviewed category label is not available. Review labels again.")
        if (any(key not in MANAGED_LABELS for key in label_ids)
                or any(not isinstance(value, str) or not _ID.fullmatch(value)
                       or self._verified_label_ids.get(key) != value
                       for key, value in label_ids.items())
                or len(set(label_ids.values())) != len(label_ids)):
            raise GmailLabelError("Only verified StudentFlow user labels can be changed.")
        self._request("POST", "/messages/batchModify", body={
            "ids": [identifier], "addLabelIds": [label_ids[category]],
            "removeLabelIds": [value for key, value in label_ids.items() if key != category],
        }, empty=True)
