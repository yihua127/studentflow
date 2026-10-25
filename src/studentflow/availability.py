"""Weekly availability and dated exceptions, resolved into bounded local windows.

Weekly rules are never expanded into persisted copies. Dated exclusions are applied
after all additions, so an overlapping weekly rule cannot undo a day off.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Asia/Singapore")
DEFAULT_HORIZON_DAYS = 28
MAX_HORIZON_DAYS = 90
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
Interval = tuple[datetime, datetime]


def _instant(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Availability times must include a timezone.")
    return parsed.astimezone(ZONE)


def _clock(value: str) -> time:
    parsed = time.fromisoformat(value)
    if parsed.tzinfo is not None or parsed.second or parsed.microsecond:
        raise ValueError("Use local times in HH:MM format.")
    return parsed


def _interval(value: dict) -> Interval:
    start, end = _instant(value["start"]), _instant(value["end"])
    if end <= start:
        raise ValueError("Available time must end after it starts.")
    return start, end


def weekly_rule(weekdays: list[int], start: time, end: time, *, rule_id: str | None = None) -> dict:
    """Build a rule only from explicitly chosen weekdays and a same-day window."""
    rule = {
        "id": rule_id or uuid4().hex,
        "weekdays": sorted(set(weekdays)),
        "start": start.isoformat(timespec="minutes"),
        "end": end.isoformat(timespec="minutes"),
    }
    _validate_rule(rule)
    return rule


def _validate_rule(rule: dict) -> None:
    if not isinstance(rule, dict) or not isinstance(rule.get("id"), str) or not rule["id"]:
        raise ValueError("Each weekly availability rule needs an identifier.")
    days = rule.get("weekdays")
    if (
        not isinstance(days, list)
        or not days
        or any(type(day) is not int or not 0 <= day <= 6 for day in days)
    ):
        raise ValueError("Choose at least one weekday.")
    if _clock(rule["end"]) <= _clock(rule["start"]):
        raise ValueError("End time must be later than start time on the same day.")


def dated_override(
    day: date, mode: str, start: time | None = None, end: time | None = None
) -> dict:
    """Create an added window, removed window, or whole-day exclusion."""
    if mode == "off":
        first = datetime.combine(day, time.min, ZONE)
        last = first + timedelta(days=1)
    elif mode in {"add", "remove"} and start is not None and end is not None:
        if end <= start:
            raise ValueError("End time must be later than start time on the same day.")
        first, last = datetime.combine(day, start, ZONE), datetime.combine(day, end, ZONE)
    else:
        raise ValueError("Choose added time, removed time, or a whole day off.")
    return {"id": uuid4().hex, "mode": mode, "start": first.isoformat(), "end": last.isoformat()}


def normalize_availability(preferences: dict) -> dict:
    """Migrate in memory without discarding legacy slots or unrelated settings."""
    value = deepcopy(preferences)
    value.setdefault("available_slots", [])
    value.setdefault("weekly_availability", [])
    value.setdefault("availability_overrides", [])
    value.setdefault("availability_horizon_days", DEFAULT_HORIZON_DAYS)
    horizon = value["availability_horizon_days"]
    if type(horizon) is not int or not 1 <= horizon <= MAX_HORIZON_DAYS:
        raise ValueError(f"Plan ahead must be between 1 and {MAX_HORIZON_DAYS} days.")
    for field in ("available_slots", "weekly_availability", "availability_overrides"):
        if not isinstance(value[field], list):
            raise TypeError("Saved availability must contain lists of time windows.")
    for rule in value["weekly_availability"]:
        _validate_rule(rule)
    for slot in value["available_slots"]:
        _interval(slot)
    for entry in value["availability_overrides"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
            raise ValueError("Each dated change needs an identifier.")
        if entry.get("mode") not in {"add", "remove", "off"}:
            raise ValueError("A dated change has an invalid type.")
        start, end = _interval(entry)
        if entry["mode"] == "off" and (
            start.time() != time.min or end != start + timedelta(days=1)
        ):
            raise ValueError("A whole day off must cover one complete local day.")
    return value


def prune_expired_availability(preferences: dict, now: str | datetime) -> tuple[dict, int]:
    """Drop only dated entries whose end has passed; retain today's future time."""
    value = normalize_availability(preferences)
    instant = _instant(now)
    removed = 0
    for field in ("available_slots", "availability_overrides"):
        existing = value[field]
        value[field] = [entry for entry in existing if _instant(entry["end"]) > instant]
        removed += len(existing) - len(value[field])
    return value, removed


def _union(intervals: list[Interval]) -> list[Interval]:
    result: list[Interval] = []
    for start, end in sorted(intervals):
        if start >= end:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def resolve_availability(preferences: dict, now: str | datetime) -> list[dict]:
    """Resolve this local date and the next horizon-1 dates; merge and subtract."""
    value = normalize_availability(preferences)
    instant = _instant(now)
    horizon = value["availability_horizon_days"]
    last = datetime.combine(instant.date() + timedelta(days=horizon), time.min, ZONE)
    additions = [_interval(slot) for slot in value["available_slots"]]
    exclusions = []
    for offset in range(horizon):
        day = instant.date() + timedelta(days=offset)
        for rule in value["weekly_availability"]:
            if day.weekday() in rule["weekdays"]:
                additions.append(
                    (
                        datetime.combine(day, _clock(rule["start"]), ZONE),
                        datetime.combine(day, _clock(rule["end"]), ZONE),
                    )
                )
    for entry in value["availability_overrides"]:
        target = additions if entry["mode"] == "add" else exclusions
        target.append(_interval(entry))
    additions = _union([(max(start, instant), min(end, last)) for start, end in additions])
    exclusions = _union(exclusions)
    resolved = []
    for start, end in additions:
        cursor = start
        for blocked_start, blocked_end in exclusions:
            if blocked_end <= cursor:
                continue
            if blocked_start >= end:
                break
            if cursor < blocked_start:
                resolved.append(
                    {"start": cursor.isoformat(), "end": min(end, blocked_start).isoformat()}
                )
            cursor = max(cursor, blocked_end)
            if cursor >= end:
                break
        if cursor < end:
            resolved.append({"start": cursor.isoformat(), "end": end.isoformat()})
    return resolved
