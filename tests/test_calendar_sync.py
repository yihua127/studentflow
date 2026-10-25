"""Offline account/ownership, retry and preview contracts; no real Google calls."""

import json
from copy import deepcopy
from datetime import datetime

import pytest

from studentflow.calendar_sync import (
    CalendarProvider,
    _content,
    _marker,
    apply_sync,
    prepare_sync,
)
from studentflow.google_workspace import (
    CALENDAR_SYNC_SCOPES,
    GoogleWorkspaceAuthError,
    GoogleWorkspaceClient,
    GoogleWorkspaceError,
)

NOW = datetime.fromisoformat("2026-10-24T00:00:00+08:00")


class FakeProvider:
    def __init__(self):
        self.email = "student@example.test"
        self.calendar_rows = []
        self.event_rows = {}
        self.calls = []
        self.fail_insert_once = False
        self.fail_calendar_once = False
        self.fail_calendar_before_send = False
        self.write_scope = True

    def account(self):
        return self.email

    def require_write_scope(self):
        if not self.write_scope:
            raise GoogleWorkspaceAuthError("Reconnect Calendar sync")

    def calendars(self):
        return deepcopy(self.calendar_rows)

    def create_calendar(self, owner):
        self.calls.append(("calendar", owner))
        if self.fail_calendar_before_send:
            raise GoogleWorkspaceError("Network interrupted")
        item = {"id": str(len(self.calendar_rows)) + "@group.calendar.google.com",
                "description": _marker(owner), "accessRole": "owner", "primary": False}
        self.calendar_rows.append(item)
        if self.fail_calendar_once:
            self.fail_calendar_once = False
            raise GoogleWorkspaceError("Network interrupted")
        return deepcopy(item)

    def events(self, calendar_id, owner):
        return [deepcopy(event) for (calendar, _), event in self.event_rows.items()
                if calendar == calendar_id and event.get("status") != "cancelled"]

    def get_event(self, calendar_id, owner, event_id):
        return deepcopy(self.event_rows.get((calendar_id, event_id)))

    def insert_event(self, calendar_id, owner, body):
        self.calls.append(("insert", body["id"]))
        self.event_rows[(calendar_id, body["id"])] = deepcopy(body) | {"etag": "v1"}
        if self.fail_insert_once:
            self.fail_insert_once = False
            raise GoogleWorkspaceError("Network interrupted")
        return deepcopy(body)

    def update_event(self, calendar_id, owner, previous, body):
        self.calls.append(("update", body["id"]))
        self.event_rows[(calendar_id, body["id"])] = deepcopy(body) | {"etag": "v2"}

    def delete_event(self, calendar_id, owner, previous):
        self.calls.append(("delete", previous["id"]))
        self.event_rows[(calendar_id, previous["id"])] = {"id": previous["id"], "status": "cancelled"}


def block(task="task-1", start="09:00", end="09:45", title="Review notes"):
    return {"task_id": task, "title": title,
            "start": "2026-10-24T" + start + ":00+08:00",
            "end": "2026-10-24T" + end + ":00+08:00"}


def plan(*blocks):
    return {"id": "saved-plan", "result": {"blocks": list(blocks)}}


def preview(provider, tmp_path, value=None, events=(), **kwargs):
    return prepare_sync(provider, tmp_path, value or plan(block()), list(events),
                        fingerprint="revision1", plan_is_fresh=True, now=NOW, **kwargs)


def apply(provider, tmp_path, value, **kwargs):
    return apply_sync(provider, tmp_path, value,
                      current_fingerprint=kwargs.pop("current_fingerprint", lambda: "revision1"),
                      load_plan=kwargs.pop("load_plan", lambda: plan(block())),
                      saved_plan_is_fresh=kwargs.pop("saved_plan_is_fresh", lambda _: True),
                      now=kwargs.pop("now", NOW), **kwargs)


def test_preview_never_writes_to_google_and_repeated_sync_is_idempotent(tmp_path):
    provider = FakeProvider()
    first = preview(provider, tmp_path)
    assert not provider.calls
    assert first["create_calendar"]
    assert [op["action"] for op in first["operations"]] == ["create"]
    apply(provider, tmp_path, first)
    assert [call[0] for call in provider.calls] == ["calendar", "insert"]
    assert preview(provider, tmp_path)["operations"] == []
    # Replaying the same confirmed operation is safe even after a restart.
    apply(provider, tmp_path, first)
    assert [call[0] for call in provider.calls] == ["calendar", "insert"]


