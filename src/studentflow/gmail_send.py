"""Human-reviewed Gmail replies using separate, narrowly scoped authorization.

The agent cannot call this module. A UI review produces an immutable payload;
only an explicit send action may submit it. A durable local journal prevents
retries after success or an ambiguous network result. No message bodies or
credentials are written to the journal.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime, getaddresses
from pathlib import Path
from typing import Any

from studentflow import gmail
from studentflow.google_workspace import _quiet_google
from studentflow.reply_validation import unresolved_placeholders

SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"
SEND_SCOPES = [SEND_SCOPE, "openid", EMAIL_SCOPE]
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
TOKEN_FILENAME = "gmail_send_token.json"
_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_RFC_ID = re.compile(r"<[^<>\s@]+@[^<>\s@]+>\Z")
_ADDRESS = re.compile(r"[^\s<>@,;:\"\\]+@[^\s<>@,;:\"\\]+\.[^\s<>@,;:\"\\]+\Z")
_HEX = re.compile(r"[a-f0-9]{64}\Z")


class GmailSendError(gmail.GmailError):
    """A safe error without private server responses or authorization details."""


class GmailSendAuthError(GmailSendError):
    """Sending authorization requires a user-initiated reconnect."""


class GmailSendUncertain(GmailSendError):
    """Delivery is unknown; automatic or repeated submission must not occur."""


def _scope_set(value: Any) -> set[str]:
    scopes = gmail._scope_set(value)
    return {EMAIL_SCOPE if item == "email" else item for item in scopes}


def _check_credentials(credentials: Any) -> None:
    expected = set(SEND_SCOPES)
    granted = getattr(credentials, "granted_scopes", None)
    if (_scope_set(credentials.scopes) != expected
            or (granted is not None and _scope_set(granted) != expected)
            or not credentials.valid):
        raise GmailSendAuthError(
            "Gmail sending and account identity permissions were not granted. Reconnect sending."
        )


def connect_send(credentials_path: Path, token_path: Path, interactive: bool = False):
    """Load isolated sending consent; open Google login only on an explicit click.

