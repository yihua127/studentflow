"""Google read services and explicitly requested, narrowly scoped desktop OAuth.

Calendar writes live in calendar_sync and use a separate user-authorized token.
The default connection remains read-only. Private responses are never logged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCOPE_PREFIX = "https://www.googleapis.com/auth/"
SCOPES = [SCOPE_PREFIX + name for name in (
    "gmail.readonly", "chat.spaces.readonly", "chat.messages.readonly",
    "calendar.calendarlist.readonly", "calendar.events.readonly",
)]
READONLY_SCOPES = frozenset([*SCOPES, SCOPE_PREFIX + "meetings.space.readonly"])
CALENDAR_SYNC_SCOPES = [SCOPE_PREFIX + name for name in (
    "gmail.readonly", "calendar.calendarlist.readonly", "calendar.app.created",
)]
SUPPORTED_REQUEST_SCOPES = READONLY_SCOPES | {SCOPE_PREFIX + "calendar.app.created"}
APPROVED_SCOPES = SUPPORTED_REQUEST_SCOPES | {SCOPE_PREFIX + "gmail.send"}
TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URIS = {
    "https://accounts.google.com/o/oauth2/auth",
    "https://accounts.google.com/o/oauth2/v2/auth",
}
GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
CHAT_BASE = "https://chat.googleapis.com/v1"
CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"
MAX_PAGES = 20
PAGE_SIZE = 100
MAX_JSON_BYTES = 2 * 1024 * 1024
REQUEST_SECONDS = 30
LIST_SECONDS = 120
MAX_WINDOW_DAYS = 93
_SPACE = re.compile(r"spaces/[A-Za-z0-9_-]{1,256}\Z")
_CHAT_ID = re.compile(r"[A-Za-z0-9_.~-]{1,512}\Z")
_CALENDAR_ID = re.compile(r"[A-Za-z0-9_.@+#=\-]{1,1024}\Z")
_EVENT_ID = re.compile(r"[A-Za-z0-9_-]{1,1024}\Z")
_OAUTH_LOCK = threading.RLock()
_TOKEN_KEYS = {
    "token", "refresh_token", "client_id", "client_secret", "token_uri", "scopes", "expiry",
}


class GoogleWorkspaceError(Exception):
    """A safe, actionable error without credentials or private response content."""


class GoogleWorkspaceAuthError(GoogleWorkspaceError):
    """Authorization needs an explicit reconnect, not an automatic browser popup."""


def _scope_set(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(value.split())
    if isinstance(value, (list, tuple, set, frozenset)) and all(
        isinstance(item, str) for item in value
    ):
        return set(value)
    return set()


def _required(scopes: Sequence[str] | None) -> set[str]:
    result = set(SCOPES) if scopes is None else _scope_set(scopes)
    if not result or not result <= SUPPORTED_REQUEST_SCOPES:
        raise GoogleWorkspaceError(
            "Choose supported read-only permissions or the StudentFlow calendar permission."
        )
    return result


def _check_grants(granted: set[str], required: set[str]) -> None:
    if not granted or not required <= granted or not granted <= APPROVED_SCOPES:
        raise GoogleWorkspaceAuthError(
            "The saved Google permissions do not match this connection. Reconnect Google."
        )


def _load_json(path: Path, description: str) -> dict:
    try:
        with path.open("rb") as source:
            raw = source.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise ValueError("oversized file")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise TypeError("invalid object")
        return data
    except (OSError, ValueError, TypeError, RecursionError):
        raise GoogleWorkspaceError(f"Could not read {description}. Check the local file.") from None


def _client_config(path: Path) -> dict:
    client = _load_json(path, "Google connection settings").get("installed")
    if not isinstance(client, dict):
        raise GoogleWorkspaceError("Use a Google OAuth Desktop app configuration.")
    if client.get("auth_uri") not in AUTH_URIS or client.get("token_uri") != TOKEN_URI:
        raise GoogleWorkspaceError("The Google authorization addresses are invalid.")
    if not all(isinstance(client.get(key), str) and client[key] for key in (
        "client_id", "client_secret",
    )):
        raise GoogleWorkspaceError("The Google connection settings are incomplete.")
    return {"installed": {key: client[key] for key in (
        "client_id", "client_secret", "auth_uri", "token_uri",
    )}}


def _check_token(info: dict, client: dict, required: set[str]) -> set[str]:
    if any(info.get(key) != client[key] for key in (
        "client_id", "client_secret", "token_uri",
    )):
        raise GoogleWorkspaceAuthError(
            "The saved Google connection belongs to a different app. Reconnect Google."
        )
    granted = _scope_set(info.get("scopes"))
    _check_grants(granted, required)
    return granted


def _write_token(path: Path, info: dict) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=".google-workspace-", delete=False,
        ) as target:
            temporary = Path(target.name)
            json.dump({key: value for key, value in info.items() if key in _TOKEN_KEYS}, target)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except (OSError, ValueError, TypeError):
        raise GoogleWorkspaceError(
            "The Google connection could not be saved. Check local folder permissions."
        ) from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _save_credentials(path: Path, credentials: Any, granted: set[str], client: dict) -> None:
    try:
        info = json.loads(credentials.to_json())
        info["scopes"] = sorted(granted)
        _check_token(info, client, granted)
    except (ValueError, TypeError, AttributeError):
        raise GoogleWorkspaceAuthError("Google returned invalid authorization. Reconnect Google.") from None
    _write_token(path, info)


@contextmanager
def _quiet_google(*, allow_partial_scopes: bool = False) -> Iterator[None]:
    """Keep SDK debug logs/callback URLs private and restore process settings."""
    with _OAUTH_LOCK:
        previous_level = logging.root.manager.disable
        previous_scope_setting = os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE")
        logging.disable(logging.CRITICAL)
        if allow_partial_scopes:
            os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
        try:
            yield
        finally:
            logging.disable(previous_level)
            if allow_partial_scopes:
                if previous_scope_setting is None:
                    os.environ.pop("OAUTHLIB_RELAX_TOKEN_SCOPE", None)
                else:
                    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = previous_scope_setting


def _requires_sign_in(error: Exception) -> bool:
    from google.auth.exceptions import RefreshError

    if not isinstance(error, RefreshError) or error.retryable:
        return False
    rejected = {"invalid_grant", "invalid_client", "unauthorized_client", "access_denied"}
    return any(isinstance(item, dict) and isinstance(item.get("error"), str)
               and item["error"] in rejected for item in error.args)


def import_workspace_token(
    client_path: Path, source_token_path: Path, target_token_path: Path,
    *, required_scopes: Sequence[str] | None = None,
) -> frozenset[str]:
    """Explicitly copy approved local consent into a NEW separate token file.

    No network, refresh, sign-in or scope expansion occurs. The UI must call this
    only after the user chooses to connect using their earlier permission check.
    The source and an existing destination are never modified.
    """
    source, target = Path(source_token_path), Path(target_token_path)
    if target.exists() or source.resolve() == target.resolve():
        raise GoogleWorkspaceError("A Google connection already exists at the destination.")
    required = _required(required_scopes)
    client = _client_config(Path(client_path))["installed"]
    info = _load_json(source, "previous Google permission check")
    granted = _check_token(info, client, required)
    try:
        from google.oauth2.credentials import Credentials

        Credentials.from_authorized_user_info(
            {key: value for key, value in info.items() if key in _TOKEN_KEYS},
            scopes=sorted(granted),
        )
    except ImportError:
        raise GoogleWorkspaceError("Install the Google connection dependencies first.") from None
    except (ValueError, TypeError):
        raise GoogleWorkspaceAuthError("The previous authorization is invalid. Reconnect Google.") from None
    info["scopes"] = sorted(granted)
    _write_token(target, info)
    return frozenset(granted)


def connect_workspace(
    client_path: Path, token_path: Path, interactive: bool = False,
    *, required_scopes: Sequence[str] | None = None,
) -> GoogleWorkspaceClient:
    """Restore consent or, only with interactive=True, explicitly sign in again.

    The default five read-only scopes cover Chat, Calendar and Gmail profile.
    A caller may request a smaller subset or explicit app-created Calendar access.
    Each operation checks its
    own scope; normalised Chat messages also need Gmail profile for account IDs.
    """
    required = _required(required_scopes)
    config = _client_config(Path(client_path))
    token_path = Path(token_path)
    if token_path.resolve() == Path(client_path).resolve():
        raise GoogleWorkspaceError("Use a separate file for Google authorization.")
    try:
        from google.auth.transport.requests import AuthorizedSession, Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise GoogleWorkspaceError("Install the Google connection dependencies first.") from None

    if interactive:
        try:
            with _quiet_google(allow_partial_scopes=True):
                flow = InstalledAppFlow.from_client_config(config, sorted(required))
                original_request = flow.oauth2session.request

                def limited_oauth_request(*args: Any, **kwargs: Any) -> Any:
                    kwargs.update(timeout=REQUEST_SECONDS, allow_redirects=False)
                    return original_request(*args, **kwargs)

                flow.oauth2session.request = limited_oauth_request
                credentials = flow.run_local_server(
                    host="127.0.0.1", port=0, timeout_seconds=180,
                    open_browser=True, authorization_prompt_message=None,
                    success_message="Google authorization received. Return to StudentFlow.",
                    prompt="select_account consent", access_type="offline",
                    include_granted_scopes="false",
                )
                granted = _scope_set(flow.oauth2session.token.get("scope"))
                if not granted:
                    granted = _scope_set(credentials.granted_scopes)
        except Exception:  # noqa: BLE001 - SDK exceptions can contain tokens/callback URLs.
            raise GoogleWorkspaceAuthError(
                "Google sign-in did not finish. Check your connection and test-user access."
            ) from None
        _check_grants(granted, required)
        if not credentials.valid:
            raise GoogleWorkspaceAuthError("Google authorization is invalid. Reconnect Google.")
        _save_credentials(token_path, credentials, granted, config["installed"])
    else:
        if not token_path.is_file():
            raise GoogleWorkspaceAuthError("Google is not connected. Click Connect Google to sign in.")
        try:
            info = _load_json(token_path, "saved Google connection")
        except GoogleWorkspaceError:
            raise GoogleWorkspaceAuthError("The saved connection is invalid. Reconnect Google.") from None
        granted = _check_token(info, config["installed"], required)
        try:
            credentials = Credentials.from_authorized_user_info(
                {key: value for key, value in info.items() if key in _TOKEN_KEYS},
                scopes=sorted(granted),
            )
        except (ValueError, TypeError):
            raise GoogleWorkspaceAuthError("The saved connection is invalid. Reconnect Google.") from None
        if not credentials.valid:
            if not credentials.refresh_token:
                raise GoogleWorkspaceAuthError("Google authorization has expired. Reconnect Google.")
            try:
                transport = Request()

                def limited_request(*args: Any, **kwargs: Any) -> Any:
                    kwargs.update(timeout=REQUEST_SECONDS, allow_redirects=False)
                    return transport(*args, **kwargs)

                with _quiet_google():
                    credentials.refresh(limited_request)
            except Exception as error:  # noqa: BLE001 - No OAuth response details in UI.
                if _requires_sign_in(error):
                    raise GoogleWorkspaceAuthError(
                        "Google authorization was rejected. Reconnect Google."
                    ) from None
                raise GoogleWorkspaceError(
                    "Could not refresh Google now. Your saved connection is retained. Try again."
                ) from None
            if credentials.granted_scopes is not None:
                granted = _scope_set(credentials.granted_scopes)
            _check_grants(granted, required)
            if not credentials.valid:
                raise GoogleWorkspaceAuthError("Google authorization has expired. Reconnect Google.")
            _save_credentials(token_path, credentials, granted, config["installed"])

    session = AuthorizedSession(
        credentials, max_refresh_attempts=0, refresh_timeout=REQUEST_SECONDS,
    )
    return GoogleWorkspaceClient(session, granted_scopes=granted)


def _identifier(value: Any, pattern: re.Pattern, label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value) or value in {".", ".."}:
        raise GoogleWorkspaceError(f"Choose a valid {label} from the Google list.")
    return value


def _text(value: Any, default: str = "", limit: int = 32_000) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or len(value) > limit:
        raise GoogleWorkspaceError("Google returned unsupported content. Nothing was imported.")
    return value


def _zone(value: Any) -> ZoneInfo:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise GoogleWorkspaceError("Google Calendar did not provide a valid time zone.")
    try:
        return ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        raise GoogleWorkspaceError("Google Calendar returned an unsupported time zone.") from None


def _aware_datetime(value: Any, zone_name: str | None = None) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            if not zone_name:
                raise ValueError("timezone required")
            zone = _zone(zone_name)
            candidates = [parsed.replace(tzinfo=zone, fold=fold) for fold in (0, 1)]
            valid = [candidate for candidate in candidates if candidate.astimezone(
                UTC
            ).astimezone(zone).replace(tzinfo=None) == parsed]
            if not valid or len({item.utcoffset() for item in valid}) != 1:
                raise ValueError("ambiguous or nonexistent local time")
            parsed = valid[0]
        return parsed
    except (ValueError, TypeError, OverflowError):
        raise GoogleWorkspaceError(
            "Google returned an invalid or ambiguous time. Nothing was imported."
        ) from None


def _event_time(value: Any, calendar_zone: str | None) -> tuple[datetime, bool]:
    if not isinstance(value, dict):
        raise GoogleWorkspaceError("A calendar event has no usable time. Nothing was imported.")
    if value.get("dateTime") is not None:
        return _aware_datetime(value["dateTime"], value.get("timeZone") or calendar_zone), False
    try:
        day = date.fromisoformat(value["date"])
        zone = _zone(calendar_zone)
        return datetime.combine(day, time.min, zone), True
    except (ValueError, TypeError, KeyError):
        raise GoogleWorkspaceError("A calendar event has no usable date. Nothing was imported.") from None


class GoogleWorkspaceClient:
    """Injectable read-only client. Returned private content is never logged here."""

    def __init__(self, session: Any, *, granted_scopes: Sequence[str] | set[str] | None = None):
        self._session = session
        self.granted_scopes = frozenset(SCOPES if granted_scopes is None else granted_scopes)
        self._account: str | None = None
        self._space_titles: dict[str, str] = {}

    def close(self) -> None:
        self._session.close()

    def _require(self, short_scope: str) -> None:
        if SCOPE_PREFIX + short_scope not in self.granted_scopes:
            raise GoogleWorkspaceAuthError(
                "This Google feature has not been authorized. Reconnect with its read-only permission."
            )

    def _get_json(self, url: str, params: dict, *, label: str, limit=MAX_JSON_BYTES) -> dict:
        if (not isinstance(url, str) or "?" in url or "#" in url
                or not any(url.startswith(base + "/") for base in (
                    GMAIL_BASE, CHAT_BASE, CALENDAR_BASE,
                ))):
            raise GoogleWorkspaceError("Only the supported Google read endpoints are allowed.")
        response = None
        started = monotonic()
        try:
            with _quiet_google():
                response = self._session.get(
                    url, params=params, timeout=REQUEST_SECONDS,
                    max_allowed_time=REQUEST_SECONDS, allow_redirects=False, stream=True,
                )
            if response.status_code == 401:
                raise GoogleWorkspaceAuthError("Google authorization has expired. Reconnect Google.")
            if response.status_code == 403:
                raise GoogleWorkspaceError(
                    f"Google denied access to {label}. Check API enablement and account permissions."
                )
            if response.status_code == 400:
                raise GoogleWorkspaceError(
                    f"Google could not read {label}. Check that the service is enabled for this account."
                )
            if response.status_code == 429:
                raise GoogleWorkspaceError("Google has temporarily limited requests. Try again later.")
            if response.status_code != 200:
                raise GoogleWorkspaceError(f"Could not read {label} from Google. Try again.")
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > limit:
                raise GoogleWorkspaceError("The Google response is too large. Nothing was imported.")
            raw = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if len(raw) + len(chunk) > limit:
                    raise GoogleWorkspaceError("The Google response is too large. Nothing was imported.")
                if monotonic() - started > REQUEST_SECONDS:
                    raise GoogleWorkspaceError("Google took too long to respond. Nothing was imported.")
                raw.extend(chunk)
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise TypeError("unexpected response")
            return payload
        except GoogleWorkspaceError:
            raise
        except Exception as error:  # noqa: BLE001 - Suppress headers, URLs, tokens and bodies.
            if _requires_sign_in(error):
                raise GoogleWorkspaceAuthError("Google authorization was rejected. Reconnect Google.") from None
            raise GoogleWorkspaceError(f"Could not read {label} from Google. Try again.") from None
        finally:
            if response is not None:
                response.close()

    def _pages(self, url: str, params: dict, key: str, label: str) -> Iterator[dict]:
        """Fail the whole list on truncation, cursor cycles, or any failed page."""
        seen_tokens: set[str] = set()
        page_token = None
        started = monotonic()
        for _ in range(MAX_PAGES):
            if monotonic() - started > LIST_SECONDS:
                raise GoogleWorkspaceError(f"Reading {label} took too long. Nothing was imported.")
            request_params = dict(params)
            if page_token is not None:
                request_params["pageToken"] = page_token
            page = self._get_json(url, request_params, label=label)
            rows = page.get(key, [])
            if (not isinstance(rows, list) or len(rows) > PAGE_SIZE
                    or not all(isinstance(row, dict) for row in rows)):
                raise GoogleWorkspaceError(f"Google returned an invalid {label} list.")
            page_token = page.get("nextPageToken")
            if page_token not in (None, ""):
                if (not isinstance(page_token, str) or not page_token.strip()
                        or len(page_token) > 4096
                        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in page_token)
                        or page_token in seen_tokens):
                    raise GoogleWorkspaceError(f"Google returned an invalid {label} cursor.")
                seen_tokens.add(page_token)
            yield page
            if page_token in (None, ""):
                return
        raise GoogleWorkspaceError(f"Too many {label} to load safely. Nothing was imported.")

    def profile(self) -> dict:
        self._require("gmail.readonly")
        if self._account is None:
            data = self._get_json(
                GMAIL_BASE + "/profile", {"fields": "emailAddress"}, label="account", limit=64_000,
            )
            address = data.get("emailAddress")
            if (not isinstance(address, str) or len(address) > 320
                    or not re.fullmatch(r"[^@\s]+@[^@\s]+", address)):
                raise GoogleWorkspaceAuthError("Could not verify the Google account. Reconnect Google.")
            self._account = address.lower()
        return {"email_address": self._account}

    def list_spaces(self) -> list[dict]:
        self._require("chat.spaces.readonly")
        result = {}
        for page in self._pages(CHAT_BASE + "/spaces", {
            "pageSize": PAGE_SIZE, "filter": 'spaceType = "SPACE"',
            "fields": "spaces(name,displayName,spaceType),nextPageToken",
        }, "spaces", "Chat spaces"):
            for item in page.get("spaces", []):
                name = _identifier(item.get("name"), _SPACE, "Chat space")
                title = _text(item.get("displayName"), name, limit=4096)
                result[name] = {"name": name, "display_name": title,
                                "space_type": _text(item.get("spaceType"), "SPACE", limit=32)}
        self._space_titles = {name: item["display_name"] for name, item in result.items()}
        return list(result.values())

    def list_messages(self, space_name: str, *, thread_id: str | None = None) -> list[dict]:
        self._require("chat.messages.readonly")
        space_name = _identifier(space_name, _SPACE, "Chat space")
        if thread_id is not None:
            prefix = space_name + "/threads/"
            if not isinstance(thread_id, str) or not thread_id.startswith(prefix):
                raise GoogleWorkspaceError("Choose a thread from the selected Chat space.")
            _identifier(thread_id[len(prefix):], _CHAT_ID, "Chat thread")
        account = self.profile()["email_address"]
        params = {
            "pageSize": PAGE_SIZE, "orderBy": "createTime asc", "showDeleted": "false",
            "fields": "messages(name,text,createTime,thread(name),sender(name,displayName,type),"
                      "threadReply,attachment(name),cards,cardsV2),nextPageToken",
        }
        if thread_id is not None:
            params["filter"] = f"thread.name = {thread_id}"
        result = {}
        for page in self._pages(
            CHAT_BASE + f"/{space_name}/messages", params, "messages", "Chat messages",
        ):
            for item in page.get("messages", []):
                name = item.get("name")
                prefix = space_name + "/messages/"
                if not isinstance(name, str) or not name.startswith(prefix):
                    raise GoogleWorkspaceError("Google returned a message from an unexpected space.")
                _identifier(name[len(prefix):], _CHAT_ID, "Chat message")
                thread = item.get("thread", {})
                if not isinstance(thread, dict):
                    raise GoogleWorkspaceError("Google returned invalid thread details.")
                thread_name = thread.get("name", "")
                if thread_name:
                    thread_prefix = space_name + "/threads/"
                    if not isinstance(thread_name, str) or not thread_name.startswith(thread_prefix):
                        raise GoogleWorkspaceError("Google returned an unexpected Chat thread.")
                    _identifier(thread_name[len(thread_prefix):], _CHAT_ID, "Chat thread")
                if thread_id is not None and thread_name != thread_id:
                    raise GoogleWorkspaceError("Google returned a different Chat thread.")
                sender = item.get("sender", {})
                if not isinstance(sender, dict):
                    raise GoogleWorkspaceError("Google returned invalid Chat sender details.")
                text = _text(item.get("text"))
                result[name] = {
                    "name": name, "space_name": space_name, "thread_id": thread_name,
                    "source_id": f"google_chat:{account}:{name}",
                    "provider": "google_chat", "account": account,
                    "title": self._space_titles.get(space_name, space_name), "text": text,
                    "sent_at": _aware_datetime(item.get("createTime")).isoformat(),
                    "sender_id": _text(sender.get("name"), limit=512),
                    "sender_name": _text(sender.get("displayName"), limit=4096),
                    "sender_type": _text(sender.get("type"), limit=32),
                    "thread_reply": item.get("threadReply") is True,
                    "text_available": bool(text.strip()),
                    "has_attachments": bool(item.get("attachment")),
                    "has_cards": bool(item.get("cards") or item.get("cardsV2")),
                }
        return list(result.values())

    def list_calendars(self) -> list[dict]:
        self._require("calendar.calendarlist.readonly")
        result = {}
        for page in self._pages(CALENDAR_BASE + "/users/me/calendarList", {
            "maxResults": PAGE_SIZE,
            "fields": "items(id,summary,summaryOverride,timeZone,primary,accessRole),nextPageToken",
        }, "items", "calendars"):
            for item in page.get("items", []):
                calendar_id = _identifier(item.get("id"), _CALENDAR_ID, "calendar")
                zone_name = _text(item.get("timeZone"), limit=128)
                if zone_name:
                    _zone(zone_name)
                result[calendar_id] = {
                    "id": calendar_id,
                    "title": _text(item.get("summaryOverride") or item.get("summary"),
                                   calendar_id, limit=4096),
                    "time_zone": zone_name, "primary": item.get("primary") is True,
                    "access_role": _text(item.get("accessRole"), limit=64),
                }
        return list(result.values())

    def list_events(
        self, calendar_id: str, time_min: datetime | str, time_max: datetime | str,
    ) -> list[dict]:
        """Return busy instances overlapping [time_min, time_max), without writes.

        All-day end dates are exclusive and use the calendar's time zone. Free,
        cancelled and personally declined events do not block study time. Broken
        dates or failed pages raise instead of being silently treated as free.
        """
        self._require("calendar.events.readonly")
        calendar_id = _identifier(calendar_id, _CALENDAR_ID, "calendar")
        lower, upper = _aware_datetime(time_min), _aware_datetime(time_max)
        window = upper.astimezone(UTC) - lower.astimezone(UTC)
        if window <= timedelta(0) or window > timedelta(days=MAX_WINDOW_DAYS):
            raise GoogleWorkspaceError(
                f"Choose a positive calendar interval no longer than {MAX_WINDOW_DAYS} days."
            )
        instances = {}
        for page in self._pages(
            CALENDAR_BASE + f"/calendars/{quote(calendar_id, safe='')}/events", {
                "maxResults": PAGE_SIZE, "singleEvents": "true", "showDeleted": "false",
                "orderBy": "startTime", "timeMin": lower.isoformat(), "timeMax": upper.isoformat(),
                "fields": "timeZone,nextPageToken,items(id,summary,start,end,status,transparency,"
                          "updated,recurringEventId,attendees(self,responseStatus),eventType)",
            }, "items", "calendar events",
        ):
            zone_name = page.get("timeZone")
            for item in page.get("items", []):
                event_id = _identifier(item.get("id"), _EVENT_ID, "calendar event")
                previous = instances.get(event_id)
                # Repeated pages must not double-count an occurrence. Different
                # recurring instance IDs are intentionally retained separately.
                if (previous and previous[0].get("updated") and item.get("updated")
                        and _aware_datetime(item["updated"]) < _aware_datetime(previous[0]["updated"])):
                    continue
                instances[event_id] = (item, zone_name)
        result = []
        for event_id, (item, zone_name) in instances.items():
            attendees = item.get("attendees", [])
            if not isinstance(attendees, list) or not all(isinstance(a, dict) for a in attendees):
                raise GoogleWorkspaceError("Google returned invalid calendar attendance details.")
            if (item.get("status") == "cancelled" or item.get("transparency") == "transparent"
                    or any(a.get("self") is True and a.get("responseStatus") == "declined"
                           for a in attendees)):
                continue
            start, all_day = _event_time(item.get("start"), zone_name)
            end, end_all_day = _event_time(item.get("end"), zone_name)
            if all_day != end_all_day or end <= start:
                raise GoogleWorkspaceError("A calendar event has invalid start/end times.")
            if end <= lower or start >= upper:
                continue
            result.append({
                "id": event_id, "calendar_id": calendar_id,
                "source_id": f"google_calendar:{calendar_id}:{event_id}",
                "provider": "google_calendar", "title": _text(item.get("summary"), "Busy", 4096),
                "start": start.isoformat(), "end": end.isoformat(), "is_all_day": all_day,
                "time_zone": zone_name or "", "recurring_event_id": item.get("recurringEventId", ""),
            })
        return sorted(result, key=lambda event: _aware_datetime(event["start"]))