def test_changed_time_replaces_future_session_and_deleted_time_can_be_reused(tmp_path):
    provider = FakeProvider()
    apply(provider, tmp_path, preview(provider, tmp_path))
    moved = preview(provider, tmp_path, plan(block(start="10:00", end="10:45")))
    assert [op["action"] for op in moved["operations"]] == ["create", "remove"]
    original_id = moved["operations"][-1]["event"]["id"]
    apply(provider, tmp_path, moved)
    back = preview(provider, tmp_path)
    assert back["operations"][0]["event"]["id"] != original_id
    apply(provider, tmp_path, back)
    assert preview(provider, tmp_path)["operations"] == []


def test_title_or_duration_change_updates_in_place(tmp_path):
    provider = FakeProvider()
    apply(provider, tmp_path, preview(provider, tmp_path))
    changed = preview(provider, tmp_path, plan(block(end="10:00", title="Study revised notes")))
    assert [op["action"] for op in changed["operations"]] == ["update"]
    apply(provider, tmp_path, changed)
    assert provider.calls[-1][0] == "update"


def test_partial_insert_timeout_is_recovered_without_duplicate(tmp_path):
    provider = FakeProvider()
    provider.fail_insert_once = True
    value = plan(block(), block(task="task-2", start="10:00", end="10:30"))
    first = preview(provider, tmp_path, value)
    with pytest.raises(GoogleWorkspaceError, match="interrupted"):
        apply(provider, tmp_path, first)
    retry = preview(provider, tmp_path, value)
    assert len(retry["operations"]) == 1
    apply(provider, tmp_path, retry)
    assert len(provider.event_rows) == 2
    assert len([call for call in provider.calls if call[0] == "insert"]) == 2


def test_ambiguous_calendar_create_recovers_by_owner_marker(tmp_path):
    provider = FakeProvider()
    provider.fail_calendar_once = True
    with pytest.raises(GoogleWorkspaceError, match="interrupted"):
        apply(provider, tmp_path, preview(provider, tmp_path))
    second = preview(provider, tmp_path)
    assert not second["create_calendar"]
    apply(provider, tmp_path, second)
    assert len(provider.calendar_rows) == 1


def test_unknown_calendar_creation_outcome_never_blindly_retries(tmp_path):
    provider = FakeProvider()
    provider.fail_calendar_before_send = True
    with pytest.raises(GoogleWorkspaceError):
        apply(provider, tmp_path, preview(provider, tmp_path))
    with pytest.raises(GoogleWorkspaceError, match="awaiting confirmation"):
        preview(provider, tmp_path)
    assert len(provider.calls) == 1


def test_unrelated_or_forged_events_are_never_removed(tmp_path):
    provider = FakeProvider()
    first = preview(provider, tmp_path)
    apply(provider, tmp_path, first)
    calendar = provider.calendar_rows[0]["id"]
    event = deepcopy(first["operations"][0]["event"])
    event["id"] = "unrelated-event"
    provider.event_rows[(calendar, event["id"])] = event
    unchanged = preview(provider, tmp_path)
    assert unchanged["operations"] == []
    apply(provider, tmp_path, unchanged)
    assert (calendar, "unrelated-event") in provider.event_rows


def test_owned_event_with_guests_is_not_modified_or_deleted(tmp_path):
    provider = FakeProvider()
    apply(provider, tmp_path, preview(provider, tmp_path))
    next(iter(provider.event_rows.values()))["attendees"] = [{"email": "friend@example.test"}]
    with pytest.raises(GoogleWorkspaceError, match="shared"):
        preview(provider, tmp_path, plan(block(start="10:00", end="10:45")))
    assert len(provider.calls) == 2


def test_google_edit_between_preview_and_apply_is_not_overwritten(tmp_path):
    provider = FakeProvider()
    apply(provider, tmp_path, preview(provider, tmp_path))
    changed = preview(provider, tmp_path, plan(block(title="New title")))
    next(iter(provider.event_rows.values()))["summary"] = "Manually edited in Calendar"
    with pytest.raises(GoogleWorkspaceError, match="changed"):
        apply(provider, tmp_path, changed)
    assert len(provider.calls) == 2


def test_account_switch_blocks_old_preview_and_creates_separate_calendar(tmp_path):
    provider = FakeProvider()
    first = preview(provider, tmp_path)
    apply(provider, tmp_path, first)
    provider.email = "second@example.test"
    with pytest.raises(GoogleWorkspaceError, match="account changed"):
        apply(provider, tmp_path, first)
    second = preview(provider, tmp_path)
    assert second["owner"] != first["owner"]
    apply(provider, tmp_path, second)
    assert len(provider.calendar_rows) == 2


def test_missing_write_scope_keeps_preview_readonly(tmp_path):
    provider = FakeProvider()
    provider.write_scope = False
    with pytest.raises(GoogleWorkspaceAuthError):
        preview(provider, tmp_path)
    assert not provider.calls


