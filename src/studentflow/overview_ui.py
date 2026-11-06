"""Local Overview calendar and task checkoff controls."""

from __future__ import annotations

import calendar
import sqlite3
from base64 import b64encode
from datetime import date, datetime, time, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from studentflow.planning_ui import read_preferences
from studentflow.planner import is_completed
from studentflow.storage import DEADLINE_COLORS

ZONE = ZoneInfo("Asia/Singapore")
_CALENDAR_FRAME = Path(__file__).resolve().parents[2] / "assets" / "overview" / "calendar-title-frame.png"

_WEEK_COLORS_JS = _CALENDAR_FRAME.with_name("week-colors.js").read_text(encoding="utf-8")
_WEEK_COLORS_KEY = "sf_week_colors"


def _save_week_color(store):
    selection = st.session_state[_WEEK_COLORS_KEY].get("color")
    if not isinstance(selection, dict):
        return
    task_id, color = selection.get("task_id"), selection.get("color")
    if not isinstance(task_id, str) or not isinstance(color, str):
        return
    try:
        store.set_deadline_color(task_id, color)
    except (OSError, sqlite3.Error, ValueError):
        st.session_state.sf_week_color_error = "Could not save this calendar color. Please try again."


def _instant(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(ZONE) if parsed.utcoffset() is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def open_tasks(tasks: list[dict]) -> list[dict]:
    """Keep unknown effort open, and order real deadlines before undated tasks."""
    return _sort_todos(task for task in tasks if not is_completed(task))


def _sort_todos(tasks):
    latest = datetime.max.replace(tzinfo=ZONE)
    return sorted(tasks, key=lambda task: (
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
        if _instant(task.get("due_at")) is not None:
            items[-1].update(task_id=task.get("id"), color=task.get("deadline_color"))
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


def _event_html(item: dict, day: date, *, editable: bool = False) -> str:
    start, end = item["start"], item["end"]
    clock = start.strftime("%H:%M") if start.date() == day else "Continues"
    if end is not None:
        clock += "–" + (end.strftime("%H:%M") if end.date() == day else "24:00")
    color = item.get("color")
    if not isinstance(color, str) or color not in DEADLINE_COLORS:
        color = "blue"
    marker = ""
    if editable and item.get("task_id"):
        marker = (
            '<button type="button" class="sf-week-marker" aria-haspopup="dialog" '
            f'aria-label="Change deadline color for {escape(item["title"])}" '
            f'data-task-id="{escape(str(item["task_id"]))}" data-current-color="{color}"></button>'
        )
    return (
        '<div class="sf-cal-event"><div class="sf-cal-event-time">' + clock + '</div>'
        f'<div class="sf-cal-event-body" style="--sf-deadline-color:{DEADLINE_COLORS[color]}">'
        + marker + '<div class="sf-cal-event-title">'
        + escape(item["title"]) + '</div><div class="sf-cal-event-kind">'
        + escape(item["kind"]) + '</div></div></div>'
    )


def _render_calendar(items: list[dict], today: date, store) -> None:
    frame = b64encode(_CALENDAR_FRAME.read_bytes()).decode("ascii")
    cropped = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="78 158 581 329">'
        f'<image width="736" height="736" href="data:image/png;base64,{frame}"/></svg>'
    )
    border = b64encode(cropped.encode()).decode("ascii")
    st.html(
        '<style>.st-key-sf_panel_overview_calendar {'
        f'--sf-cal-border: url("data:image/svg+xml;base64,{border}");'
        '}</style><h3 class="sf-cal-panel-title">YOUR CALENDER</h3>'
    )
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
        title, controls = st.columns([2, 1], gap="small")
        title.html(f'<div class="sf-cal-pixel-month">{month:%B %Y}</div>')
        with controls:
            back, forward, current = st.columns([1, 1, 2], gap="small")
            back.button("Previous month", icon=":material/chevron_left:",
                        help="Previous month", key="sf_cal_previous", on_click=_change_month,
                        args=(-1,), disabled=month <= date(1900, 1, 1), width="stretch")
            forward.button("Next month", icon=":material/chevron_right:",
                           help="Next month", key="sf_cal_next", on_click=_change_month,
                           args=(1,), disabled=month >= date(2100, 12, 1), width="stretch")
            current.button("Today", key="sf_cal_today", on_click=_today, args=(today,), width="stretch")
    with st.container(key="sf_cal_layout"):
        pages, agenda = st.columns([2, 1], gap="small")
        with pages:
            with st.container(key=f"sf_cal_book_{month:%Y_%m}"):
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
        with agenda, st.container(key="sf_cal_day_details"):
            st.markdown("**" + selected.strftime("%A, %d %B") + "**")
            entries = items_for_day(items, selected)
            with st.container(height=140, border=False, key="sf_cal_agenda"):
                if entries:
                    st.html(''.join(_event_html(item, selected, editable=True) for item in entries))
                else:
                    st.html('<div class="sf-cal-empty">A little breathing room.<br>'
                            '<span>No saved items for this day.</span></div>')
    monday = selected - timedelta(days=selected.weekday())
    week_columns = []
    for offset in range(7):
        day = monday + timedelta(days=offset)
        events = "".join(_event_html(item, day, editable=True) for item in items_for_day(items, day))
        current = ' aria-current="date"' if day == selected else ''
        week_columns.append(
            f'<section class="sf-week-day"{current}>'
            f'<h4>{day:%A}<small>{day:%d %b}</small></h4>'
            f'<div class="sf-week-events">{events}</div></section>'
        )
    swatches = "".join(
        f'<button type="button" data-color="{name}" style="--sf-swatch:{value}">{name.title()}</button>'
        for name, value in DEADLINE_COLORS.items()
    )
    week_colors = st.components.v2.component(
        "studentflow_week_colors", js=_WEEK_COLORS_JS, isolate_styles=False,
    )
    week_colors(
        key=_WEEK_COLORS_KEY,
        data='<div class="sf-week-plan" role="region" aria-label="Selected week">'
        + "".join(week_columns) + '</div>'
        + '<div class="sf-week-palette" popover role="dialog" aria-label="Deadline color">'
        + swatches + '</div>',
        on_color_change=lambda: _save_week_color(store),
    )
    if error := st.session_state.pop("sf_week_color_error", None):
        st.error(error)
    st.caption("• Dates with saved study sessions, fixed events or task deadlines.")


def _open_task(identifier: str, complete: bool = False) -> None:
    st.session_state.sf_app_pending_tab = "Tasks"
    st.session_state.tasks_next_category = "Completed" if complete else "Unfinished"
    st.session_state.sf_todo_refresh_app = True
    st.session_state.tasks_next_selection = identifier
    st.session_state.sf_task_search = ""
    st.session_state.sf_task_status = "All tasks"


def _duration(minutes: int) -> str:
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _toggle_todo(store, task, key):
    try:
        store.set_task_completion(task["id"], st.session_state[key], expected_updated_at=task.get("updated_at"))
    except (OSError, sqlite3.Error, ValueError) as error:
        st.session_state.sf_todo_error = str(error) if isinstance(error, ValueError) else "Could not update this task."
    else:
        st.session_state.sf_todo_refresh_app = True


@st.fragment(run_every="30s")
def _render_todos(store) -> None:
    if st.session_state.pop("sf_todo_refresh_app", False):
        st.rerun()
    now = datetime.now(ZONE)
    st.subheader("To do")
    if error := st.session_state.pop("sf_todo_error", None):
        st.error(error)
    tasks = store.tasks()
    todos = open_tasks(tasks)
    open_count = len(todos)
    completed = _sort_todos(task for task in tasks if is_completed(task)
                            and ((due := _instant(task.get("due_at"))) is None or due > now))
    choice = st.radio("Show to-do tasks", ["All open", "Due today", "Needs details"],
                      horizontal=True, label_visibility="collapsed", key="sf_overview_todo_filter")
    todos += completed
    if choice == "Due today":
        todos = [task for task in todos if (due := _instant(task.get("due_at")))
                 and due.date() == now.date()]
    elif choice == "Needs details":
        todos = [task for task in todos if task.get("status") != "confirmed"]
    with st.container(height=420, border=False, key="sf_overview_todos"):
        if not todos:
            st.html('<div class="sf-cal-empty">All clear here.<br>'
                    '<span>No tasks match this view.</span></div>')
            if open_count and choice != "All open":
                with st.container(horizontal=True, horizontal_alignment="center"):
                    st.button(
                        f"Show all {open_count} open tasks",
                        key="sf_overview_show_all",
                        on_click=st.session_state.update,
                        kwargs={"sf_overview_todo_filter": "All open"},
                    )
        for task in todos:
            complete = is_completed(task)
            identifier = str(task["id"])
            checkbox_key = "sf_overview_done_" + identifier
            st.session_state[checkbox_key] = complete
            row_key = "sf_overview_todo_" + ("completed_" if complete else "") + identifier
            with st.container(key=row_key):
                with st.container(key="sf_todo_line_" + identifier):
                    checkbox_column, title_column = st.columns([1, 12], gap="small")
                    checkbox_column.checkbox(
                        "Complete " + task["title"], key=checkbox_key, label_visibility="collapsed",
                        disabled=complete and is_completed({**task, "marked_complete": False}),
                        on_change=_toggle_todo, args=(store, task, checkbox_key),
                    )
                    title_column.button(task["title"], key="sf_overview_open_" + identifier,
                              on_click=_open_task, args=(task["id"], complete), width="stretch", wrap=True,
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
                if complete:
                    duration, status = "", "Completed"
                st.html('<div class="sf-todo-meta"><span class="sf-todo-deadline'
                        + (' sf-todo-overdue' if overdue else '') + '">'
                        + escape(date_text) + '</span><span>' + escape(duration) + '</span></div>'
                        '<div class="sf-todo-status">' + status + '</div>')
                if estimate and completed and not complete:
                    st.progress(min(1.0, completed / estimate))
    st.caption(f"{open_count} open task(s) · Earlier deadlines first")
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
    left, right = st.columns([2.35, 1], gap="medium")
    with left, st.container(border=True, key="sf_panel_overview_calendar"):
        _render_calendar(items, now.date(), store)
        if preferences_error:
            st.caption("Manual commitments could not be loaded. Review your Study plan settings.")
    with right, st.container(border=True, key="sf_panel_overview_todos"):
        _render_todos(store)
