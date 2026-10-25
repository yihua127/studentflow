"""Personal study planning controls for the StudentFlow interface."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
from copy import deepcopy
from datetime import datetime, time, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import streamlit as st

from studentflow.availability import (
    DEFAULT_HORIZON_DAYS,
    normalize_availability,
    prune_expired_availability,
    resolve_availability,
)
from studentflow.availability_ui import render_availability
from studentflow.planner import (
    DEFAULT_MEALS,
    PLANNING_POLICY_VERSION,
    is_completed,
    propose_schedule,
    validate_schedule,
)

ZONE = ZoneInfo("Asia/Singapore")


def default_preferences() -> dict:
    """Start with no assumed personal availability or commitments."""
    return {
        "available_slots": [],
        "weekly_availability": [],
        "availability_overrides": [],
        "availability_horizon_days": DEFAULT_HORIZON_DAYS,
        "busy_events": [],
        "settings": {
            "timezone": "Asia/Singapore",
            "study_start": "08:00",
            "study_end": "22:00",
            "daily_limit_minutes": 360,
            "default_commute_minutes": 40,
            "max_block_minutes": 120,
            "break_minutes": 30,
            "meals": deepcopy(DEFAULT_MEALS),
        },
    }


def window(day, start: time, end: time) -> dict:
    if end <= start:
        raise ValueError("End time must be later than start time on the same day.")
    return {
        "id": uuid4().hex,
        "start": datetime.combine(day, start, ZONE).isoformat(),
        "end": datetime.combine(day, end, ZONE).isoformat(),
    }


def read_preferences(path: Path) -> dict:
    if not path.exists():
        return default_preferences()
    if path.stat().st_size > 1_000_000:
        raise ValueError("Saved planning preferences could not be loaded.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("Saved planning preferences could not be loaded.")
    settings = value.get("settings")
    if not isinstance(settings, dict):
        raise TypeError("Saved planning preferences could not be loaded.")
    for field in ("available_slots", "busy_events"):
        if not isinstance(value.get(field), list):
            raise TypeError("Saved planning preferences could not be loaded.")
        for entry in value[field]:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                raise TypeError("Saved planning preferences could not be loaded.")
            start = datetime.fromisoformat(entry["start"])
            end = datetime.fromisoformat(entry["end"])
            if start.tzinfo is None or end.tzinfo is None or end <= start:
                raise ValueError("Saved planning preferences could not be loaded.")
    defaults = default_preferences()["settings"]
    settings = defaults | settings
    settings["timezone"] = "Asia/Singapore"
    settings.pop("now", None)
    value["settings"] = settings
    # This independent validator also checks rule ranges and time formats.
    errors = validate_schedule([], [], value["available_slots"], value["busy_events"], settings)
    if errors:
        raise ValueError("Saved planning preferences could not be loaded.")
    return normalize_availability(value)


def write_preferences(path: Path, preferences: dict) -> None:
    """Atomically replace preferences after a UI change or expired-date cleanup."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix="planning-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(preferences, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def parse_meal_breaks(value: str) -> list[dict]:
    """Accept editable daily meal windows; an empty field reserves no meals."""
    meals = []
    for line in value.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"\s*(\d{2}:\d{2})\s*[-–—]\s*(\d{2}:\d{2})\s*", line)
        if not match:
            raise ValueError("Enter each meal break as HH:MM-HH:MM, one per line.")
        start, end = match.groups()
        try:
            start_time, end_time = time.fromisoformat(start), time.fromisoformat(end)
        except ValueError as exc:
            raise ValueError("Meal times must be valid 24-hour times, for example 12:15-13:00.") from exc
        if end_time <= start_time:
            raise ValueError("Each meal break must end after it starts on the same day.")
        meals.append({"start": start, "end": end})
    return meals


def make_draft(tasks: list[dict], preferences: dict, now: str, completed_by_day: dict) -> dict:
    settings = preferences["settings"] | {"now": now, "completed_by_day": completed_by_day}
    slots, events = resolve_availability(preferences, now), preferences["busy_events"]
    result = propose_schedule(tasks, slots, events, settings)
    return {
        "signature": planning_signature(tasks, preferences | {"completed_by_day": completed_by_day}, now),
        "result": result,
        "validation_errors": validate_schedule(result["blocks"], tasks, slots, events, settings),
        "inputs": {"tasks": tasks, "slots": slots, "busy": events, "settings": settings},
    }