@pytest.mark.parametrize("change", ["revision", "freshness", "expired", "started", "tampered"])
def test_stale_or_altered_preview_cannot_write(tmp_path, change):
    provider = FakeProvider()
    first = preview(provider, tmp_path)
    kwargs = {}
    if change == "revision":
        kwargs["current_fingerprint"] = lambda: "revision2"
    elif change == "freshness":
        kwargs["saved_plan_is_fresh"] = lambda _: False
    elif change == "expired":
        kwargs["now"] = datetime.fromisoformat("2026-10-24T00:16:00+08:00")
    elif change == "started":
        # A later clock must also reject, regardless of expired preview check.
        kwargs["now"] = datetime.fromisoformat("2026-10-24T09:01:00+08:00")
    else:
        first["operations"][0]["event"]["summary"] = "Tampered"
    with pytest.raises(GoogleWorkspaceError):
        apply(provider, tmp_path, first, **kwargs)
    assert not provider.calls


def test_only_confirmed_fixed_events_for_selected_account_are_synced(tmp_path):
    provider = FakeProvider()
    event = {"id": "exam1", "title": "Examination", "start_at": block()["start"],
             "end_at": block()["end"], "account": provider.email, "status": "confirmed"}
    events = [event, event | {"id": "pending", "status": "pending"},
              event | {"id": "other-account", "account": "someone@example.test"}]
    first = preview(provider, tmp_path, plan(), events=events)
    assert len(first["operations"]) == 1
    assert first["operations"][0]["event"]["summary"] == "Examination"
    assert first["excluded_fixed_count"] == 1
    assert "attendees" not in first["operations"][0]["event"]
    apply(provider, tmp_path, first)
    moved = preview(provider, tmp_path, plan(), events=[event | {
        "start_at": block(start="10:00")["start"], "end_at": block(end="10:45")["end"],
    }])
    assert moved["operations"][0]["action"] == "update"


def test_past_history_is_retained_and_not_exported_again(tmp_path):
    provider = FakeProvider()
    first = preview(provider, tmp_path)
    apply(provider, tmp_path, first)
    later = datetime.fromisoformat("2026-10-24T11:00:00+08:00")
    second = prepare_sync(provider, tmp_path, plan(), [], fingerprint="revision1",
                          plan_is_fresh=True, now=later)
    assert second["operations"] == []
    assert len(provider.event_rows) == 1


def test_new_fingerprint_is_checked_between_mutations(tmp_path):
    provider = FakeProvider()
    first = preview(provider, tmp_path, plan(block(), block("task-2", "10:00", "10:30")))
    with pytest.raises(GoogleWorkspaceError, match="changed"):
        apply(provider, tmp_path, first, current_fingerprint=lambda: (
            "changed" if provider.event_rows else "revision1"
        ))
    assert len(provider.event_rows) == 1


def test_equivalent_timezone_representation_does_not_trigger_update(tmp_path):
    provider = FakeProvider()
    apply(provider, tmp_path, preview(provider, tmp_path))
    value = next(iter(provider.event_rows.values()))
    value["start"]["dateTime"] = "2026-10-24T09:00:00+08:00"
    assert _content(value)["start"] == "2026-10-24T01:00:00+00:00"
    assert preview(provider, tmp_path)["operations"] == []


class HttpResponse:
    def __init__(self, value, status=200):
        self.value, self.status_code, self.closed = value, status, False
        self.headers = {}

    def iter_content(self, chunk_size):
        yield json.dumps(self.value).encode()

    def close(self):
        self.closed = True


class HttpSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


def adapter(responses):
    session = HttpSession(responses)
    return CalendarProvider(GoogleWorkspaceClient(session, granted_scopes=CALENDAR_SYNC_SCOPES)), session


def test_provider_insert_never_sends_guests_and_disables_invitations(tmp_path):
    body = preview(FakeProvider(), tmp_path)["operations"][0]["event"]
    owner = body["extendedProperties"]["private"]["sf_owner"]
    calendars = HttpResponse({"items": [{"id": "secondary@group.calendar.google.com",
                                         "description": _marker(owner), "accessRole": "owner"}]})
    provider, session = adapter([calendars, HttpResponse(body)])
    provider.insert_event("secondary@group.calendar.google.com", owner, body)
    method, url, args = session.calls[-1]
    assert method == "POST" and url.endswith("/events")
    assert args["params"] == {"sendUpdates": "none"}
    assert args["allow_redirects"] is False
    assert "attendees" not in args["json"]
    assert calendars.closed


def test_provider_refuses_primary_even_with_copied_marker(tmp_path):
    body = preview(FakeProvider(), tmp_path)["operations"][0]["event"]
    owner = body["extendedProperties"]["private"]["sf_owner"]
    provider, session = adapter([HttpResponse({"items": [
        {"id": "primary", "primary": True, "description": _marker(owner), "accessRole": "owner"},
    ]})])
    with pytest.raises(GoogleWorkspaceError, match="unavailable"):
        provider.insert_event("primary", owner, body)
    assert [call[0] for call in session.calls] == ["GET"]


