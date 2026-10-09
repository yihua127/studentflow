"""Deterministic draft scheduling; no model calls, database writes, or route lookups.

Inputs and outputs are ordinary dictionaries. All arithmetic uses UTC instants;
daily limits and wall-clock study/meal windows use the configured local timezone.
The greedy schedule is a proposal, not a proof that a different plan is impossible.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
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
        raise ValueError("时间必须是包含时区偏移量的 ISO 字符串")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间缺少时区偏移量")
    return parsed.astimezone(UTC)


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name}必须是大于等于 {minimum} 的整数")
    return value


def _clock(value: Any) -> time:
    parsed = time.fromisoformat(value)
    if parsed.tzinfo is not None:
        raise ValueError("每日时间应为本地 HH:MM，不包含时区")
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
        raise ValueError(f"{label}的结束时间必须晚于开始时间")
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
    warnings: list[str]


def _context(available_slots: list[dict], busy_events: list[dict], settings: dict) -> _Context:
    zone = ZoneInfo(settings.get("timezone", "Asia/Singapore"))
    study_start = _clock(settings.get("study_start", "08:00"))
    study_end = _clock(settings.get("study_end", "22:00"))
    if study_end <= study_start:
        raise ValueError("每日学习窗口必须在同一天内结束，且结束时间晚于开始时间")
    # These bounds come from this project's supplied schedule policy.
    if study_start < time(8) or study_end > time(22):
        raise ValueError("学习窗口必须位于 08:00—22:00 内")
    daily_limit = _integer(settings.get("daily_limit_minutes", 360), "每日学习上限")
    max_block = _integer(settings.get("max_block_minutes", 120), "单次学习上限", 1)
    break_minutes = _integer(settings.get("break_minutes", 30), "学习段之间的休息", 30)
    buffer_minutes = _integer(settings.get("buffer_minutes", 0), "建议提前量")
    if daily_limit > 360 or max_block > 120:
        raise ValueError("每日学习上限不可超过 360 分钟，单次学习不可超过 120 分钟")
    default_commute = _integer(
        settings.get("default_commute_minutes", 40), "默认单程通勤时长"
    )
    manual_commute = settings.get("manual_commute_minutes")
    if manual_commute is not None:
        manual_commute = _integer(manual_commute, "手动单程通勤时长")
    now = _instant(settings["now"]) if settings.get("now") is not None else None
    source_slots = _union([_interval(slot, "空闲时段") for slot in available_slots])
    meals = []
    for item in settings.get("meals", DEFAULT_MEALS):
        start, end = _clock(item["start"]), _clock(item["end"])
        if end <= start:
            raise ValueError("用餐时段的结束时间必须晚于开始时间")
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
        start, end = _interval(event, "已有日程")
        online = str(event.get("mode", "")).strip().lower() == "online"
        commute = event.get("commute_minutes")
        if online:
            commute = 0
        elif commute is not None:
            commute = _integer(commute, "日程单程通勤时长")
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
            f"{default_count} 段日程未提供通勤时长，已按前后各 {default_commute} 分钟预留；"
            "尚未查询真实路线。在线日程请明确标为 online。"
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
        warnings=warnings,
    )


def _task_info(task: dict, duplicates: set[str]) -> tuple[str, str, int, datetime | None, str]:
    task_id = str(task.get("id") or "")
    title = str(task.get("title") or "未命名任务")
    estimate = task.get("estimated_minutes")
    minutes = estimate if isinstance(estimate, int) and not isinstance(estimate, bool) else 0
    if not task_id or task_id in duplicates:
        return task_id, title, max(0, minutes), None, "任务编号缺失或重复，请先修正"
    if task.get("status") != "confirmed":
        return task_id, title, max(0, minutes), None, "任务尚未确认，不能安排"
    if minutes <= 0:
        return task_id, title, 0, None, "请填写大于零的预计耗时（整数分钟）"
    if not task.get("due_at"):
        return task_id, title, minutes, None, "截止时间缺失，请先补充并确认"
    try:
        due = _instant(task["due_at"])
    except (ValueError, TypeError):
        return task_id, title, minutes, None, "截止时间无效或缺少时区，请先修正"
    return task_id, title, minutes, due, ""


def _duplicate_ids(tasks: list[dict]) -> set[str]:
    counts = Counter(str(task.get("id") or "") for task in tasks)
    return {task_id for task_id, count in counts.items() if count > 1}


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
    task_infos = [_task_info(task, duplicates) for task in tasks]
    try:
        ctx = _context(available_slots, busy_events, settings)
    except (ValueError, TypeError, KeyError) as exc:
        reason = f"排程配置或日程无效：{exc}"
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
            reason = "截止时间已到或已过，请先调整任务"
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
    daily_used: dict = defaultdict(int)
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
            result["warnings"].append(f"“{title}”未达到建议提前量，但仍安排在实际截止时间之前。")
        if remaining:
            result["unscheduled"].append(
                {
                    "task_id": task_id,
                    "title": title,
                    "remaining_minutes": remaining,
                    "reason": (
                        "本次按截止时间排序的排程未找到足够时段；请调整空闲时间、"
                        "任务耗时或优先级后重试，不代表其他安排一定不可行"
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
        return [f"无法校验：排程配置或日程无效：{exc}"]
    issues = []
    duplicates = _duplicate_ids(tasks)
    task_map = {str(task.get("id") or ""): task for task in tasks}
    daily_seconds: dict = defaultdict(float)
    task_seconds: dict = defaultdict(float)
    intervals = []
    for index, block in enumerate(blocks, 1):
        label = f"第 {index} 个学习时段"
        try:
            start, end = _interval(block, label)
        except (ValueError, TypeError, KeyError) as exc:
            issues.append(f"{label}无效：{exc}")
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
            issues.append(f"{label}的分钟数与起止时间不符")
        if seconds > ctx.max_block * 60:
            issues.append(f"{label}超过单次学习上限")
        if not any(allowed_start <= start and end <= allowed_end for allowed_start, allowed_end in ctx.free):
            issues.append(f"{label}不在允许的空闲时间内，或与已有日程、用餐、通勤冲突")
        if ctx.now is not None and start < ctx.now:
            issues.append(f"{label}安排在当前时间之前")
        local_day = start.astimezone(ctx.zone).date()
        if (end - timedelta(microseconds=1)).astimezone(ctx.zone).date() != local_day:
            issues.append(f"{label}跨越本地日期，应拆分")
        daily_seconds[local_day] += seconds
        task_id = str(block.get("task_id") or "")
        task_seconds[task_id] += seconds
        task = task_map.get(task_id)
        if task is None:
            issues.append(f"{label}引用不存在的任务")
            continue
        _, _, _, due, reason = _task_info(task, duplicates)
        if reason:
            issues.append(f"{label}对应的任务不可安排：{reason}")
        elif end > due:
            issues.append(f"{label}晚于任务截止时间")
    for day, seconds in daily_seconds.items():
        if seconds > ctx.daily_limit * 60:
            issues.append(f"{day.isoformat()} 的学习时长超过每日上限")
    for task_id, seconds in task_seconds.items():
        task = task_map.get(task_id)
        if task:
            _, _, minutes, _, reason = _task_info(task, duplicates)
            if not reason and seconds > minutes * 60:
                issues.append(f"任务 {task_id} 的总安排时长超过预计耗时")
    # Keep the farthest end encountered, so a nested overlapping block cannot
    # conceal a later collision with an earlier, longer block.
    latest_end = None
    latest_index = None
    for start, end, index in sorted(intervals):
        if latest_end is not None:
            if start < latest_end:
                issues.append(f"第 {latest_index} 和第 {index} 个学习时段重叠")
            elif start < latest_end + timedelta(minutes=ctx.break_minutes):
                issues.append(f"第 {latest_index} 和第 {index} 个学习时段之间休息不足")
        if latest_end is None or end > latest_end:
            latest_end, latest_index = end, index
    return issues
