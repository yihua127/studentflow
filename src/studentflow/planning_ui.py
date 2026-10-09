"""Personal study planning controls for the StudentFlow interface."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
import tempfile
from copy import deepcopy
from datetime import datetime, time
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import streamlit as st

from studentflow.planner import DEFAULT_MEALS, propose_schedule, validate_schedule

ZONE = ZoneInfo("Asia/Singapore")


def default_preferences() -> dict:
    """Start with no assumed personal availability or commitments."""
    return {
        "available_slots": [],
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
    return value


def write_preferences(path: Path, preferences: dict) -> None:
    """Replace one local preference file only after an explicit UI action."""
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


def planning_signature(tasks: list[dict], preferences: dict, now: str) -> str:
    payload = {"tasks": tasks, "preferences": preferences, "now": now}
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
    slots = preferences["available_slots"]
    st.markdown("**Available study time**")
    if slots:
        _period_table(slots)
    else:
        st.info("Add a time window when you are available to study.")
    with (
        st.expander("Add available time", expanded=not slots),
        st.form("sf_plan_add_availability"),
    ):
        day = st.date_input("Date", value=datetime.now(ZONE).date(), key="sf_plan_slot_day")
        left, right = st.columns(2)
        start = left.time_input("Start", value=time(9), key="sf_plan_slot_start")
        end = right.time_input("End", value=time(11), key="sf_plan_slot_end")
        if st.form_submit_button("Add available time"):
            try:
                entry = window(day, start, end)
                updated = deepcopy(preferences)
                updated["available_slots"].append(entry)
                _save(path, updated, "Available time added.")
            except ValueError as exc:
                st.error(str(exc))
    if slots:
        with st.expander("Remove available time"):
            chosen = st.selectbox(
                "Time window",
                range(len(slots)),
                format_func=lambda index: _period_label(slots[index]),
                key="sf_plan_remove_slot",
            )
            if st.button("Remove selected time", key="sf_plan_remove_slot_button"):
                updated = deepcopy(preferences)
                updated["available_slots"].pop(chosen)
                _save(path, updated, "Available time removed.")


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
        st.caption("Meals are reserved at 08:00–08:30, 11:30–12:00 and 17:30–18:00.")
        if st.form_submit_button("Save preferences"):
            if not (time(8) <= start < end <= time(22)):
                st.error(
                    "Choose study times between 08:00 and 22:00, with the end after the start."
                )
            else:
                updated = deepcopy(preferences)
                updated["settings"].update(
                    {
                        "study_start": start.isoformat(timespec="minutes"),
                        "study_end": end.isoformat(timespec="minutes"),
                        "daily_limit_minutes": int(daily),
                        "max_block_minutes": int(block),
                        "break_minutes": int(rest),
                        "default_commute_minutes": int(commute),
                    }
                )
                _save(path, updated, "Study preferences saved.")


def render_planner(tasks: list[dict], store, data_dir: Path) -> None:
    """Render personal schedule inputs and a separately confirmed local draft."""
    st.subheader("Study Plan")
    st.caption(
        "Plan confirmed tasks around your availability and commitments. All times are Singapore time."
    )
    if notice := st.session_state.pop("sf_plan_notice", None):
        st.success(notice)
    path = Path(data_dir) / "planning_preferences.json"
    try:
        preferences = read_preferences(path)
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

    st.markdown("**Plan your tasks**")
    if not tasks:
        st.info("Save a task from an email before creating a study plan.")
    else:
        ready = sum(task.get("status") == "confirmed" for task in tasks)
        st.caption(
            f"{ready} confirmed task(s). Tasks with missing details will be listed for review."
        )
    now = datetime.now(ZONE).replace(second=0, microsecond=0).isoformat()
    settings = preferences["settings"] | {"now": now}
    slots, events = preferences["available_slots"], preferences["busy_events"]
    signature = planning_signature(tasks, preferences, now)
    if st.button("Generate study plan", disabled=not tasks or not slots, type="primary"):
        result = propose_schedule(tasks, slots, events, settings)
        errors = validate_schedule(result["blocks"], tasks, slots, events, settings)
        st.session_state["planner_draft"] = {
            "signature": signature,
            "result": result,
            "validation_errors": errors,
            "inputs": {"tasks": tasks, "slots": slots, "busy": events, "settings": settings},
        }
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
            st.success("Plan saved on this device.")
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