def test_provider_insert_conflict_reads_and_verifies_same_owned_event(tmp_path):
    body = preview(FakeProvider(), tmp_path)["operations"][0]["event"]
    owner = body["extendedProperties"]["private"]["sf_owner"]
    row = {"id": "secondary", "description": _marker(owner), "accessRole": "owner"}
    provider, session = adapter([
        HttpResponse({"items": [row]}), HttpResponse({}, 409),
        HttpResponse({"items": [row]}), HttpResponse(body),
    ])
    assert provider.insert_event("secondary", owner, body)["id"] == body["id"]
    assert [call[0] for call in session.calls] == ["GET", "POST", "GET", "GET"]


@pytest.mark.parametrize("status", [401, 403])
def test_expired_or_missing_remote_scope_returns_reauthorization_error(status):
    provider, _ = adapter([HttpResponse({}, status)])
    with pytest.raises(GoogleWorkspaceAuthError) as error:
        provider.create_calendar("a" * 64)
    assert error.value.request_rejected


def test_network_error_does_not_expose_private_request():
    provider, _ = adapter([RuntimeError("secret-token@example.test")])
    with pytest.raises(GoogleWorkspaceError) as error:
        provider.create_calendar("a" * 64)
    assert "secret" not in str(error.value)
    assert not getattr(error.value, "request_rejected", False)


def test_definitive_create_rejection_allows_safe_retry(tmp_path):
    class RejectedProvider(FakeProvider):
        def create_calendar(self, owner):
            error = GoogleWorkspaceAuthError("Reconnect")
            error.request_rejected = True
            raise error

    provider = RejectedProvider()
    with pytest.raises(GoogleWorkspaceAuthError):
        apply(provider, tmp_path, preview(provider, tmp_path))
    assert preview(provider, tmp_path)["create_calendar"]


def test_fixed_only_sync_keeps_existing_study_events_even_with_stale_plan(tmp_path):
    provider = FakeProvider()
    original = preview(provider, tmp_path)
    apply(provider, tmp_path, original)
    study_id = original["operations"][0]["event"]["id"]
    event = {"id": "exam", "title": "Exam", "start_at": block()["start"],
             "end_at": block()["end"], "status": "confirmed", "account": provider.email}
    fixed_only = prepare_sync(
        provider, tmp_path, plan(block()), [event], fingerprint="revision1",
        plan_is_fresh=False, now=NOW, include_study_plan=False,
    )
    assert [op["action"] for op in fixed_only["operations"]] == ["create"]
    apply(provider, tmp_path, fixed_only, saved_plan_is_fresh=lambda _: False)
    assert any(value.get("id") == study_id and value.get("status") != "cancelled"
               for value in provider.event_rows.values())
    assert len(provider.event_rows) == 2


@pytest.mark.parametrize("action", ["create", "update", "remove"])
def test_clock_rechecked_after_slow_remote_read_before_every_event_mutation(tmp_path, action):
    provider = FakeProvider()
    if action != "create":
        apply(provider, tmp_path, preview(provider, tmp_path))
    value = plan() if action == "remove" else plan(block(title="Updated title"))
    first = prepare_sync(provider, tmp_path, value, [], fingerprint="revision1",
                         plan_is_fresh=True, now=NOW)
    current = [NOW]
    original_get = provider.get_event

    def slow_get(*args):
        result = original_get(*args)
        current[0] = datetime.fromisoformat("2026-10-24T09:01:00+08:00")
        return result

    provider.get_event = slow_get
    previous_event_calls = [call for call in provider.calls if call[0] != "calendar"]
    with pytest.raises(GoogleWorkspaceError, match="already started"):
        apply(provider, tmp_path, first, clock=lambda: current[0])
    assert [call for call in provider.calls if call[0] != "calendar"] == previous_event_calls


def test_http_boundary_guard_runs_after_calendar_ownership_network_lookup(tmp_path):
    body = preview(FakeProvider(), tmp_path)["operations"][0]["event"]
    owner = body["extendedProperties"]["private"]["sf_owner"]
    provider, session = adapter([HttpResponse({"items": [
        {"id": "secondary", "description": _marker(owner), "accessRole": "owner"},
    ]})])

    def guard():
        assert len(session.calls) == 1
        raise GoogleWorkspaceError("Session has started")

    provider.before_write = guard
    with pytest.raises(GoogleWorkspaceError, match="started"):
        provider.insert_event("secondary", owner, body)
    assert len(session.calls) == 1
