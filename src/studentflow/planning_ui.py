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
from html import escape
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import streamlit as st

from studentflow.page_heading import render_page_heading

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


def preserve_planning_inputs() -> None:
    """Keep ordinary received input values across sidebar navigation, never approvals.

    Streamlit forms send their values on submission; values that have not left the
    browser are not covered by this helper. Saved preferences remain authoritative.
    """
    keys = {
        "sf_plan_meals", "sf_plan_pref_start", "sf_plan_pref_end", "sf_plan_pref_daily",
        "sf_plan_pref_block", "sf_plan_pref_rest", "sf_plan_pref_commute",
        "sf_plan_event_title", "sf_plan_event_day", "sf_plan_event_start",
        "sf_plan_event_end", "sf_plan_event_online", "sf_plan_event_commute",
        "sf_plan_weekdays", "sf_plan_weekly_start", "sf_plan_weekly_end",
        "sf_plan_edit_weekly", "sf_plan_override_kind", "sf_plan_slot_day",
        "sf_plan_slot_start", "sf_plan_slot_end", "sf_plan_horizon_days",
        "sf_plan_view_saved", "sf_plan_view_draft",
    }
    prefixes = ("sf_plan_edit_days_", "sf_plan_edit_start_", "sf_plan_edit_end_")
    for key in list(st.session_state):
        if key in keys or key.startswith(prefixes):
            st.session_state[key] = st.session_state[key]


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
        title = st.text_input("Title", placeholder="Class or meeting", max_chars=200,
                              key="sf_plan_event_title")
        day = st.date_input("Date", value=datetime.now(ZONE).date(), key="sf_plan_event_day")
        left, right = st.columns(2)
        start = left.time_input("Start", value=time(13), key="sf_plan_event_start")
        end = right.time_input("End", value=time(14), key="sf_plan_event_end")
        online = st.checkbox("Online (no travel time)", value=True, key="sf_plan_event_online")
        commute = st.number_input(
            "Travel time each way for an in-person commitment (min)",
            min_value=0,
            max_value=240,
            value=preferences["settings"]["default_commute_minutes"],
            step=5,
            key="sf_plan_event_commute",
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
            "Earliest study time", time.fromisoformat(settings["study_start"]),
            key="sf_plan_pref_start",
        )
        end = right.time_input("Latest study time", time.fromisoformat(settings["study_end"]),
                               key="sf_plan_pref_end")
        daily = left.number_input(
            "Daily study limit (min)",
            min_value=30,
            max_value=360,
            value=settings["daily_limit_minutes"],
            step=30,
            key="sf_plan_pref_daily",
        )
        block = right.number_input(
            "Maximum study session (min)",
            min_value=15,
            max_value=120,
            value=settings["max_block_minutes"],
            step=15,
            key="sf_plan_pref_block",
        )
        rest = left.number_input(
            "Break between study sessions (min)",
            min_value=30,
            max_value=180,
            value=settings["break_minutes"],
            step=5,
            key="sf_plan_pref_rest",
        )
        commute = right.number_input(
            "Default travel time each way (min)",
            min_value=0,
            max_value=240,
            value=settings["default_commute_minutes"],
            step=5,
            key="sf_plan_pref_commute",
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


def _duration(minutes: int) -> str:
    hours, remainder = divmod(int(minutes), 60)
    return f"{hours}h {remainder:02d}m" if hours else f"{remainder}m"


def _agenda_html(blocks: list[dict]) -> str:
    """A readable schedule using saved/planned facts; source titles remain plain text."""
    days: dict[str, list[tuple[dict, datetime, datetime]]] = {}
    for block in sorted(blocks, key=lambda item: item["start"]):
        start = datetime.fromisoformat(block["start"]).astimezone(ZONE)
        end = datetime.fromisoformat(block["end"]).astimezone(ZONE)
        days.setdefault(start.date().isoformat(), []).append((block, start, end))
    sections = []
    for entries in days.values():
        total = sum(entry[0]["minutes"] for entry in entries)
        day = entries[0][1]
        rows = []
        for block, start, end in entries:
            rows.append(
                '<div class="sf-agenda-row">'
                f'<div class="sf-agenda-time">{start:%H:%M}<span>{end:%H:%M}</span></div>'
                '<div class="sf-agenda-content">'
                f'<div class="sf-agenda-title">{escape(str(block["title"]))}</div>'
                f'<div class="sf-agenda-meta">{_duration(block["minutes"])} · Study session</div>'
                '</div></div>'
            )
        sections.append(
            '<section class="sf-agenda-day">'
            f'<div class="sf-agenda-heading"><strong>{day:%A, %d %b %Y}</strong>'
            f'<span>{_duration(total)}</span></div>' + ''.join(rows) + '</section>'
        )
    return '<div class="sf-agenda">' + ''.join(sections) + '</div>'


def _schedule_display(blocks: list[dict], *, key: str) -> None:
    view = st.radio("Schedule view", ["Agenda", "Table"], horizontal=True,
                    label_visibility="collapsed", key="sf_plan_view_" + key)
    if view == "Table":
        st.dataframe(schedule_rows(blocks), hide_index=True, width="stretch")
    else:
        with st.container(height=430, border=False, key="sf_plan_agenda_" + key):
            st.html(_agenda_html(blocks))


def _saved_plan(store, *, fresh: bool = True) -> None:
    plan = store.latest_plan()
    if not plan:
        return
    with st.container(border=True, key="sf_panel_plan_saved"):
        st.markdown("**Saved study plan**")
        if not fresh:
            st.warning("Tasks or planning settings have changed. Generate and save a new plan before syncing study sessions.")
        blocks = plan.get("result", {}).get("blocks", [])
        st.caption(f"{len(blocks)} saved session(s) · "
                   f"{_duration(sum(block['minutes'] for block in blocks))} scheduled · Singapore time")
        if blocks:
            _schedule_display(blocks, key="saved")
            st.download_button(
                "Download saved schedule (CSV)", schedule_csv(blocks),
                "studentflow_saved_schedule.csv", "text/csv", key="sf_saved_plan_download",
            )
        st.caption("Daily progress appears at the study end time while the app is open, "
                   "or on your next visit if it was closed.")
        reviews = store.daily_reviews()
        if reviews:
            with st.expander("Recorded daily progress"):
                st.dataframe([
                    {"Date": review["date"], "Completed (min)": sum(review["minutes"].values())}
                    for review in reviews
                ], hide_index=True, width="stretch")


def render_planner(tasks: list[dict], store, data_dir: Path, *, root: Path | None = None) -> None:
    """Render personal schedule inputs and a separately confirmed local draft."""
    render_page_heading(
        "MAKE ROOM TO STUDY",
        "Study plan",
        "Plan confirmed tasks around your availability and commitments. All times are Singapore time.",
    )
    if notice := st.session_state.pop("sf_plan_notice", None):
        st.success(notice)
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
            for key in list(st.session_state):
                if key.startswith("sf_plan_pref_") or key == "sf_plan_meals":
                    st.session_state.pop(key, None)
            _save(path, default_preferences(), "Planning preferences reset.")
        # A damaged settings file must not hide the last saved schedule/export.
        _saved_plan(store)
        return
    from studentflow.fixed_events_ui import render_fixed_events
    from studentflow.scheduling_context import (
        saved_plan_is_fresh,
        source_fingerprint,
        with_fixed_events,
    )

    fixed_events = store.list_fixed_events()
    saved = store.latest_plan()
    ready = [task for task in tasks if task.get("status") == "confirmed" and not is_completed(task)]
    remaining = sum(max(0, (task.get("estimated_minutes") or 0) -
                        (task.get("completed_minutes") or 0)) for task in ready)
    with st.container(key="sf_plan_metrics"):
        a, b = st.columns(2)
        a.metric("Ready to plan", len(ready))
        b.metric("Remaining study", _duration(remaining))
        c, d = st.columns(2)
        c.metric("Saved sessions", len((saved or {}).get("result", {}).get("blocks", [])))
        d.metric("Fixed commitments", len(preferences["busy_events"]) + len(fixed_events))
    schedule, availability, commitments = st.tabs(
        ["Schedule", "Availability & preferences", "Commitments & calendar"], key="sf_plan_tabs"
    )
    with schedule:
        with st.container(border=True, key="sf_panel_plan_builder"):
            _build_plan(tasks, store, with_fixed_events(preferences, fixed_events))
        if saved:
            fresh = saved_plan_is_fresh(
                saved, tasks, preferences, fixed_events,
                store.progress_by_day(), datetime.now(ZONE).isoformat(),
            )
            _saved_plan(store, fresh=fresh)
        elif not st.session_state.get("planner_draft"):
            st.html('<div class="sf-empty-state"><strong>Your next study session starts here.</strong>'
                    '<p>Set your available time, generate a plan, then review and save it.</p></div>')

    with availability:
        with st.container(border=True, key="sf_panel_plan_preferences"):
            settings = preferences["settings"]
            rhythm = (
                f"{settings['study_start']}–{settings['study_end']} · "
                f"Up to {_duration(settings['daily_limit_minutes'])} a day · "
                f"{settings['break_minutes']} min between sessions"
            )
            st.html(
                '<div class="sf-plan-builder-heading"><strong>Your study rhythm</strong>'
                f'<span>{escape(rhythm)}</span></div>'
            )
            _settings(preferences, path)
        with st.container(border=True, key="sf_panel_plan_availability"):
            st.html(
                '<div class="sf-plan-builder-heading"><strong>Available study time</strong>'
                '<span>Set your usual week once, then adjust individual dates when plans change.</span></div>'
            )
            _availability(preferences, path)

    with commitments:
        left, right = st.columns([1.1, 1], gap="medium")
        with left, st.container(border=True, key="sf_panel_plan_commitments"):
            _commitments(preferences, path)
        with right, st.container(border=True, key="sf_panel_plan_events"):
            _fixed_event_controls(store, fixed_events, render_fixed_events)
        if root is not None:
            with st.container(border=True, key="sf_panel_plan_calendar"):
                st.markdown("**Connect your schedule**")
                st.caption("Preview changes and confirm before updating your StudentFlow calendar.")
                _calendar_controls(store, root, data_dir, path, fixed_events,
                                   source_fingerprint, saved_plan_is_fresh)


def _fixed_event_controls(store, fixed_events, render_fixed_events) -> None:
    accounts = sorted({event["account"] for event in fixed_events})
    connected = str(st.session_state.get("gmail_profile", {}).get("email_address") or "").casefold()
    if connected and connected not in accounts:
        accounts.append(connected)
    if accounts:
        account = accounts[0] if len(accounts) == 1 else st.selectbox("Fixed-event account", accounts)
        render_fixed_events(store, account=account)
    else:
        st.markdown("**Saved fixed events**")
        st.caption("Reviewed appointments from Inbox or Assistant appear here and protect your study time.")


def _calendar_controls(store, root, data_dir, path, fixed_events,
                       source_fingerprint, saved_plan_is_fresh) -> None:
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


def _build_plan(tasks: list[dict], store, preferences: dict) -> None:
    """Keep draft validation and explicit save separate from visual organization."""
    if tasks:
        ready = sum(task.get("status") == "confirmed" and not is_completed(task) for task in tasks)
        st.html(
            '<div class="sf-plan-builder-heading"><strong class="sf-plan-builder-title">'
            'Plan your next sessions</strong>'
            f'<span>{ready} confirmed unfinished task(s). '
            'Schedule remaining study time around your routine.</span></div>'
        )
    else:
        st.markdown("**Plan your next sessions**")
        st.info("Save a task from an email before creating a study plan.")
    with st.expander("How your plan is built"):
        st.caption(
            "Earlier deadlines come first. Today's elapsed time is skipped; future days start "
            "from the earliest available time after meals and commitments. Sessions prefer whole-hour "
            "starts, using other times when needed to fit work before deadlines. Tasks with missing "
            "details are listed for review."
        )
    # Start at the next whole minute so saving a draft never schedules in the past.
    now = (datetime.now(ZONE) + timedelta(minutes=1)).replace(second=0, microsecond=0).isoformat()
    progress = store.progress_by_day()
    slots = resolve_availability(preferences, now)
    signature = planning_signature(tasks, preferences | {"completed_by_day": progress}, now)
    with st.container(key="sf_plan_builder_actions"):
        left, right = st.columns(2)
        generate = left.button("Generate study plan", disabled=not tasks or not slots,
                               type="primary", width="stretch")
        replan = right.button("Replan remaining tasks", disabled=not tasks or not slots,
                              key="sf_plan_replan", width="stretch")
    if not slots:
        st.caption("Add weekly hours or a date-specific window in Availability & preferences to get started.")
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
        _schedule_display(result["blocks"], key="draft")
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