def planning_signature(tasks: list[dict], preferences: dict, now: str) -> str:
    payload = {"tasks": tasks, "preferences": preferences, "now": now,
               "planning_policy": PLANNING_POLICY_VERSION}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def schedule_rows(blocks: list[dict]) -> list[dict]:
    rows = []
    for block in blocks:
        start = datetime.fromisoformat(block["start"]).astimezone(ZONE)
        end = datetime.fromisoformat(block["end"]).astimezone(ZONE)
        rows.append(
            {
                "Task": block["title"],
                "Date": start.strftime("%d %b %Y"),
                "Start": start.strftime("%H:%M"),
                "End": end.strftime("%H:%M"),
                "Duration (min)": block["minutes"],
            }
        )
    return rows


def schedule_csv(blocks: list[dict]) -> str:
    buffer = io.StringIO()
    fields = ["Task", "Date", "Start", "End", "Duration (min)"]
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    for row in schedule_rows(blocks):
        # Task titles originate in email and must not become spreadsheet formulas.
        if str(row["Task"]).lstrip().startswith(("=", "+", "-", "@")):
            row["Task"] = "'" + str(row["Task"])
        writer.writerow(row)
    return buffer.getvalue()


def _period_label(entry: dict) -> str:
    start = datetime.fromisoformat(entry["start"]).astimezone(ZONE)
    end = datetime.fromisoformat(entry["end"]).astimezone(ZONE)
    prefix = f"{entry['title']} · " if entry.get("title") else ""
    return f"{prefix}{start:%d %b %Y, %H:%M}–{end:%H:%M}"


def _save(path: Path, preferences: dict, message: str) -> None:
    try:
        write_preferences(path, preferences)
    except OSError:
        st.error("Changes could not be saved. Check that the application can save local data.")
        return
    st.session_state["sf_plan_notice"] = message
    st.rerun()


def _period_table(entries: list[dict], commitments: bool = False) -> None:
    rows = []
    for entry in entries:
        start = datetime.fromisoformat(entry["start"]).astimezone(ZONE)
        end = datetime.fromisoformat(entry["end"]).astimezone(ZONE)
        row = {
            "Date": start.strftime("%d %b %Y"),
            "Start": start.strftime("%H:%M"),
            "End": end.strftime("%H:%M"),
        }
        if commitments:
            row = {
                "Commitment": entry["title"],
                **row,
                "Type": "Online" if entry.get("mode") == "online" else "In person",
                "Travel each way (min)": entry.get("commute_minutes", 0),
            }
        rows.append(row)
    st.dataframe(rows, hide_index=True, width="stretch")


def _availability(preferences: dict, path: Path) -> None:
    render_availability(preferences, path, _save, datetime.now(ZONE))


def _commitments(preferences: dict, path: Path) -> None:
    events = preferences["busy_events"]
    st.markdown("**Fixed commitments**")
    if events:
        _period_table(events, commitments=True)
    else:
        st.caption("Add classes, meetings or other commitments to keep that time free.")
    with st.expander("Add a commitment"), st.form("sf_plan_add_commitment"):
        title = st.text_input("Title", placeholder="Class or meeting", max_chars=200)
        day = st.date_input("Date", value=datetime.now(ZONE).date(), key="sf_plan_event_day")
        left, right = st.columns(2)
        start = left.time_input("Start", value=time(13), key="sf_plan_event_start")
        end = right.time_input("End", value=time(14), key="sf_plan_event_end")
        online = st.checkbox("Online (no travel time)", value=True)
        commute = st.number_input(
            "Travel time each way for an in-person commitment (min)",
            min_value=0,
            max_value=240,
            value=preferences["settings"]["default_commute_minutes"],
            step=5,
        )
        if st.form_submit_button("Add commitment"):
            try:
                if not title.strip():
                    raise ValueError("Enter a title for this commitment.")
                entry = window(day, start, end)
                entry.update(
                    {
                        "title": title.strip(),
                        "mode": "online" if online else "onsite",
                        "commute_minutes": 0 if online else int(commute),
                        # Unknown places cannot safely be merged into one trip.
                        "location": entry["id"],
                    }
                )
                updated = deepcopy(preferences)
                updated["busy_events"].append(entry)
                _save(path, updated, "Commitment added. Generate a new plan to include it.")
            except ValueError as exc:
                st.error(str(exc))
    if events:
        with st.expander("Remove a commitment"):
            chosen = st.selectbox(
                "Commitment",
                range(len(events)),
                format_func=lambda index: _period_label(events[index]),
                key="sf_plan_remove_event",
            )
            if st.button("Remove selected commitment", key="sf_plan_remove_event_button"):
                updated = deepcopy(preferences)
                updated["busy_events"].pop(chosen)
                _save(path, updated, "Commitment removed.")


