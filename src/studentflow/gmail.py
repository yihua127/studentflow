"""Read the signed-in user's Gmail inbox through desktop OAuth, without mail writes.

Credentials and tokens stay in caller-selected local files. No model is called here.
The Google libraries are imported only when the user connects an account.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URIS = {
    "https://accounts.google.com/o/oauth2/auth",
    "https://accounts.google.com/o/oauth2/v2/auth",
}
MAX_EMAIL_BYTES = 20 * 1024 * 1024
MAX_RAW_CHARS = 4 * ((MAX_EMAIL_BYTES + 2) // 3)
MAX_JSON_BYTES = MAX_RAW_CHARS + 64 * 1024
MAX_PAGE_TOKEN_CHARS = 4096
_MESSAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")


class GmailError(Exception):
    """A user-facing error that never contains token values or server responses."""


def _load_json(path: Path, description: str) -> dict:
    try:
        with path.open("rb") as source:
            raw = source.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise ValueError("oversized configuration")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise TypeError("configuration must be an object")
        return result
    except (OSError, ValueError, TypeError, RecursionError):
        raise GmailError(f"Could not read {description}. Check the local JSON file.") from None


def _client_config(path: Path) -> dict:
    if not path.is_file():
        raise GmailError("Gmail connection settings are missing. Add the desktop OAuth configuration file to connect.")
    config = _load_json(path, "Gmail connection settings")
    client = config.get("installed")
    if not isinstance(client, dict):
        raise GmailError("Use a Google OAuth Desktop app configuration.")
    if client.get("auth_uri") not in AUTH_URIS or client.get("token_uri") != TOKEN_URI:
        raise GmailError("The Gmail authorization address is invalid. Download the configuration from Google again.")
    if not all(isinstance(client.get(key), str) and client[key] for key in (
        "client_id", "client_secret",
    )):
        raise GmailError("The Gmail connection settings are incomplete. Download the configuration again.")
    # Pass only validated endpoint and client fields to the OAuth library.
    return {"installed": {key: client[key] for key in (
        "client_id", "client_secret", "auth_uri", "token_uri",
    )}}


def _scope_set(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(value.split())
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return set(value)
    return set()


def _check_token_info(info: dict, client: dict) -> None:
    if (
        info.get("client_id") != client["client_id"]
        or info.get("client_secret") != client["client_secret"]
        or info.get("token_uri") != TOKEN_URI
        or _scope_set(info.get("scopes")) != set(SCOPES)
    ):
        raise GmailError("The saved connection does not match this app or its read-only permissions. Reconnect Gmail.")


def _check_credentials(credentials: Any) -> None:
    granted = getattr(credentials, "granted_scopes", None)
    if _scope_set(credentials.scopes) != set(SCOPES) or (
        granted is not None and _scope_set(granted) != set(SCOPES)
    ):
        raise GmailError("Gmail read-only permissions were not granted as requested. Check the connection settings.")
    if not credentials.valid:
        raise GmailError("Google did not return a valid authorization. Reconnect Gmail.")


def _save_token(path: Path, credentials: Any) -> None:
    """Replace the local token atomically; it is sensitive plaintext, not encrypted."""
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".gmail-", delete=False,
        ) as destination:
            temporary = Path(destination.name)
            destination.write(credentials.to_json())
        os.replace(temporary, path)
    except OSError:
        raise GmailError("Gmail authorization succeeded, but the connection could not be saved. Check folder permissions.") from None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink(missing_ok=True)


def connect(
    credentials_path: Path, token_path: Path, interactive: bool = False,
) -> GmailMailbox:
    """Connect using existing consent, or open Google login only if explicitly requested."""
    config = _client_config(Path(credentials_path))
    token_path = Path(token_path)
    try:
        from google.auth.transport.requests import AuthorizedSession, Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise GmailError("The Gmail connection components are unavailable. Reinstall the app dependencies.") from None

    credentials = None
    if token_path.is_file() and not interactive:
        info = _load_json(token_path, "saved Gmail connection")
        _check_token_info(info, config["installed"])
        try:
            credentials = Credentials.from_authorized_user_info(info, SCOPES)
        except (ValueError, TypeError):
            raise GmailError("The saved Gmail connection is invalid. Reconnect Gmail.") from None

    if not interactive:
        if credentials is None:
            raise GmailError("Gmail is not connected. Click Connect Gmail to sign in.")
        if not credentials.valid:
            if not credentials.refresh_token:
                raise GmailError("Your Gmail authorization has expired. Reconnect Gmail.")
            try:
                request = Request()

                def limited_request(*args: Any, **kwargs: Any) -> Any:
                    kwargs["timeout"] = 30
                    return request(*args, **kwargs)

                credentials.refresh(limited_request)
            except Exception:  # noqa: BLE001 - OAuth errors may contain credential material.
                raise GmailError("Could not refresh your Gmail authorization. Check your connection or reconnect Gmail.") from None
            _check_credentials(credentials)
            _save_token(token_path, credentials)
    else:
        try:
            flow = InstalledAppFlow.from_client_config(config, SCOPES)
            credentials = flow.run_local_server(
                host="127.0.0.1", port=0, timeout_seconds=180,
                prompt="select_account consent",
                authorization_prompt_message="Complete Google sign-in in your browser.",
                success_message="Gmail is connected. You can return to StudentFlow.",
            )
        except Exception:  # noqa: BLE001 - Do not surface OAuth callback or token details.
            raise GmailError("Google sign-in did not finish. Check your connection and test-user access, then try again.") from None
        _check_credentials(credentials)
        _save_token(token_path, credentials)

    _check_credentials(credentials)
    return GmailMailbox(AuthorizedSession(credentials, max_refresh_attempts=0))


def _validate_message_id(message_id: str) -> str:
    if not isinstance(message_id, str) or not _MESSAGE_ID.fullmatch(message_id):
        raise GmailError("The message ID is invalid. Refresh the inbox.")
    return message_id


def _validate_page_token(page_token: Any) -> str:
    """Keep Gmail cursors opaque; validate shape without decoding or rewriting."""
    if (
        not isinstance(page_token, str)
        or not page_token.strip()
        or len(page_token) > MAX_PAGE_TOKEN_CHARS
        or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in page_token)
    ):
        raise GmailError("The inbox page could not be read. Refresh the inbox.")
    return page_token


class GmailMailbox:
    """Small read-only Gmail API client; injectable session enables offline tests."""

    def __init__(self, session: Any) -> None:
        self._session = session

    def close(self) -> None:
        self._session.close()

    def _get_json(self, endpoint: str, params: dict | None = None, limit: int = 256_000) -> dict:
        response = None
        try:
            response = self._session.get(
                API_BASE + endpoint, params=params, timeout=30,
                max_allowed_time=30, allow_redirects=False, stream=True,
            )
            if response.status_code == 401:
                raise GmailError("Your Gmail authorization has expired. Reconnect Gmail.")
            if response.status_code == 403:
                raise GmailError("Gmail denied access. Check that the Gmail API is enabled and your account has test-user access and read-only permission.")
            if response.status_code == 429:
                raise GmailError("Too many Gmail requests. Try again later.")
            if response.status_code != 200:
                raise GmailError("Gmail is temporarily unavailable. Check your connection and try again.")
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > limit:
                raise GmailError("The Gmail response is too large. Nothing was imported.")
            raw = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if len(raw) + len(chunk) > limit:
                    raise GmailError("The Gmail response is too large. Nothing was imported.")
                raw.extend(chunk)
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise GmailError("Gmail returned an unreadable response. Try again.")
            return result
        except GmailError:
            raise
        except Exception:  # noqa: BLE001 - Suppress request headers and private response bodies.
            raise GmailError("Could not read Gmail. Check your connection and authorization, then try again.") from None
        finally:
            if response is not None:
                response.close()

    def profile(self) -> dict:
        result = self._get_json("/profile", limit=64_000)
        address = result.get("emailAddress")
        if not isinstance(address, str) or len(address) > 320 or "@" not in address:
            raise GmailError("Could not verify the connected Gmail account. Reconnect Gmail.")
        return {"email_address": address}

    def list_messages(self, limit: int = 5) -> list[dict]:
        """Return the first page of headers for existing callers."""
        return self.list_message_page(limit)["messages"]

    def list_message_page(self, limit: int = 20, page_token: str | None = None) -> dict:
        """Read one inbox page of headers and its optional continuation cursor."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise GmailError("Choose between 1 and 20 messages at a time.")
        params = {
            "labelIds": "INBOX", "maxResults": limit, "includeSpamTrash": "false",
        }
        if page_token is not None:
            params["pageToken"] = _validate_page_token(page_token)
        result = self._get_json("/messages", params)
        next_page_token = result.get("nextPageToken")
        if next_page_token is None or next_page_token == "":
            next_page_token = None
        else:
            next_page_token = _validate_page_token(next_page_token)
        messages = result.get("messages", [])
        if not isinstance(messages, list) or len(messages) > limit:
            raise GmailError("The inbox list could not be read. Refresh the inbox.")
        output = []
        for message in messages:
            message_id = _validate_message_id(message.get("id") if isinstance(message, dict) else None)
            metadata = self._get_json(f"/messages/{message_id}", {
                "format": "metadata", "metadataHeaders": ["Subject", "From", "Date"],
            })
            payload = metadata.get("payload", {})
            headers = payload.get("headers", []) if isinstance(payload, dict) else None
            if not isinstance(headers, list) or len(headers) > 200:
                raise GmailError("The message details could not be read. Refresh the inbox.")
            selected = {}
            for header in headers:
                if not isinstance(header, dict):
                    continue
                name, value = header.get("name"), header.get("value")
                if isinstance(name, str) and isinstance(value, str):
                    selected.setdefault(name.lower(), value[:8192])
            output.append({
                "id": message_id, "subject": selected.get("subject", "(No subject)"),
                "sender": selected.get("from", ""), "sent_at": selected.get("date", ""),
            })
        return {"messages": output, "next_page_token": next_page_token}

    def fetch_eml(self, message_id: str) -> bytes:
        message_id = _validate_message_id(message_id)
        result = self._get_json(
            f"/messages/{message_id}", {"format": "raw"}, limit=MAX_JSON_BYTES,
        )
        raw = result.get("raw")
        if not isinstance(raw, str) or not raw or len(raw) > MAX_RAW_CHARS:
            raise GmailError("The message is empty or exceeds 20 MB. Nothing was imported.")
        if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", raw):
            raise GmailError("The message could not be decoded. Nothing was imported.")
        try:
            decoded = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
        except (ValueError, binascii.Error):
            raise GmailError("The message could not be decoded. Nothing was imported.") from None
        if not decoded or len(decoded) > MAX_EMAIL_BYTES:
            raise GmailError("The message is empty or exceeds 20 MB. Nothing was imported.")
        return decoded
