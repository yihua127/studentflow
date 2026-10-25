"""Previewed, account-bound synchronization to an app-owned secondary calendar.

No operation runs on import. Caller must obtain human confirmation of a preview
before apply_sync. Local SQLite journaling makes event retries and ambiguous
calendar creation safe across restarts; it contains no credentials.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from studentflow.google_workspace import (
    CALENDAR_BASE,
    MAX_JSON_BYTES,
    PAGE_SIZE,
    REQUEST_SECONDS,
    GoogleWorkspaceAuthError,
    GoogleWorkspaceError,
    _quiet_google,
)

ZONE_NAME = "Asia/Singapore"
_HEX = re.compile(r"[a-f0-9]{64}\Z")


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _dt(value) -> datetime:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(UTC)
    except (ValueError, TypeError):
        raise GoogleWorkspaceError("A calendar item needs a valid date, time and time zone.") from None


def _instant(value) -> str:
    return _dt(value).isoformat()


def _event_id(owner: str, key: str, generation: int) -> str:
    # Hex is a subset of the Calendar API's base32hex event ID alphabet.
    return "sf" + _digest([owner, key, generation])


def _marker(owner: str) -> str:
    return "StudentFlow managed calendar. Owner: " + owner


@contextmanager
def _state(data_dir: Path):
    path = Path(data_dir) / "calendar_sync.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=1) as db:
        db.execute("CREATE TABLE IF NOT EXISTS installation (id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE IF NOT EXISTS accounts (account TEXT PRIMARY KEY, "
                   "calendar_id TEXT, pending INTEGER NOT NULL DEFAULT 0)")
        db.execute("CREATE TABLE IF NOT EXISTS generations (account TEXT, item_key TEXT, "
                   "generation INTEGER NOT NULL, PRIMARY KEY(account, item_key))")
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT id FROM installation LIMIT 1").fetchone()
        if row is None:
            installation = uuid4().hex
            db.execute("INSERT INTO installation VALUES (?)", (installation,))
        else:
            installation = row[0]
        # Keep the transaction only for short local operations. Network writes
        # use separate durable journal transactions before the HTTP operation.
        yield db, installation


def _identity(data_dir: Path, account: str) -> str:
    with _state(data_dir) as (db, installation):
        db.execute("INSERT OR IGNORE INTO accounts(account) VALUES (?)", (account,))
    return _digest([installation, account])


def _generation(data_dir: Path, account: str, key: str, *, advance_from=None) -> int:
    with _state(data_dir) as (db, _):
        row = db.execute("SELECT generation FROM generations WHERE account=? AND item_key=?",
                         (account, key)).fetchone()
        generation = row[0] if row else 0
        if advance_from is not None and generation <= advance_from:
            generation = advance_from + 1
        db.execute("INSERT INTO generations VALUES (?,?,?) ON CONFLICT(account,item_key) "
                   "DO UPDATE SET generation=excluded.generation", (account, key, generation))
    return generation


class CalendarProvider:
    """Small HTTP adapter; every write is limited to a verified managed calendar."""

    def __init__(self, client):
        self.client = client
        self.before_write: Callable[[], None] = lambda: None

    def account(self) -> str:
        return self.client.profile()["email_address"].lower()

    def require_write_scope(self):
        self.client._require("calendar.app.created")

    def calendars(self) -> list[dict]:
        self.client._require("calendar.calendarlist.readonly")
        return [item for page in self.client._pages(
            CALENDAR_BASE + "/users/me/calendarList",
            {"maxResults": PAGE_SIZE, "showHidden": "true", "minAccessRole": "owner",
             "fields": "items(id,summary,description,primary,accessRole),nextPageToken"},
            "items", "StudentFlow calendars",
        ) for item in page.get("items", [])]

    def _request(self, method: str, path: str, *, body=None, etag=None,
                 allow_missing=False, allow_conflict=False):
        self.require_write_scope()
        response = None
        try:
            if method != "GET":
                # Calendar ownership verification can itself take a network
                # round trip. Recheck facts/time at the actual mutation boundary.
                try:
                    self.before_write()
                except GoogleWorkspaceError as error:
                    error.request_rejected = True
                    raise
            with _quiet_google():
                response = self.client._session.request(
                    method, CALENDAR_BASE + path, json=body,
                    params={"sendUpdates": "none"}
                    if "/events" in path and method != "GET" else {},
                    headers={"If-Match": etag} if etag else {},
                    timeout=REQUEST_SECONDS, max_allowed_time=REQUEST_SECONDS,
                    allow_redirects=False, stream=True,
                )
            status = response.status_code
            if status in (401, 403):
                error = GoogleWorkspaceAuthError(
                    "Calendar access was denied. Reconnect Calendar sync and grant its permission."
                )
                error.request_rejected = True
                raise error
            if allow_missing and status in (404, 410):
                return None
            if allow_conflict and status == 409:
                return {"conflict": True}
            if status == 412:
                raise GoogleWorkspaceError("A calendar event changed. Refresh the sync preview.")
            if status not in (200, 201, 204):
                error = GoogleWorkspaceError(
                    "Calendar sync did not finish. Refresh the preview to retry."
                )
                error.request_rejected = 400 <= status < 500
                raise error
            if status == 204:
                return {}
            raw = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                raw.extend(chunk)
                if len(raw) > MAX_JSON_BYTES:
                    raise ValueError
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise TypeError
            return result
        except GoogleWorkspaceError:
            raise
        except Exception:  # noqa: BLE001 - SDK failures may contain credentials and private URLs.
            raise GoogleWorkspaceError(
                "The Calendar response was interrupted. Refresh the preview before retrying."
            ) from None
        finally:
            if response is not None:
                response.close()

    def create_calendar(self, owner: str) -> dict:
        if not _HEX.fullmatch(owner):
            raise GoogleWorkspaceError("Invalid calendar owner.")
        return self._request("POST", "/calendars", body={
            "summary": "StudentFlow", "description": _marker(owner), "timeZone": ZONE_NAME,
        })

    def _calendar_path(self, calendar_id: str, owner: str) -> str:
        matches = [c for c in self.calendars() if c.get("id") == calendar_id
                   and c.get("description") == _marker(owner)
                   and c.get("accessRole") == "owner" and not c.get("primary")]
        if len(matches) != 1:
            raise GoogleWorkspaceError("The StudentFlow calendar is unavailable. Refresh the preview.")
        return "/calendars/" + quote(calendar_id, safe="") + "/events"

    def events(self, calendar_id: str, owner: str) -> list[dict]:
        self.require_write_scope()
        path = self._calendar_path(calendar_id, owner)
        return [item for page in self.client._pages(
            CALENDAR_BASE + path,
            {"maxResults": PAGE_SIZE, "showDeleted": "false",
             "privateExtendedProperty": "sf_owner=" + owner},
            "items", "StudentFlow events",
        ) for item in page.get("items", [])]

    def get_event(self, calendar_id: str, owner: str, event_id: str):
        path = self._calendar_path(calendar_id, owner)
        return self._request("GET", path + "/" + quote(event_id, safe=""), allow_missing=True)

    def insert_event(self, calendar_id: str, owner: str, body: dict):
        _assert_owned(body, owner)
        path = self._calendar_path(calendar_id, owner)
        result = self._request("POST", path, body=body, allow_conflict=True)
        if result.get("conflict"):
            result = self.get_event(calendar_id, owner, body["id"])
            if not result or result.get("status") == "cancelled":
                raise GoogleWorkspaceError("This event was removed. Refresh the sync preview to retry.")
            _assert_owned(result, owner)
            if _content(result) != _content(body):
                raise GoogleWorkspaceError("This event changed. Refresh the sync preview.")
        return result

    def update_event(self, calendar_id: str, owner: str, previous: dict, body: dict):
        _assert_owned(previous, owner)
        _assert_owned(body, owner)
        if previous["id"] != body["id"]:
            raise GoogleWorkspaceError("Invalid calendar update.")
        path = self._calendar_path(calendar_id, owner)
        return self._request("PATCH", path + "/" + quote(body["id"], safe=""),
                             body=body, etag=previous.get("etag"))

    def delete_event(self, calendar_id: str, owner: str, previous: dict):
        _assert_owned(previous, owner)
        path = self._calendar_path(calendar_id, owner)
        return self._request("DELETE", path + "/" + quote(previous["id"], safe=""),
                             etag=previous.get("etag"), allow_missing=True)


def _owned(event: dict, owner: str) -> bool:
    private = event.get("extendedProperties", {}).get("private", {})
    key, generation = private.get("sf_key"), private.get("sf_generation")
    return (private.get("sf_owner") == owner and isinstance(key, str)
            and private.get("sf_kind") in {"study", "fixed"}
            and isinstance(generation, str) and generation.isdigit()
            and event.get("id") == _event_id(owner, key, int(generation)))


def _assert_owned(event: dict, owner: str):
    if (not _owned(event, owner) or event.get("attendees") or event.get("recurrence")
            or event.get("recurringEventId") or event.get("conferenceData")):
        raise GoogleWorkspaceError(
            "A calendar item is shared, recurring or no longer managed by StudentFlow. "
            "It was not changed. Review it in Google Calendar."
        )


def _content(event: dict) -> dict:
    return {"summary": event.get("summary", ""), "location": event.get("location", ""),
            "start": _instant(event["start"]["dateTime"]),
            "end": _instant(event["end"]["dateTime"]),
            "description": event.get("description", "")}


def _discover(provider, data_dir: Path, account: str, owner: str) -> str | None:
    matches = [item for item in provider.calendars()
               if item.get("description") == _marker(owner)
               and item.get("accessRole") == "owner" and not item.get("primary")]
    if len(matches) > 1:
        raise GoogleWorkspaceError("More than one managed calendar was found. Resolve it in Google Calendar.")
    with _state(data_dir) as (db, _):
        stored, pending = db.execute(
            "SELECT calendar_id,pending FROM accounts WHERE account=?", (account,),
        ).fetchone()
        if matches:
            found = matches[0]["id"]
            if stored and stored != found:
                raise GoogleWorkspaceError("The managed calendar changed. Reconnect Calendar sync.")
            db.execute("UPDATE accounts SET calendar_id=?,pending=0 WHERE account=?", (found, account))
            return found
        if stored:
            raise GoogleWorkspaceError("The saved StudentFlow calendar is unavailable. Restore it in Google Calendar.")
        if pending:
            raise GoogleWorkspaceError(
                "Calendar creation is awaiting confirmation. Wait briefly and preview again; "
                "StudentFlow will not create a second calendar."
            )
    return None


def _source_items(plan: dict | None, fixed_events: list[dict], account: str, now: datetime):
    items = []
    for block in (plan or {}).get("result", {}).get("blocks", []):
        start, end = _dt(block["start"]), _dt(block["end"])
        if end <= start:
            raise GoogleWorkspaceError("A saved study session has invalid times. Save a new plan.")
        if start < now:
            continue
        key = "study:" + str(block["task_id"]) + ":" + start.isoformat()
        items.append((key, "study", block["title"], start, end, ""))
    for event in fixed_events:
        if event.get("status") != "confirmed" or event.get("account", "").lower() != account:
            continue
        start = _dt(event.get("start_at", event.get("start")))
        end = _dt(event.get("end_at", event.get("end")))
        if end <= start:
            raise GoogleWorkspaceError("A fixed event has invalid times. Review it first.")
        if start < now:
            continue
        items.append(("fixed:" + str(event["id"]), "fixed", event["title"], start, end,
                      event.get("location") or ""))
    keys = [item[0] for item in items]
    if len(keys) != len(set(keys)):
        raise GoogleWorkspaceError("Duplicate saved calendar items were found. Review the plan.")
    return items


def prepare_sync(provider, data_dir: Path, plan: dict | None, fixed_events: list[dict], *,
                 fingerprint: str, plan_is_fresh: bool, now: datetime | None = None,
                 include_study_plan: bool = True) -> dict:
    """Read-only network preview. Local journal writes never create Google events."""
    if include_study_plan and plan and not plan_is_fresh:
        raise GoogleWorkspaceError("Tasks or planning settings changed. Generate and save a fresh plan first.")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise GoogleWorkspaceError("Refresh the saved plan before syncing.")
    now = _dt(now or datetime.now(UTC))
    provider.require_write_scope()
    account = provider.account().lower()
    owner = _identity(data_dir, account)
    calendar_id = _discover(provider, data_dir, account, owner)
    remote = provider.events(calendar_id, owner) if calendar_id else []
    managed_kinds = {"study", "fixed"} if include_study_plan else {"fixed"}
    owned = {item["id"]: item for item in remote if _owned(item, owner)
             and item["extendedProperties"]["private"]["sf_kind"] in managed_kinds}
    desired = {}
    for key, kind, title, start, end, location in _source_items(
        plan if include_study_plan else None, fixed_events, account, now,
    ):
        generation = _generation(data_dir, account, key)
        identifier = _event_id(owner, key, generation)
        existing = owned.get(identifier)
        if calendar_id and existing is None:
            # Look up deterministic IDs too: deleted tombstones may lose private
            # properties and therefore be absent from filtered list results.
            existing = provider.get_event(calendar_id, owner, identifier)
        if existing and (existing.get("status") == "cancelled"
                         or _dt(existing["start"]["dateTime"]) < now):
            generation = _generation(data_dir, account, key, advance_from=generation)
            identifier = _event_id(owner, key, generation)
        elif existing:
            _assert_owned(existing, owner)
            owned[identifier] = existing
        desired[identifier] = {
            "id": identifier, "summary": title, "location": location,
            "description": "Saved in StudentFlow. " + (
                "Study session." if kind == "study" else "Confirmed fixed event."
            ),
            "start": {"dateTime": start.isoformat(), "timeZone": ZONE_NAME},
            "end": {"dateTime": end.isoformat(), "timeZone": ZONE_NAME},
            "extendedProperties": {"private": {
                "sf_owner": owner, "sf_key": key, "sf_kind": kind,
                "sf_generation": str(generation),
            }},
            "visibility": "private", "transparency": "opaque",
        }
    operations = []
    for identifier, body in desired.items():
        previous = owned.get(identifier)
        if previous is None:
            operations.append({"action": "create", "event": body})
        elif _content(previous) != _content(body):
            _assert_owned(previous, owner)
            operations.append({"action": "update", "event": body, "previous": previous})
    for identifier, event in owned.items():
        if identifier not in desired and _dt(event["start"]["dateTime"]) >= now:
            _assert_owned(event, owner)
            operations.append({"action": "remove", "event": event, "previous": event})
    preview = {"account": account, "owner": owner, "calendar_id": calendar_id,
               "fingerprint": fingerprint, "operations": operations,
               "include_study_plan": include_study_plan,
               "excluded_fixed_count": sum(
                   event.get("status") == "confirmed"
                   and event.get("account", "").lower() != account for event in fixed_events
               ),
               "create_calendar": calendar_id is None and bool(desired),
               "prepared_at": now.isoformat(), "desired_count": len(desired)}
    preview["signature"] = _digest(preview)
    return preview


def apply_sync(provider, data_dir: Path, preview: dict, *,
               current_fingerprint: Callable[[], str], load_plan: Callable[[], dict | None],
               saved_plan_is_fresh: Callable[[dict], bool], now: datetime | None = None,
               clock: Callable[[], datetime] | None = None) -> dict:
    """Apply exactly a reviewed preview; caller's explicit button is authorization."""
    signature = preview.get("signature")
    if signature != _digest({k: v for k, v in preview.items() if k != "signature"}):
        raise GoogleWorkspaceError("The sync preview changed. Preview again.")
    clock = clock or ((lambda: now) if now is not None else (lambda: datetime.now(UTC)))
    now = _dt(clock())
    if (now - _dt(preview["prepared_at"])).total_seconds() > 900:
        raise GoogleWorkspaceError("The sync preview expired. Preview again.")

    def check_current():
        plan = load_plan()
        if current_fingerprint() != preview["fingerprint"] or (
            preview["include_study_plan"] and plan and not saved_plan_is_fresh(plan)
        ):
            raise GoogleWorkspaceError("The plan or events changed. Preview Calendar sync again.")

    def check_future(*events):
        check_current()
        current_time = _dt(clock())
        if any(_dt(event["start"]["dateTime"]) < current_time for event in events):
            raise GoogleWorkspaceError("A session has already started. Refresh the sync preview.")

    check_current()
    provider.require_write_scope()
    account = provider.account().lower()
    owner = _identity(data_dir, account)
    if account != preview["account"] or owner != preview["owner"]:
        raise GoogleWorkspaceError("The Google account changed. Preview again for this account.")
    calendar_id = _discover(provider, data_dir, account, owner)
    if preview["calendar_id"] and calendar_id != preview["calendar_id"]:
        raise GoogleWorkspaceError("The calendar changed. Preview again.")
    operations = preview["operations"]
    if any(_dt(op["event"]["start"]["dateTime"]) < now for op in operations):
        raise GoogleWorkspaceError("A session has already started. Refresh the sync preview.")
    if not calendar_id and preview["create_calendar"]:
        check_future(*(op["event"] for op in operations))
        # Journal BEFORE POST. An interrupted response cannot cause blind repeat
        # calendar creation on the next click or after restarting the app.
        with _state(data_dir) as (db, _):
            changed = db.execute("UPDATE accounts SET pending=1 WHERE account=? AND pending=0 "
                                 "AND calendar_id IS NULL", (account,)).rowcount
            if changed != 1:
                raise GoogleWorkspaceError("Calendar creation is already in progress. Preview again.")
        try:
            provider.before_write = lambda: check_future(*(op["event"] for op in operations))
            created = provider.create_calendar(owner)
        except GoogleWorkspaceError as error:
            if getattr(error, "request_rejected", False):
                # A definitive HTTP rejection is different from an unknown
                # timeout outcome: no calendar exists and retry is safe.
                with _state(data_dir) as (db, _):
                    db.execute("UPDATE accounts SET pending=0 WHERE account=?", (account,))
            raise
        if not created.get("id") or created.get("description") != _marker(owner):
            raise GoogleWorkspaceError("Calendar creation needs verification. Preview again.")
        calendar_id = created["id"]
        with _state(data_dir) as (db, _):
            db.execute("UPDATE accounts SET calendar_id=?,pending=0 WHERE account=?", (calendar_id, account))
    counts = {"create": 0, "update": 0, "remove": 0}
    # Upserts precede removals: a failed creation never removes the old schedule.
    for operation in operations:
        check_current()
        event, action = operation["event"], operation["action"]
        _assert_owned(event, owner)
        actual = provider.get_event(calendar_id, owner, event["id"])
        if actual and actual.get("status") != "cancelled":
            _assert_owned(actual, owner)
        if action == "create":
            if actual and actual.get("status") != "cancelled":
                if _content(actual) != _content(event):
                    raise GoogleWorkspaceError("A calendar event changed. Preview again.")
            else:
                check_future(event)
                provider.before_write = partial(check_future, event)
                provider.insert_event(calendar_id, owner, event)
                counts[action] += 1
        elif action in {"update", "remove"}:
            if not actual or actual.get("status") == "cancelled":
                if action == "update":
                    raise GoogleWorkspaceError("A calendar event was removed. Preview again.")
                continue
            previous = operation["previous"]
            if action == "update" and _content(actual) == _content(event):
                continue
            if (_content(actual) != _content(previous)
                    or (previous.get("etag") and actual.get("etag") != previous["etag"])):
                raise GoogleWorkspaceError("A calendar event changed. Preview again.")
            if action == "update":
                check_future(actual, event)
                provider.before_write = partial(check_future, actual, event)
                provider.update_event(calendar_id, owner, actual, event)
            else:
                check_future(actual)
                provider.before_write = partial(check_future, actual)
                private = actual["extendedProperties"]["private"]
                _generation(data_dir, account, private["sf_key"],
                            advance_from=int(private["sf_generation"]))
                provider.delete_event(calendar_id, owner, actual)
            counts[action] += 1
        else:
            raise GoogleWorkspaceError("Invalid sync operation. Preview again.")
    return {"account": account, "calendar_id": calendar_id, "counts": counts}