def _settings(preferences: dict, path: Path) -> None:
    settings = preferences["settings"]
    with st.expander("Study preferences"), st.form("sf_plan_settings"):
        left, right = st.columns(2)
        start = left.time_input(
            "Earliest study time", time.fromisoformat(settings["study_start"])
        )
        end = right.time_input("Latest study time", time.fromisoformat(settings["study_end"]))
        daily = st.number_input(
            "Daily study limit (min)",
            min_value=30,
            max_value=360,
            value=settings["daily_limit_minutes"],
            step=30,
        )
        block = st.number_input(
            "Maximum study session (min)",
            min_value=15,
            max_value=120,
            value=settings["max_block_minutes"],
            step=15,
        )
        rest = st.number_input(
            "Break between study sessions (min)",
            min_value=30,
            max_value=180,
            value=settings["break_minutes"],
            step=5,
        )
        commute = st.number_input(
            "Default travel time each way (min)",
            min_value=0,
            max_value=240,
            value=settings["default_commute_minutes"],
            step=5,
        )
        meals_text = st.text_area(
            "Meal breaks (HH:MM-HH:MM, one per line)",
            value="\n".join(f"{meal['start']}-{meal['end']}" for meal in settings["meals"]),
            help="Add, edit or remove any line. Leave empty if you do not want reserved meal breaks.",
            key="sf_plan_meals",
        )
        st.caption("Meal times are editable. Save changes, then generate and save a new plan to apply them.")
        if st.form_submit_button("Save preferences"):
            if not (time(8) <= start < end <= time(22)):
                st.error(
                    "Choose study times between 08:00 and 22:00, with the end after the start."
                )
            else:
                try:
                    meals = parse_meal_breaks(meals_text)
                except ValueError as exc:
                    st.error(str(exc))
                    return
                updated = deepcopy(preferences)
                updated["settings"].update(
                    {
                        "study_start": start.isoformat(timespec="minutes"),
                        "study_end": end.isoformat(timespec="minutes"),
                        "daily_limit_minutes": int(daily),
                        "max_block_minutes": int(block),
                        "break_minutes": int(rest),
                        "default_commute_minutes": int(commute),
                        "meals": meals,
                    }
                )
                _save(path, updated, "Study preferences saved. Generate and save a new plan to apply the changes.")


def _saved_plan(store) -> None:
    plan = store.latest_plan()
    if not plan:
        return
    st.markdown("**Saved study plan**")
    st.caption(
        "This plan is saved on this device. Daily progress appears at the study end time "
        "while the app is open, or on your next visit if it was closed."
    )
    blocks = plan.get("result", {}).get("blocks", [])
    if blocks:
        st.dataframe(schedule_rows(blocks), hide_index=True, width="stretch")
        st.download_button(
            "Download saved schedule (CSV)", schedule_csv(blocks),
            "studentflow_saved_schedule.csv", "text/csv", key="sf_saved_plan_download",
        )
    reviews = store.daily_reviews()
    if reviews:
        with st.expander("Recorded daily progress"):
            st.dataframe([
                {"Date": review["date"], "Completed (min)": sum(review["minutes"].values())}
                for review in reviews
            ], hide_index=True, width="stretch")


