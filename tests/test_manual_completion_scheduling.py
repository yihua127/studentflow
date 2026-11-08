"""Manual checkoff must invalidate saved plans and previously reviewed Calendar writes."""

from copy import deepcopy
from datetime import datetime, time

import pytest

from studentflow.availability import weekly_rule
from studentflow.calendar_sync import apply_sync, prepare_sync
from studentflow.google_workspace import GoogleWorkspaceError
from studentflow.planner import propose_schedule, validate_schedule
from studentflow.planning_ui import default_preferences, make_draft
from studentflow.scheduling_context import saved_plan_is_fresh, source_fingerprint
from studentflow.storage import Store

NOW = "2030-01-07T08:00:00+08:00"


@pytest.fixture
def workspace(tmp_path):
    store = Store(tmp_path / "tasks.sqlite3")
    identifier = store.save_task({
        "mail_id": "synthetic-mail", "title": "Review source notes", "status": "confirmed",
        "due_at": "2030-01-07T18:00:00+08:00", "estimated_minutes": 60,
        "completed_minutes": 15,
    })
    preferences = default_preferences()
    preferences["weekly_availability"] = [weekly_rule([0], time(9), time(17))]
    draft = make_draft(store.tasks(), preferences, NOW, {})
    assert draft["result"]["blocks"] and not draft["validation_errors"]
    store.save_plan(draft)
    return store, identifier, preferences


def checkoff(store, identifier, complete):
    current = next(task for task in store.tasks() if task["id"] == identifier)
    return store.set_task_completion(
        identifier, complete, expected_updated_at=current["updated_at"],
    )


def test_checkoff_blocks_old_plan_but_preserves_logged_time_and_reopening(workspace):
    store, identifier, preferences = workspace
    original = store.latest_plan()
    assert saved_plan_is_fresh(original, store.tasks(), preferences, [], {}, NOW)
    checked = checkoff(store, identifier, True)
    assert checked["is_completed"] and checked["completed_minutes"] == 15
    assert not saved_plan_is_fresh(original, store.tasks(), preferences, [], {}, NOW)
    new = make_draft(store.tasks(), preferences, NOW, {})
    assert new["result"]["blocks"] == [] and not new["validation_errors"]

    reopened = checkoff(store, identifier, False)
    assert not reopened["is_completed"] and reopened["completed_minutes"] == 15
    regenerated = make_draft(store.tasks(), preferences, NOW, {})
    assert sum(block["minutes"] for block in regenerated["result"]["blocks"]) == 45


def test_independent_validator_rejects_a_block_for_manually_completed_task(workspace):
    store, identifier, _ = workspace
    old = store.latest_plan()
    checkoff(store, identifier, True)
    inputs = old["inputs"]
    tasks = store.tasks()
    assert not propose_schedule(tasks, inputs["slots"], inputs["busy"], inputs["settings"])["blocks"]
    issues = validate_schedule(
        old["result"]["blocks"], tasks, inputs["slots"], inputs["busy"], inputs["settings"],
    )
    assert any("already completed" in issue for issue in issues)


def test_fingerprint_tracks_checkoff_but_ignores_cosmetic_color_and_legacy_false(workspace):
    store, identifier, preferences = workspace
    initial = source_fingerprint(store.tasks(), preferences, [], {})
    legacy_false = [{**task, "marked_complete": False} for task in store.tasks()]
    assert source_fingerprint(legacy_false, preferences, [], {}) == initial
    store.set_deadline_color(identifier, "pink")
    assert source_fingerprint(store.tasks(), preferences, [], {}) == initial
    checkoff(store, identifier, True)
    assert source_fingerprint(store.tasks(), preferences, [], {}) != initial


class PreviewOnlyProvider:
    """Any provider operation after preview is a failure in these stale-approval tests."""

    def __init__(self):
        self.calls = []

    def require_write_scope(self):
        self.calls.append("scope")

    def account(self):
        self.calls.append("account")
        return "redteam@example.invalid"

    def calendars(self):
        self.calls.append("calendars")
        return []

    def create_calendar(self, owner):
        self.calls.append("create_calendar")
        pytest.fail("A stale checkoff preview reached a Calendar write")


@pytest.mark.parametrize("change", ["complete", "reopen"])
def test_completion_change_blocks_previously_reviewed_sync_before_provider_calls(
    workspace, tmp_path, change,
):
    store, identifier, preferences = workspace
    if change == "reopen":
        checkoff(store, identifier, True)
        store.save_plan(make_draft(store.tasks(), preferences, NOW, {}))
    provider = PreviewOnlyProvider()
    def fingerprint():
        return source_fingerprint(store.tasks(), preferences, [], {})

    def fresh(plan):
        return saved_plan_is_fresh(plan, store.tasks(), preferences, [], {}, NOW)

    preview = prepare_sync(
        provider, tmp_path / "sync", store.latest_plan(), [],
        fingerprint=fingerprint(), plan_is_fresh=fresh(store.latest_plan()),
        now=datetime.fromisoformat(NOW),
    )
    before = deepcopy(preview)
    provider.calls.clear()
    checkoff(store, identifier, change == "complete")
    with pytest.raises(GoogleWorkspaceError, match="changed"):
        apply_sync(
            provider, tmp_path / "sync", preview, current_fingerprint=fingerprint,
            load_plan=store.latest_plan, saved_plan_is_fresh=fresh,
            now=datetime.fromisoformat(NOW),
        )
    assert provider.calls == []
    assert preview == before
