"""Saved-plan daily check-ins, including overdue prompts after the app reopens."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("A saved plan time must include a timezone.")
    return parsed.astimezone(UTC)


def _activation(plan: dict) -> datetime:
    value = plan.get("saved_at") or plan.get("inputs", {}).get("settings", {}).get("now")
    if value:
        # The planner offers whole-minute sessions. Saving in the same minute
        # must not discard the opening minute of the first offered session.
        return _instant(value).replace(second=0, microsecond=0)
    blocks = plan.get("result", {}).get("blocks", [])
    return min((_instant(block["start"]) for block in blocks), default=datetime.min.replace(tzinfo=UTC))


def planned_days(plans: list[dict]) -> dict[str, dict]:
    """Retain elapsed sessions; a saved replacement supersedes future sessions.

    All plan versions are needed so an overdue day survives later replanning.
    Daily check-ins are keyed by date, never by draft/plan version.
    """
    ordered = sorted(plans, key=_activation)
    days: dict[str, dict] = {}
    for index, plan in enumerate(ordered):
        activation = _activation(plan)
        until = _activation(ordered[index + 1]) if index + 1 < len(ordered) else None
        settings = plan.get("inputs", {}).get("settings", {})
        zone_name = settings.get("timezone", "Asia/Singapore")
        zone = ZoneInfo(zone_name)
        study_end = settings.get("study_end", "22:00")
        for block in plan.get("result", {}).get("blocks", []):
            start = max(_instant(block["start"]), activation)
            end = _instant(block["end"])
            if until is not None:
                end = min(end, until)
            minutes = int((end - start).total_seconds() // 60)
            if minutes <= 0:
                continue
            day = start.astimezone(zone).date()
            key = day.isoformat()
            entry = days.setdefault(key, {"date": key, "minutes": {}, "titles": {}})
            entry.update({
                "plan_id": plan["id"],
                "study_end": study_end,
                "timezone": zone_name,
                "due_at": datetime.combine(day, time.fromisoformat(study_end), zone).isoformat(),
            })
            task_id = str(block["task_id"])
            entry["minutes"][task_id] = entry["minutes"].get(task_id, 0) + minutes
            entry["titles"][task_id] = block.get("title", "Untitled task")
    return dict(sorted(days.items()))


def due_daily_reviews(plans: list[dict], reviews: list[dict], now: datetime) -> list[dict]:
    """Return every unreviewed planned day whose configured study end has arrived."""
    if now.tzinfo is None:
        raise ValueError("The current time must include a timezone.")
    reviewed = {review["date"] for review in reviews}
    return [
        day for key, day in planned_days(plans).items()
        if key not in reviewed and _instant(day["due_at"]) <= now.astimezone(UTC)
    ]


def planned_daily_minutes(plans: list[dict], day: str) -> dict[str, int]:
    """Convenience for storage validation and reporting."""
    return planned_days(plans).get(day, {}).get("minutes", {})


@st.fragment(run_every="60s")
def render_daily_review(store, data_dir: Path) -> None:
    """Check while the page is open; show missed check-ins on the next visit."""
    try:
        due = due_daily_reviews(store.plans(), store.daily_reviews(), datetime.now(UTC))
    except (ValueError, KeyError, sqlite3.Error):
        st.error("Daily progress could not be loaded. Please reopen the app and try again.")
        return
    if notice := st.session_state.get("sf_daily_review_notice"):
        st.success(notice)
        st.caption("You can now replan your remaining work, with earlier deadlines first.")
        if st.button("Replan remaining tasks", key="sf_checkin_replan"):
            st.session_state["sf_replan_requested"] = True
            st.session_state.pop("sf_daily_review_notice", None)
            st.rerun()
    if not due:
        return
    task_map = {task["id"]: task for task in store.tasks()}
    st.subheader("Daily progress")
    st.caption(
        "Your planned study end time has arrived. Record what you completed to update "
        "remaining study time. Missed check-ins are kept for your next visit."
    )
    for day in due:
        key = day["date"]
        st.markdown(f"**{key} · study ends at {day['study_end']}**")
        st.dataframe([
            {"Task": day["titles"][task_id], "Planned (min)": minutes}
            for task_id, minutes in day["minutes"].items()
        ], hide_index=True, width="stretch")
        answer = st.radio(
            "Did you finish all the study time planned for this day?",
            ["Yes, all planned study time", "No, enter completed minutes"],
            index=None,
            key=f"sf_checkin_answer_{key}",
        )
        minutes = {}
        if answer == "Yes, all planned study time":
            minutes = dict(day["minutes"])
            st.caption("This records the minutes in the plan. A task is completed when its remaining time reaches zero.")
        elif answer:
            st.caption("Enter the minutes actually completed for each task. Leave 0 for tasks you did not study.")
            for task_id, planned in day["minutes"].items():
                task = task_map.get(task_id, {})
                remaining = max(0, (task.get("estimated_minutes") or 0) - task.get("completed_minutes", 0))
                minutes[task_id] = int(st.number_input(
                    f"{day['titles'][task_id]} — completed minutes (planned: {planned})",
                    min_value=0,
                    max_value=max(remaining, 1),
                    value=0,
                    step=5,
                    disabled=remaining == 0,
                    key=f"sf_checkin_minutes_{key}_{task_id}",
                )) if remaining else 0
        if st.button("Save daily progress", key=f"sf_checkin_save_{key}", disabled=answer is None):
            try:
                saved = store.record_daily_review(key, day["plan_id"], minutes)
            except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
                st.error(f"Progress could not be saved: {exc}")
            else:
                total = sum(saved["minutes"].values())
                st.session_state["sf_daily_review_notice"] = (
                    f"Progress saved for {key}: {total} minute(s). "
                    "Tasks with no remaining study time are now in Completed."
                )
                st.session_state.pop("planner_draft", None)
                st.rerun()