def render_planner(tasks: list[dict], store, data_dir: Path, *, root: Path | None = None) -> None:
    """Render personal schedule inputs and a separately confirmed local draft."""
    st.subheader("Study Plan")
    st.caption(
        "Plan confirmed tasks around your availability and commitments. All times are Singapore time."
    )
    if notice := st.session_state.pop("sf_plan_notice", None):
        st.success(notice)
    _saved_plan(store)
    path = Path(data_dir) / "planning_preferences.json"
    try:
        preferences = read_preferences(path)
        preferences, removed = prune_expired_availability(preferences, datetime.now(ZONE))
        if removed:
            # Only availability records are pruned; saved plans and daily progress
            # are separate historical records and are intentionally untouched.
            try:
                write_preferences(path, preferences)
            except OSError:
                st.warning("Expired availability is hidden, but cleanup could not be saved. Check local file access.")
    except (ValueError, TypeError, KeyError, OSError):
        st.error(
            "Saved planning preferences could not be loaded. Restore your settings to continue."
        )
        if st.button("Reset planning preferences", key="sf_plan_reset"):
            _save(path, default_preferences(), "Planning preferences reset.")
        return
    _settings(preferences, path)
    left, right = st.columns(2)
    with left:
        _availability(preferences, path)
    with right:
        _commitments(preferences, path)

    from studentflow.fixed_events_ui import render_fixed_events
    from studentflow.scheduling_context import (
        saved_plan_is_fresh,
        source_fingerprint,
        with_fixed_events,
    )

    fixed_events = store.list_fixed_events()
    accounts = sorted({event["account"] for event in fixed_events})
    connected = str(st.session_state.get("gmail_profile", {}).get("email_address") or "").casefold()
    if connected and connected not in accounts:
        accounts.append(connected)
    if accounts:
        account = accounts[0] if len(accounts) == 1 else st.selectbox("Fixed-event account", accounts)
        render_fixed_events(store, account=account)

    if root is not None:
        from studentflow.calendar_sync_ui import render_calendar_sync

        def current_fingerprint():
            fingerprint = source_fingerprint(
                store.tasks(), read_preferences(path), store.list_fixed_events(), store.progress_by_day(),
            )
            return fingerprint + ":" + str((store.latest_plan() or {}).get("id", ""))

        def is_fresh(plan):
            return saved_plan_is_fresh(
                plan, store.tasks(), read_preferences(path), store.list_fixed_events(),
                store.progress_by_day(), datetime.now(ZONE).isoformat(),
            )

        render_calendar_sync(
            store, root, data_dir, fixed_events=fixed_events,
            current_fingerprint=current_fingerprint, saved_plan_is_fresh=is_fresh,
        )
    # Confirmed appointments block the planner but are never written into the
    # user's manually edited preference file or counted as study effort.
    preferences = with_fixed_events(preferences, fixed_events)

    st.markdown("**Plan your tasks**")
    if not tasks:
        st.info("Save a task from an email before creating a study plan.")
    else:
        ready = sum(task.get("status") == "confirmed" and not is_completed(task) for task in tasks)
        st.caption(
            f"{ready} confirmed unfinished task(s). Remaining study time is scheduled, with earlier deadlines first. "
            "Today's elapsed time is skipped; future days start from the earliest available time after meals and commitments. "
            "Sessions prefer whole-hour starts, using other times when needed to fit work before deadlines. "
            "Tasks with missing details will be listed for review."
        )
    # Start at the next whole minute so saving a draft never schedules in the past.
    now = (datetime.now(ZONE) + timedelta(minutes=1)).replace(second=0, microsecond=0).isoformat()
    progress = store.progress_by_day()
    slots = resolve_availability(preferences, now)
    signature = planning_signature(tasks, preferences | {"completed_by_day": progress}, now)
    generate = st.button("Generate study plan", disabled=not tasks or not slots, type="primary")
    replan = st.button("Replan remaining tasks", disabled=not tasks or not slots, key="sf_plan_replan")
    requested = st.session_state.pop("sf_replan_requested", False)
    if generate or replan or requested:
        st.session_state["planner_draft"] = make_draft(tasks, preferences, now, progress)
        if requested:
            st.info("A new draft is ready below. Open Study Plan to review it and choose Save plan.")
    draft = st.session_state.get("planner_draft")
    if not draft:
        return
    if draft["signature"] != signature:
        st.info("Tasks, availability or time have changed. Generate a fresh plan before saving.")
        return
    result = draft["result"]
    st.markdown("**Proposed schedule**")
    for warning in dict.fromkeys(result["warnings"]):
        st.warning(warning)
    if draft["validation_errors"]:
        st.error("This plan could not pass the schedule checks. Adjust your inputs and try again.")
    if result["blocks"]:
        st.dataframe(schedule_rows(result["blocks"]), hide_index=True, width="stretch")
    else:
        st.info("No study sessions could be scheduled. Review the items below.")
    if result["unscheduled"]:
        st.warning("Some work remains unscheduled. Review these tasks before accepting the plan.")
        st.dataframe(
            [
                {
                    "Task": item["title"],
                    "Remaining (min)": item["remaining_minutes"],
                    "Reason": item["reason"],
                }
                for item in result["unscheduled"]
            ],
            hide_index=True,
            width="stretch",
        )
    confirmed = st.checkbox(
        "I have reviewed the schedule and any unscheduled work.",
        key="sf_plan_confirm_" + signature,
    )
    if st.button(
        "Save plan",
        disabled=not confirmed or bool(draft["validation_errors"]) or not result["blocks"],
    ):
        try:
            store.save_plan(draft)
            st.session_state.pop("planner_draft", None)
            st.session_state["sf_plan_notice"] = "Plan saved on this device. Daily progress will use this schedule."
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
        except (OSError, sqlite3.Error):
            st.error("The plan could not be saved. Please try again.")
    if result["blocks"]:
        st.download_button(
            "Download schedule (CSV)",
            schedule_csv(result["blocks"]),
            "studentflow_schedule.csv",
            "text/csv",
            key="sf_plan_download_" + signature,
        )
