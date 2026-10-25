"""Shared schedule inputs and checks before exporting a saved plan."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime

from studentflow.availability import resolve_availability
from studentflow.planner import PLANNING_POLICY_VERSION, validate_schedule


def with_fixed_events(preferences: dict, events: list[dict]) -> dict:
    from studentflow.fixed_events import as_busy_events

    result = deepcopy(preferences)
    result["busy_events"] = list(result.get("busy_events", [])) + as_busy_events(events)
    return result


def _task_state(tasks: list[dict]) -> list[dict]:
    fields = ("id", "title", "status", "due_at", "estimated_minutes", "completed_minutes")
    return sorted(
        [{field: task.get(field, 0 if field == "completed_minutes" else None)
          for field in fields} for task in tasks],
        key=lambda item: str(item["id"]),
    )


def source_fingerprint(tasks: list[dict], preferences: dict, events: list[dict], progress: dict) -> str:
    """No clock/credentials: a reviewed export changes only with its real inputs."""
    payload = {"tasks": _task_state(tasks), "preferences": preferences,
               "fixed_events": events, "completed_by_day": progress,
               "planning_policy": PLANNING_POLICY_VERSION}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def saved_plan_is_fresh(plan: dict, tasks: list[dict], preferences: dict,
                        events: list[dict], progress: dict, now: str) -> bool:
    """Refuse stale task facts or constraints; permit harmless expired availability cleanup.

    Old saved plans have no preference fingerprint. Revalidate their future blocks
    against today's resolved availability instead of assuming they are current.
    """
    try:
        inputs = plan["inputs"]
        if _task_state(inputs["tasks"]) != _task_state(tasks):
            return False
        effective = with_fixed_events(preferences, events)
        settings = effective["settings"]
        previous_settings = {key: value for key, value in inputs["settings"].items()
                             if key not in {"now", "completed_by_day"}}
        if previous_settings != settings:
            return False
        # Retaining expired appointments does not make a future plan invalid.
        instant = datetime.fromisoformat(now)
        if instant.utcoffset() is None:
            return False

        def future_busy(items):
            return sorted(
                [item for item in items if datetime.fromisoformat(item["end"]) > instant],
                key=lambda item: json.dumps(item, sort_keys=True),
            )

        if future_busy(inputs.get("busy", [])) != future_busy(effective.get("busy_events", [])):
            return False
        # Sync preserves started sessions; their elapsed minutes must not make
        # unchanged later sessions appear stale halfway through the day.
        blocks = [block for block in plan["result"]["blocks"]
                  if datetime.fromisoformat(block["start"]) >= instant]
        errors = validate_schedule(
            blocks, tasks, resolve_availability(effective, now), effective.get("busy_events", []),
            settings | {"now": now, "completed_by_day": progress},
        )
        return not errors
    except (ValueError, TypeError, KeyError, OverflowError):
        return False
