"""Deterministic draft scheduling; no model calls, database writes, or route lookups.

Inputs and outputs are ordinary dictionaries. All arithmetic uses UTC instants;
daily limits and wall-clock study/meal windows use the configured local timezone.
The greedy schedule is a proposal, not a proof that a different plan is impossible.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

Interval = tuple[datetime, datetime]
DEFAULT_MEALS = [
    {"start": "08:00", "end": "08:30"},
    {"start": "11:30", "end": "12:00"},
    {"start": "17:30", "end": "18:00"},
]


def _instant(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError("Times must include a timezone offset in ISO format.")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("The time has no timezone offset.")
    return parsed.astimezone(UTC)


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be a whole number of at least {minimum}.")
    return value


def _clock(value: Any) -> time:
    parsed = time.fromisoformat(value)
    if parsed.tzinfo is not None:
        raise ValueError("Daily times must use local HH:MM format without a timezone.")
    return parsed


def _union(intervals: list[Interval]) -> list[Interval]:
    result: list[Interval] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def _subtract(intervals: list[Interval], exclusions: list[Interval]) -> list[Interval]:
    result: list[Interval] = []
    for start, end in intervals:
        cursor = start
        for blocked_start, blocked_end in _union(exclusions):
            if blocked_end <= cursor:
                continue
            if blocked_start >= end:
                break
            if cursor < blocked_start:
                result.append((cursor, min(end, blocked_start)))
            cursor = max(cursor, blocked_end)
            if cursor >= end:
                break
        if cursor < end:
            result.append((cursor, end))
    return result


def _interval(value: dict, label: str) -> Interval:
    start, end = _instant(value.get("start")), _instant(value.get("end"))
    if end <= start:
        raise ValueError(f"{label} must end after it starts.")
    return start, end


@dataclass
class _Context:
    zone: ZoneInfo
    free: list[Interval]
    allowed: list[Interval]
    blocked: list[Interval]
    daily_limit: int
    max_block: int
    break_minutes: int
    buffer_minutes: int
    now: datetime | None
    completed_by_day: dict[date, int]
    warnings: list[str]


def _context(available_slots: list[dict], busy_events: list[dict], settings: dict) -> _Context:
    zone = ZoneInfo(settings.get("timezone", "Asia/Singapore"))
    study_start = _clock(settings.get("study_start", "08:00"))
    study_end = _clock(settings.get("study_end", "22:00"))
    if study_end <= study_start:
        raise ValueError("The daily study window must end after it starts on the same day.")
    # These bounds come from this project's supplied schedule policy.
    if study_start < time(8) or study_end > time(22):
        raise ValueError("Study hours must be between 08:00 and 22:00.")
    daily_limit = _integer(settings.get("daily_limit_minutes", 360), "Daily study limit")
    max_block = _integer(settings.get("max_block_minutes", 120), "Session duration limit", 1)
    break_minutes = _integer(settings.get("break_minutes", 30), "Break between sessions", 30)
    buffer_minutes = _integer(settings.get("buffer_minutes", 0), "Preferred deadline buffer")
    if daily_limit > 360 or max_block > 120:
        raise ValueError("Study limits cannot exceed 360 minutes per day or 120 minutes per session.")
    default_commute = _integer(
        settings.get("default_commute_minutes", 40), "Default one-way commute"
    )
    manual_commute = settings.get("manual_commute_minutes")
    if manual_commute is not None:
        manual_commute = _integer(manual_commute, "Custom one-way commute")
    now = _instant(settings["now"]) if settings.get("now") is not None else None
    completed_by_day = {
        date.fromisoformat(day): _integer(minutes, "Completed study time")
        for day, minutes in settings.get("completed_by_day", {}).items()
    }
    source_slots = _union([_interval(slot, "Available time") for slot in available_slots])
    meals = []
    for item in settings.get("meals", DEFAULT_MEALS):
        start, end = _clock(item["start"]), _clock(item["end"])
        if end <= start:
            raise ValueError("A meal must end after it starts.")
        meals.append((start, end))

    allowed: list[Interval] = []
    meal_blocks: list[Interval] = []
    for slot_start, slot_end in source_slots:
        day = slot_start.astimezone(zone).date()
        last_day = slot_end.astimezone(zone).date()
        while day <= last_day:
            start = datetime.combine(day, study_start, zone).astimezone(UTC)
            end = datetime.combine(day, study_end, zone).astimezone(UTC)
            clipped_start = max(slot_start, start, now) if now else max(slot_start, start)
            clipped_end = min(slot_end, end)
            if clipped_end > clipped_start:
                allowed.append((clipped_start, clipped_end))
            for meal_start, meal_end in meals:
                meal_blocks.append(
                    (
                        datetime.combine(day, meal_start, zone).astimezone(UTC),
                        datetime.combine(day, meal_end, zone).astimezone(UTC),
                    )
                )
            day += timedelta(days=1)

    # Merge touching/overlapping sessions at one physical location before adding
    # the inbound and outbound buffers. Online events never receive travel time.
    sessions: list[dict] = []
    default_count = 0
    for event in busy_events:
        start, end = _interval(event, "Existing event")
        online = str(event.get("mode", "")).strip().lower() == "online"
        commute = event.get("commute_minutes")
        if online:
            commute = 0
        elif commute is not None:
            commute = _integer(commute, "Event one-way commute")
        elif manual_commute is not None:
            commute = manual_commute
        else:
            commute = default_commute
            default_count += 1
        sessions.append(
            {
                "start": start,
                "end": end,
                "location": str(event.get("location") or "").strip().casefold(),
                "online": online,
                "before": commute,
                "after": commute,
            }
        )
    merged: list[dict] = []
    for event in sorted(sessions, key=lambda item: item["start"]):
        previous = merged[-1] if merged else None
        if (
            previous
            and not event["online"]
            and not previous["online"]
            and event["location"]
            and event["location"] == previous["location"]
            and event["start"] <= previous["end"]
        ):
            if event["start"] == previous["start"]:
                previous["before"] = max(previous["before"], event["before"])
            if event["end"] > previous["end"]:
                previous["end"] = event["end"]
                previous["after"] = event["after"]
            elif event["end"] == previous["end"]:
                previous["after"] = max(previous["after"], event["after"])
        else:
            merged.append(event.copy())
    event_blocks = [
        (
            item["start"] - timedelta(minutes=item["before"]),
            item["end"] + timedelta(minutes=item["after"]),
        )
        for item in merged
    ]
    blocked = _union(meal_blocks + event_blocks)
    warnings = []
    if default_count:
        warnings.append(
            f"{default_count} event(s) have no commute estimate. {default_commute} minutes have been reserved before and after each event. "
            "Travel time has not been checked against a route. Mark online events as Online to skip the commute."
        )
    return _Context(
        zone=zone,
        free=_subtract(_union(allowed), blocked),
        allowed=_union(allowed),
        blocked=blocked,
        daily_limit=daily_limit,
        max_block=max_block,
        break_minutes=break_minutes,
        buffer_minutes=buffer_minutes,
        now=now,
        completed_by_day=completed_by_day,
        warnings=warnings,
    )


def _task_info(task: dict, duplicates: set[str]) -> tuple[str, str, int, datetime | None, str]:
    task_id = str(task.get("id") or "")
    title = str(task.get("title") or "Untitled task")
    estimate = task.get("estimated_minutes")
    minutes = estimate if isinstance(estimate, int) and not isinstance(estimate, bool) else 0
    completed = task.get("completed_minutes", 0)
    if isinstance(completed, bool) or not isinstance(completed, int) or completed < 0:
        return task_id, title, max(0, minutes), None, "Completed study time must be a nonnegative whole number."
    if minutes > 0 and completed >= minutes:
        return task_id, title, 0, None, "This task is already completed."
    minutes = max(0, minutes - completed)
    if not task_id or task_id in duplicates:
        return task_id, title, max(0, minutes), None, "The task ID is missing or duplicated. Correct it before scheduling."
    if task.get("status") != "confirmed":
        return task_id, title, max(0, minutes), None, "The task is not confirmed. Review and confirm it before scheduling."
    if minutes <= 0:
        return task_id, title, 0, None, "Enter an estimated effort greater than zero, in whole minutes."
    if not task.get("due_at"):
        return task_id, title, minutes, None, "The deadline is missing. Enter and confirm it before scheduling."
    try:
        due = _instant(task["due_at"])
    except (ValueError, TypeError):
        return task_id, title, minutes, None, "The deadline is invalid or has no timezone. Correct it before scheduling."
    return task_id, title, minutes, due, ""


def _duplicate_ids(tasks: list[dict]) -> set[str]:
    counts = Counter(str(task.get("id") or "") for task in tasks)
    return {task_id for task_id, count in counts.items() if count > 1}


def is_completed(task: dict) -> bool:
    """Completion depends on recorded effort, separately from confirmation status."""
    estimate, completed = task.get("estimated_minutes"), task.get("completed_minutes", 0)
    return (
        isinstance(estimate, int) and not isinstance(estimate, bool) and estimate > 0
        and isinstance(completed, int) and not isinstance(completed, bool)
        and completed >= estimate
    )


def propose_schedule(
    tasks: list[dict], available_slots: list[dict], busy_events: list[dict], settings: dict
) -> dict:
    """Return a draft using earliest-deadline-first, splittable greedy allocation.

    Only explicitly confirmed tasks with a deadline and positive integer duration
    can enter a plan. ``now`` is optional so historic fixtures remain reproducible.
    ``buffer_minutes`` is a soft preference reported as a warning, never a hidden
    earlier deadline. The caller remains responsible for user confirmation.
    """
    result: dict = {"blocks": [], "unscheduled": [], "warnings": []}
    duplicates = _duplicate_ids(tasks)
    task_infos = [_task_info(task, duplicates) for task in tasks if not is_completed(task)]
    try:
        ctx = _context(available_slots, busy_events, settings)
    except (ValueError, TypeError, KeyError) as exc:
        reason = f"The schedule settings or events are invalid: {exc}"
        result["warnings"].append(reason)
        result["unscheduled"] = [
            {"task_id": item[0], "title": item[1], "remaining_minutes": item[2], "reason": reason}
            for item in task_infos
        ]
        return result
    result["warnings"].extend(ctx.warnings)
    candidates = []
    for task_id, title, minutes, due, reason in task_infos:
        if not reason and ctx.now is not None and due <= ctx.now:
            reason = "The deadline has passed. Update the task before scheduling."
        if reason:
            result["unscheduled"].append(
                {
                    "task_id": task_id,
                    "title": title,
                    "remaining_minutes": minutes,
                    "reason": reason,
                }
            )
        else:
            candidates.append((due, task_id, title, minutes))
    allocated: list[Interval] = []
    daily_used: dict = defaultdict(int, ctx.completed_by_day)
    for due, task_id, title, minutes in sorted(candidates):
        remaining = minutes
        padding = timedelta(minutes=ctx.break_minutes)
        free = _subtract(ctx.free, [(start - padding, end + padding) for start, end in allocated])
        soft_target_missed = False
        next_start = None
        for free_start, free_end in free:
            start, end = free_start, min(free_end, due)
            if next_start is not None:
                start = max(start, next_start)
            while start < end and remaining:
                day = start.astimezone(ctx.zone).date()
                capacity = int((end - start).total_seconds() // 60)
                amount = min(
                    remaining, capacity, ctx.max_block, ctx.daily_limit - daily_used[day]
                )
                if amount <= 0:
                    break
                finish = start + timedelta(minutes=amount)
                result["blocks"].append(
                    {
                        "task_id": task_id,
                        "title": title,
                        "start": start.astimezone(ctx.zone).isoformat(),
                        "end": finish.astimezone(ctx.zone).isoformat(),
                        "minutes": amount,
                    }
                )
                allocated.append((start, finish))
                daily_used[day] += amount
                remaining -= amount
                if ctx.buffer_minutes and finish > due - timedelta(minutes=ctx.buffer_minutes):
                    soft_target_missed = True
                next_start = finish + padding
                start = next_start
            if not remaining:
                break
        if soft_target_missed:
            result["warnings"].append(f"{title} is scheduled before its deadline, but without the preferred deadline buffer.")
        if remaining:
            result["unscheduled"].append(
                {
                    "task_id": task_id,
                    "title": title,
                    "remaining_minutes": remaining,
                    "reason": (
                        "This plan could not fit all the work before its deadline. Adjust availability, "
                        "effort estimates, or priorities and try again. A different arrangement may still be possible."
                    ),
                }
            )
    result["blocks"].sort(key=lambda block: _instant(block["start"]))
    return result


def validate_schedule(
    blocks: list[dict],
    tasks: list[dict],
    available_slots: list[dict],
    busy_events: list[dict],
    settings: dict,
) -> list[str]:
    """Independently check a proposed/edited plan before its explicit confirmation.

    This inspects the supplied blocks; it does not call the proposal algorithm or
    assume that a previously generated plan stayed valid after editing tasks.
    """
    try:
        ctx = _context(available_slots, busy_events, settings)
    except (ValueError, TypeError, KeyError) as exc:
        return [f"Cannot validate the plan. The schedule settings or events are invalid: {exc}"]
    issues = []
    duplicates = _duplicate_ids(tasks)
    task_map = {str(task.get("id") or ""): task for task in tasks}
    daily_seconds: dict = defaultdict(float)
    task_seconds: dict = defaultdict(float)
    intervals = []
    for index, block in enumerate(blocks, 1):
        label = f"Study session {index}"
        try:
            start, end = _interval(block, label)
        except (ValueError, TypeError, KeyError) as exc:
            issues.append(f"{label} is invalid: {exc}")
            continue
        intervals.append((start, end, index))
        seconds = (end - start).total_seconds()
        claimed_minutes = block.get("minutes")
        if (
            isinstance(claimed_minutes, bool)
            or not isinstance(claimed_minutes, int)
            or claimed_minutes <= 0
            or claimed_minutes * 60 != seconds
        ):
            issues.append(f"{label} has a duration that does not match its start and end times.")
        if seconds > ctx.max_block * 60:
            issues.append(f"{label} exceeds the session duration limit.")
        if not any(allowed_start <= start and end <= allowed_end for allowed_start, allowed_end in ctx.free):
            issues.append(f"{label} falls outside your availability or conflicts with an event, meal, or commute.")
        if ctx.now is not None and start < ctx.now:
            issues.append(f"{label} starts in the past.")
        local_day = start.astimezone(ctx.zone).date()
        if (end - timedelta(microseconds=1)).astimezone(ctx.zone).date() != local_day:
            issues.append(f"{label} crosses midnight and must be split.")
        daily_seconds[local_day] += seconds
        task_id = str(block.get("task_id") or "")
        task_seconds[task_id] += seconds
        task = task_map.get(task_id)
        if task is None:
            issues.append(f"{label} refers to a task that does not exist.")
            continue
        _, _, _, due, reason = _task_info(task, duplicates)
        if reason:
            issues.append(f"{label} cannot be scheduled: {reason}")
        elif end > due:
            issues.append(f"{label} ends after the task deadline.")
    for day, seconds in daily_seconds.items():
        if seconds + ctx.completed_by_day.get(day, 0) * 60 > ctx.daily_limit * 60:
            issues.append(f"Study time on {day.isoformat()} exceeds the daily limit.")
    for task_id, seconds in task_seconds.items():
        task = task_map.get(task_id)
        if task:
            _, _, minutes, _, reason = _task_info(task, duplicates)
            if not reason and seconds > minutes * 60:
                issues.append(f"The scheduled time for task {task_id} exceeds its estimated effort remaining after recorded progress.")
    # Keep the farthest end encountered, so a nested overlapping block cannot
    # conceal a later collision with an earlier, longer block.
    latest_end = None
    latest_index = None
    for start, end, index in sorted(intervals):
        if latest_end is not None:
            if start < latest_end:
                issues.append(f"Study sessions {latest_index} and {index} overlap.")
            elif start < latest_end + timedelta(minutes=ctx.break_minutes):
                issues.append(f"The break between study sessions {latest_index} and {index} is too short.")
        if latest_end is None or end > latest_end:
            latest_end, latest_index = end, index
    return issues