openid/email identify the sending account without asking for additional mailbox
read/compose privileges. The usual read-only Gmail token is never modified.
"""
    token_path = Path(token_path)
    if token_path.name != TOKEN_FILENAME:
        raise GmailSendError("Use the separate Gmail sending connection file.")
    try:
        config = gmail._client_config(Path(credentials_path))
        from google.auth.transport.requests import AuthorizedSession, Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except gmail.GmailError as error:
        raise GmailSendError(str(error)) from None
    except ImportError:
        raise GmailSendError("The Google connection components are unavailable.") from None

    credentials = None
    if token_path.is_file() and not interactive:
        try:
            info = gmail._load_json(token_path, "saved Gmail sending connection")
            client = config["installed"]
            if (any(info.get(key) != client[key] for key in (
                    "client_id", "client_secret", "token_uri"))
                    or _scope_set(info.get("scopes")) != set(SEND_SCOPES)):
                raise ValueError
            credentials = Credentials.from_authorized_user_info(info, SEND_SCOPES)
        except (gmail.GmailError, ValueError, TypeError):
            raise GmailSendAuthError("The Gmail sending connection is invalid. Reconnect sending.") from None
    if interactive:
        try:
            with _quiet_google(allow_partial_scopes=True):
                flow = InstalledAppFlow.from_client_config(config, SEND_SCOPES)
                credentials = flow.run_local_server(
                    host="127.0.0.1", port=0, timeout_seconds=180,
                    prompt="select_account consent", access_type="offline",
                    include_granted_scopes="false",
                    authorization_prompt_message="Complete Google sign-in in your browser.",
                    success_message="Gmail sending is connected. Return to StudentFlow.",
                )
        except Exception:  # noqa: BLE001 - OAuth exceptions may contain secrets.
            raise GmailSendError("Google sign-in did not finish. Try connecting Gmail sending again.") from None
        _check_credentials(credentials)
        try:
            gmail._save_token(token_path, credentials)
        except gmail.GmailError:
            raise GmailSendError("The Gmail sending connection could not be saved.") from None
    else:
        if credentials is None:
            raise GmailSendAuthError("Connect Gmail sending before sending a reviewed reply.")
        if not credentials.valid:
            if not credentials.refresh_token:
                raise GmailSendAuthError("Gmail sending authorization has expired. Reconnect sending.")
            try:
                request = Request()

                def limited_request(*args: Any, **kwargs: Any):
                    kwargs["timeout"] = 30
                    return request(*args, **kwargs)

                with _quiet_google():
                    credentials.refresh(limited_request)
            except Exception as error:  # noqa: BLE001 - Never expose credentials.
                if gmail._requires_sign_in(error):
                    raise GmailSendAuthError("Gmail sending permission has expired. Reconnect sending.") from None
                raise GmailSendError("Could not refresh Gmail sending. The saved connection is retained.") from None
            _check_credentials(credentials)
            try:
                gmail._save_token(token_path, credentials)
            except gmail.GmailError:
                raise GmailSendError("The refreshed Gmail sending connection could not be saved.") from None
    _check_credentials(credentials)
    return GmailSendClient(AuthorizedSession(credentials, max_refresh_attempts=0))


def _single_address(value: str) -> str:
    if not isinstance(value, str) or len(value) > 1000 or any(ord(c) < 32 for c in value):
        raise GmailSendError("The reply needs one valid email recipient without header breaks.")
    try:
        addresses = getaddresses([value], strict=True)
    except (TypeError, ValueError):
        raise GmailSendError("The reply needs one valid email recipient.") from None
    if len(addresses) != 1 or not _ADDRESS.fullmatch(addresses[0][1]):
        raise GmailSendError("The reply needs one valid email recipient.")
    return addresses[0][1].casefold()


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _subject(value: str) -> str:
    try:
        value = str(make_header(decode_header(value))).strip()
    except (LookupError, UnicodeError, ValueError, TypeError):
        raise GmailSendError("The original email subject could not be decoded safely.") from None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise GmailSendError("The original email subject contains invalid header breaks.")
    while value.casefold().startswith("re:"):
        value = value[3:].lstrip()
    return value.casefold()


def _source_metadata(read_client, account: str, message_id: str) -> dict:
    if not isinstance(message_id, str) or not _ID.fullmatch(message_id):
        raise GmailSendError("Open the original Gmail message again before replying.")
    try:
        if _single_address(read_client.profile()["email_address"]) != account:
            raise GmailSendError("The inbox account changed. Open this email in its original account.")
        result = read_client._get_json(
            f"/messages/{message_id}",
            {"format": "metadata", "metadataHeaders": [
                "From", "Reply-To", "Subject", "Message-ID", "References",
            ]},
        )
    except GmailSendError:
        raise
    except Exception:  # noqa: BLE001 - Read failures may contain source or token text.
        raise GmailSendError("Could not verify the original email. Reconnect the inbox and try again.") from None
    if not isinstance(result, dict) or result.get("id") != message_id:
        raise GmailSendError("The original email could not be verified.")
    thread_id = result.get("threadId")
    if not isinstance(thread_id, str) or not _ID.fullmatch(thread_id):
        raise GmailSendError("The original Gmail conversation could not be verified.")
    payload = result.get("payload")
    if not isinstance(payload, dict):
        raise GmailSendError("The original email headers could not be verified.")
    headers = payload.get("headers", [])
    if not isinstance(headers, list) or len(headers) > 200:
        raise GmailSendError("The original email headers could not be verified.")
    selected = {}
    for header in headers:
        if not isinstance(header, dict):
            raise GmailSendError("The original email headers could not be verified.")
        name, value = header.get("name"), header.get("value")
        if not isinstance(name, str) or not isinstance(value, str) or len(value) > 8192:
            raise GmailSendError("The original email headers could not be verified.")
        name = name.casefold()
        if name in {"from", "reply-to", "subject", "message-id", "references"}:
            if name in selected:
                raise GmailSendError("The original email contains ambiguous reply headers.")
            selected[name] = re.sub(r"\r?\n[ \t]+", " ", value)
            if any(ord(c) < 32 for c in selected[name]):
                raise GmailSendError("The original email contains invalid reply headers.")
    recipient = _single_address(selected.get("reply-to") or selected.get("from", ""))
    rfc_id = selected.get("message-id", "").strip()
    if not _RFC_ID.fullmatch(rfc_id):
        raise GmailSendError("The original email has no valid Message-ID for a threaded reply.")
    references = selected.get("references", "").split()
    if len(references) > 100 or any(not _RFC_ID.fullmatch(item) for item in references):
        raise GmailSendError("The original email has invalid conversation references.")
    if rfc_id not in references:
        references.append(rfc_id)
    return {
        "thread_id": thread_id, "rfc_message_id": rfc_id,
        "references": tuple(references), "recipient": recipient,
        "original_subject": selected.get("subject", ""),
    }


@dataclass(frozen=True)
class ReplyPreview:
    sender: str
    recipient: str
    subject: str
    body: str
    source_message_id: str
    thread_id: str
    rfc_message_id: str
    references: tuple[str, ...]
    source_fingerprint: str
    fingerprint: str


def prepare_reply(read_client, *, source_account: str, source_message_id: str,
                  recipient: str, subject: str, body: str) -> ReplyPreview:
    """Read-only verification of the exact payload that the UI must show for review."""
    sender = _single_address(source_account)
    target = _single_address(recipient)
    if (not isinstance(subject, str) or not subject.strip() or len(subject) > 998
            or any(ord(c) < 32 for c in subject)):
        raise GmailSendError("Enter a non-empty reply subject without header breaks.")
    if (not isinstance(body, str) or not body.strip() or len(body) > 100_000
            or any(ord(c) < 32 and c not in "\r\n\t" for c in body)):
        raise GmailSendError("Enter a reply message of at most 100,000 characters.")
    if unresolved_placeholders(subject + "\n" + body):
        raise GmailSendError("Replace the remaining template placeholders before reviewing for sending.")
    metadata = _source_metadata(read_client, sender, source_message_id)
    if target != metadata["recipient"]:
        raise GmailSendError("The recipient must match the original email's verified Reply-To or From address.")
    if _subject(subject) != _subject(metadata["original_subject"]):
        raise GmailSendError("Keep the original subject, with an optional Re: prefix, to reply in this conversation.")
    source_fingerprint = _digest(metadata)
    content = [sender, source_message_id, target, subject, body, source_fingerprint]
    return ReplyPreview(sender, target, subject, body, source_message_id,
                        metadata["thread_id"], metadata["rfc_message_id"], metadata["references"],
                        source_fingerprint, _digest(content))


@contextmanager
def _journal(data_dir: Path):
    try:
        path = Path(data_dir) / "gmail_sends.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=5)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS sends (fingerprint TEXT PRIMARY KEY, "
                       "status TEXT NOT NULL, message_id TEXT, updated_at TEXT NOT NULL)")
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            yield db
    except (OSError, sqlite3.Error):
        raise GmailSendError("The local send record could not be read or saved. Sending is blocked.") from None


def reply_send_status(data_dir: Path, fingerprint: str) -> dict | None:
    if not isinstance(fingerprint, str) or not _HEX.fullmatch(fingerprint):
        raise GmailSendError("The reviewed reply is invalid. Review it again.")
    with _journal(data_dir) as db:
        row = db.execute("SELECT status, message_id FROM sends WHERE fingerprint=?",
                         (fingerprint,)).fetchone()
    return {"status": row[0], "id": row[1]} if row else None


def _reserve(data_dir: Path, fingerprint: str) -> dict | None:
    with _journal(data_dir) as db:
        row = db.execute("SELECT status,message_id FROM sends WHERE fingerprint=?",
                         (fingerprint,)).fetchone()
        if row and row[0] == "sent":
            return {"status": "already_sent", "id": row[1]}
        if row and row[0] in {"pending", "uncertain"}:
            raise GmailSendUncertain(
                "This reply may already have been sent. Check Gmail Sent; it will not be sent again."
            )
        db.execute("INSERT INTO sends VALUES (?, 'pending', NULL, ?) ON CONFLICT(fingerprint) "
                   "DO UPDATE SET status='pending', message_id=NULL, updated_at=excluded.updated_at",
                   (fingerprint, datetime.now(UTC).isoformat()))
    return None


def _finish(data_dir: Path, fingerprint: str, status: str, message_id: str | None = None):
    with _journal(data_dir) as db:
        db.execute("UPDATE sends SET status=?, message_id=?, updated_at=? WHERE fingerprint=?",
                   (status, message_id, datetime.now(UTC).isoformat(), fingerprint))


def _read_response(response, limit=64_000):
    raw = bytearray()
    for chunk in response.iter_content(chunk_size=8192):
        raw.extend(chunk)
        if len(raw) > limit:
            raise ValueError("oversized response")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise TypeError("invalid response")
    return result


class GmailSendClient:
    """An isolated sending client; the injected session makes tests fully offline."""

    def __init__(self, session):
        self._session = session

    def close(self):
        self._session.close()

    def account(self) -> str:
        response = None
        try:
            with _quiet_google():
                response = self._session.get(USERINFO_URL, timeout=30, max_allowed_time=30,
                                             allow_redirects=False, stream=True)
            if response.status_code != 200:
                raise ValueError
            info = _read_response(response)
            if info.get("email_verified") is not True or not info.get("sub"):
                raise ValueError
            return _single_address(info.get("email"))
        except Exception:  # noqa: BLE001 - Private HTTP errors stay out of UI/logs.
            raise GmailSendAuthError("Could not verify the Gmail sending account. Reconnect sending.") from None
        finally:
            if response is not None:
                response.close()

    def _send_once(self, preview: ReplyPreview) -> str:
        message = EmailMessage(policy=SMTP)
        message["From"] = preview.sender
        message["To"] = preview.recipient
        message["Subject"] = preview.subject
        message["Date"] = format_datetime(datetime.now(UTC))
        message["In-Reply-To"] = preview.rfc_message_id
        message["References"] = " ".join(preview.references)
        message["Message-ID"] = "<studentflow-" + preview.fingerprint + "@" + preview.sender.split("@")[1] + ">"
        message.set_content(preview.body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        response = None
        try:
            with _quiet_google():
                response = self._session.post(
                    gmail.API_BASE + "/messages/send",
                    json={"raw": raw, "threadId": preview.thread_id},
                    timeout=30, max_allowed_time=30, allow_redirects=False, stream=True,
                )
            if response.status_code in {400, 401, 403, 404, 413, 429}:
                error = GmailSendError("Gmail rejected this send. Check the sending permission and retry only after reviewing.")
                error.request_rejected = True
                raise error
            if response.status_code != 200:
                raise GmailSendUncertain("Gmail did not confirm delivery. Check Gmail Sent before taking any further action.")
            result = _read_response(response)
            message_id = result.get("id")
            if not isinstance(message_id, str) or not _ID.fullmatch(message_id):
                raise ValueError
            if result.get("threadId", preview.thread_id) != preview.thread_id:
                raise ValueError
            return message_id
        except GmailSendError:
            raise
        except Exception:  # noqa: BLE001 - Even timeouts can follow a successful send.
            raise GmailSendUncertain("Delivery could not be confirmed. Check Gmail Sent; this reply will not be sent again automatically.") from None
        finally:
            if response is not None:
                response.close()


def send_reviewed_reply(send_client: GmailSendClient, read_client, preview: ReplyPreview,
                        *, data_dir: Path, confirmed: bool = False) -> dict:
    """Send the exact human-reviewed payload once, after checking account/source again."""
    if confirmed is not True or not isinstance(preview, ReplyPreview):
        raise GmailSendError("Review the recipient and message and confirm before sending.")
    refreshed = prepare_reply(
        read_client, source_account=preview.sender, source_message_id=preview.source_message_id,
        recipient=preview.recipient, subject=preview.subject, body=preview.body,
    )
    if refreshed != preview:
        raise GmailSendError("The original email or reply changed. Review the current reply before sending.")
    if send_client.account() != preview.sender:
        raise GmailSendError("Connect Gmail sending with the same account as the original inbox email.")
    prior = _reserve(data_dir, preview.fingerprint)
    if prior:
        return prior
    try:
        message_id = send_client._send_once(preview)
    except GmailSendError as error:
        status = "rejected" if getattr(error, "request_rejected", False) else "uncertain"
        _finish(data_dir, preview.fingerprint, status)
        raise
    except Exception:  # noqa: BLE001 - Injectable clients must also obey no-retry semantics.
        _finish(data_dir, preview.fingerprint, "uncertain")
        raise GmailSendUncertain("Delivery could not be confirmed. Check Gmail Sent; this reply is blocked from being sent again.") from None
    try:
        _finish(data_dir, preview.fingerprint, "sent", message_id)
    except GmailSendError:
        raise GmailSendUncertain("Gmail accepted this reply, but the local record could not be saved. Check Gmail Sent; do not resend.") from None
    return {"status": "sent", "id": message_id}
