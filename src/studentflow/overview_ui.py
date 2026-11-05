"""A read-only calendar and task shortcuts for the local Overview workspace."""

from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from studentflow.planning_ui import read_preferences

ZONE = ZoneInfo("Asia/Singapore")


def _instant(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(ZONE) if parsed.utcoffset() is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def open_tasks(tasks: list[dict]) -> list[dict]:
    """Keep unknown effort open, and order real deadlines before undated tasks."""
    remaining = [task for task in tasks if not (
        (task.get("estimated_minutes") or 0) > 0
        and (task.get("completed_minutes") or 0) >= task["estimated_minutes"]
    )]
    latest = datetime.max.replace(tzinfo=ZONE)
    return sorted(remaining, key=lambda task: (
        _instant(task.get("due_at")) or latest, str(task.get("title", "")).casefold(),
        str(task.get("id", "")),
    ))


unfinished_tasks = open_tasks


def calendar_items(tasks: list[dict], plan: dict | None,
                   fixed_events: list[dict], commitments: list[dict]) -> list[dict]:
    """Read exact saved times, omitting malformed and timezone-free entries."""
    items = []

    def add(title, kind, start_value, end_value=None):
        start = _instant(start_value)
        end = _instant(end_value) if end_value is not None else None
        if start is None or (end_value is not None and (end is None or end <= start)):
            return
        items.append({"title": str(title or "Untitled"), "kind": kind,
                      "start": start, "end": end})

    for block in (plan or {}).get("result", {}).get("blocks", []):
        add(block.get("title"), "Saved study", block.get("start"), block.get("end"))
    for event in fixed_events:
        add(event.get("title"), "Fixed event", event.get("start_at"), event.get("end_at"))
    for event in commitments:
        add(event.get("title"), "Commitment", event.get("start"), event.get("end"))
    for task in open_tasks(tasks):
        add(task.get("title"), "Deadline", task.get("due_at"))
    return sorted(items, key=lambda item: (item["start"], item["kind"], item["title"]))


def items_for_day(items: list[dict], day: date) -> list[dict]:
    start = datetime.combine(day, time.min, ZONE)
    end = start + timedelta(days=1)
    return [item for item in items if (
        start <= item["start"] < end if item["end"] is None
        else item["start"] < end and item["end"] > start
    )]


def shift_month(month: date, offset: int) -> date:
    index = month.year * 12 + month.month - 1 + offset
    return date(index // 12, index % 12 + 1, 1)


def _change_month(offset: int) -> None:
    month = date.fromisoformat(st.session_state.sf_cal_month)
    updated = shift_month(month, offset)
    st.session_state.sf_cal_month = updated.isoformat()
    st.session_state.sf_cal_selected = updated.isoformat()


def _select_day(day: date) -> None:
    st.session_state.sf_cal_selected = day.isoformat()


def _today(day: date) -> None:
    st.session_state.sf_cal_month = day.replace(day=1).isoformat()
    st.session_state.sf_cal_selected = day.isoformat()


def _event_html(item: dict, day: date) -> str:
    start, end = item["start"], item["end"]
    clock = start.strftime("%H:%M") if start.date() == day else "Continues"
    if end is not None:
        clock += "–" + (end.strftime("%H:%M") if end.date() == day else "24:00")
    return (
        '<div class="sf-cal-event"><div class="sf-cal-event-time">' + clock + '</div>'
        '<div class="sf-cal-event-body"><div class="sf-cal-event-title">'
        + escape(item["title"]) + '</div><div class="sf-cal-event-kind">'
        + escape(item["kind"]) + '</div></div></div>'
    )


def _render_calendar(items: list[dict], today: date) -> None:
    st.subheader("Your calendar")
    st.session_state.setdefault("sf_cal_month", today.replace(day=1).isoformat())
    st.session_state.setdefault("sf_cal_selected", today.isoformat())
    try:
        month = date.fromisoformat(st.session_state.sf_cal_month).replace(day=1)
        selected = date.fromisoformat(st.session_state.sf_cal_selected)
        if not 1900 <= month.year <= 2100:
            raise ValueError
    except (TypeError, ValueError):
        _today(today)
        month, selected = today.replace(day=1), today
    with st.container(key="sf_cal_toolbar"):
        title, back, forward, current = st.columns([3, 0.65, 0.65, 1.1], gap="small")
        title.markdown("**" + month.strftime("%B %Y") + "**")
        back.button("Previous month", icon=":material/chevron_left:",
                    help="Previous month", key="sf_cal_previous", on_click=_change_month,
                    args=(-1,), disabled=month <= date(1900, 1, 1), width="stretch")
        forward.button("Next month", icon=":material/chevron_right:",
                       help="Next month", key="sf_cal_next", on_click=_change_month,
                       args=(1,), disabled=month >= date(2100, 12, 1), width="stretch")
        current.button("Today", key="sf_cal_today", on_click=_today, args=(today,), width="stretch")
    st.html('<div class="sf-cal-weekdays">' + ''.join(
        '<span>' + name + '</span>' for name in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    ) + '</div>')
    with st.container(key="sf_cal_grid"):
        for week in calendar.Calendar(firstweekday=0).monthdatescalendar(month.year, month.month):
            for column, day in zip(st.columns(7, gap="small"), week, strict=True):
                entries = items_for_day(items, day)
                label = str(day.day) + (" ·" if entries else "")
                wrapper = ("sf_cal_today_" if day == today else "sf_cal_cell_") + day.isoformat()
                with column, st.container(key=wrapper):
                    st.button(label, key="sf_cal_day_" + day.isoformat(),
                              disabled=day.month != month.month,
                              type="primary" if day == selected else "secondary",
                              help=f"{day:%d %B %Y} · {len(entries)} item(s)",
                              width="stretch", on_click=_select_day, args=(day,))
    st.caption("• Dates with saved study sessions, fixed events or task deadlines.")
    st.markdown("**" + selected.strftime("%A, %d %B") + "**")
    entries = items_for_day(items, selected)
    with st.container(height=140, border=False, key="sf_cal_agenda"):
        if entries:
            st.html(''.join(_event_html(item, selected) for item in entries))
        else:
            st.html('<div class="sf-cal-empty">A little breathing room.<br>'
                    '<span>No saved items for this day.</span></div>')


def _open_task(identifier: str) -> None:
    st.session_state.sf_app_pending_tab = "Tasks"
    st.session_state.tasks_next_category = "Unfinished"
    st.session_state.tasks_next_selection = identifier
    st.session_state.sf_task_search = ""
    st.session_state.sf_task_status = "All tasks"


def _duration(minutes: int) -> str:
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _render_todos(tasks: list[dict], now: datetime) -> None:
    st.subheader("To do")
    todos = open_tasks(tasks)
    st.caption(f"{len(todos)} open task(s) · Earlier deadlines first")
    choice = st.radio("Show to-do tasks", ["All open", "Due today", "Needs details"],
                      horizontal=True, label_visibility="collapsed", key="sf_overview_todo_filter")
    if choice == "Due today":
        todos = [task for task in todos if (due := _instant(task.get("due_at")))
                 and due.date() == now.date()]
    elif choice == "Needs details":
        todos = [task for task in todos if task.get("status") != "confirmed"]
    with st.container(height=420, border=False, key="sf_overview_todos"):
        if not todos:
            st.html('<div class="sf-cal-empty">All clear here.<br>'
                    '<span>No open tasks match this view.</span></div>')
        for task in todos:
            with st.container(key="sf_overview_todo_" + str(task["id"])):
                st.button(task["title"], key="sf_overview_open_" + str(task["id"]),
                          on_click=_open_task, args=(task["id"],), width="stretch", wrap=True,
                          help="Open task details and progress")
                due = _instant(task.get("due_at"))
                date_text = due.strftime("%d %b · %H:%M") if due else "Deadline needed"
                overdue = due is not None and due < now
                if overdue:
                    date_text = "Overdue · " + date_text
                estimate = task.get("estimated_minutes") or 0
                completed = task.get("completed_minutes") or 0
                duration = _duration(max(0, estimate - completed)) + " left" if estimate else "Time needed"
                status = "Needs details" if task.get("status") != "confirmed" else "Ready to plan"
                st.html('<div class="sf-todo-meta"><span class="sf-todo-deadline'
                        + (' sf-todo-overdue' if overdue else '') + '">'
                        + escape(date_text) + '</span><span>' + escape(duration) + '</span></div>'
                        '<div class="sf-todo-status">' + status + '</div>')
                if estimate and completed:
                    st.progress(min(1.0, completed / estimate))
    st.caption("Open a task to review details or update its progress.")


def render_overview_schedule(store, data_dir: Path) -> None:
    """Show local saved data without fetching or changing external calendars."""
    now = datetime.now(ZONE)
    tasks = store.tasks()
    preferences_error = False
    try:
        commitments = read_preferences(Path(data_dir) / "planning_preferences.json")["busy_events"]
    except (OSError, ValueError, TypeError, KeyError):
        commitments, preferences_error = [], True
    items = calendar_items(tasks, store.latest_plan(), store.list_fixed_events(), commitments)
    left, right = st.columns([1.1, 1], gap="medium")
    with left, st.container(border=True, key="sf_panel_overview_calendar"):
        _render_calendar(items, now.date())
        if preferences_error:
            st.caption("Manual commitments could not be loaded. Review your Study plan settings.")
    with right, st.container(border=True, key="sf_panel_overview_todos"):
        _render_todos(tasks, now)
