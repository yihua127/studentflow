"""Review, edit and track saved tasks without changing their source evidence."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
from base64 import b64encode
from collections import Counter
from datetime import datetime
from html import escape
from pathlib import Path
from time import sleep
from zoneinfo import ZoneInfo

import streamlit as st

from studentflow.page_heading import render_page_heading
from studentflow.planner import is_completed as _is_completed

ZONE = ZoneInfo("Asia/Singapore")


def _display_time(value):
    if not value:
        return "Not specified"
    try:
        return datetime.fromisoformat(value).astimezone(ZONE).strftime("%d %b %Y, %H:%M")
    except (ValueError, TypeError):
        return str(value)


def _deadline_input(value):
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).astimezone(ZONE).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(value)


def _duration(minutes):
    hours, remainder = divmod(int(minutes), 60)
    return f"{hours}h {remainder}m" if hours and remainder else f"{hours}h" if hours else f"{remainder}m"


def _task_choice_labels(tasks: list[dict]) -> dict[str, str]:
    """Keep native selection labels unique without changing stable task IDs."""
    labels = {
        task["id"]: task["title"] + " · " + (task.get("mail_subject") or "Saved message")
        for task in tasks
    }
    counts = Counter(labels.values())
    reserved = set(labels.values())
    sequence = Counter()
    for identifier, label in labels.items():
        if counts[label] == 1:
            continue
        sequence[label] += 1
        candidate = f"{label} · #{sequence[label]}"
        while candidate in reserved:
            sequence[label] += 1
            candidate = f"{label} · #{sequence[label]}"
        labels[identifier] = candidate
        reserved.add(candidate)
    return labels


def preserve_task_inputs():
    """Keep browsing choices and ordinary edits when another workspace is shown.

    Review and deletion confirmations intentionally remain scoped to a visible
    editor; they must not be restored when a user returns to this page.
    """
    for key in list(st.session_state):
        if key in ("tasks_category", "sf_task_search", "sf_task_status") or key.startswith(
            "tasks_selected_",
        ) or (key.startswith("task_edit_") and key.endswith(
            ("_title", "_due", "_estimate", "_completed"),
        )):
            st.session_state[key] = st.session_state[key]


def _choose_task(identifier):
    st.session_state.tasks_next_selection = identifier


def _task_status(task):
    if _is_completed(task):
        return "Completed", "sf-pill-success"
    if task.get("status") == "confirmed":
        return "Ready to plan", ""
    return "Needs details", "sf-pill-warning"


def _render_task_card(task, active):
    identifier = task["id"]
    key = "sf_task_row_" + ("active_" if active else "") + str(identifier)
    with st.container(key=key):
        st.button(task["title"], key=f"sf_task_open_{identifier}", width="stretch", wrap=True,
                  on_click=_choose_task, args=(identifier,))
        label, style = _task_status(task)
        estimate = task.get("estimated_minutes") or 0
        completed = task.get("completed_minutes") or 0
        remaining = "" if _is_completed(task) else (
            _duration(max(0, estimate - completed)) + " left" if estimate else "Time needed"
        )
        st.html(
            '<div class="sf-task-meta"><span class="sf-pill ' + style + '">'
            + label + '</span><span>' + escape(remaining) + '</span></div>'
            '<div class="sf-task-date">Due ' + escape(_display_time(task.get("due_at"))) + '</div>'
        )
        if estimate:
            st.progress(min(1.0, completed / estimate))


def _render_task_summary(task):
    label, style = _task_status(task)
    estimate = task.get("estimated_minutes") or 0
    completed = task.get("completed_minutes") or 0
    progress = (
        f"{_duration(completed)} completed · {_duration(max(0, estimate - completed))} remaining"
        if estimate else "Add an estimated study time to track progress."
    )
    st.html(
        '<div class="sf-task-summary">'
        f'<h3>{escape(task["title"])}</h3>'
        '<div class="sf-task-summary-progress">'
        f'<span class="sf-pill {style}">{label}</span>'
        f'<span>{progress}</span></div></div>'
    )


def task_rows(tasks):
    """Build readable rows for the table and a complete progress export."""
    rows = []
    for task in tasks:
        estimate = task.get("estimated_minutes") or None
        completed = task.get("completed_minutes") or 0
        rows.append({
            "Task": task["title"],
            "Deadline": _display_time(task.get("due_at")),
            "Study time (min)": estimate,
            "Status": "Confirmed" if task.get("status") == "confirmed" else "Needs details",
            "Source message": task.get("mail_subject") or "Saved message",
            "Completed (min)": completed,
            "Remaining (min)": 0 if _is_completed(task) else (max(0, estimate - completed) if estimate else None),
            "Progress": "Completed" if _is_completed(task) else "Unfinished",
        })
    return rows


def tasks_csv(tasks):
    rows = task_rows(tasks)
    if not rows:
        return ""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    for row in rows:
        writer.writerow({
            key: "'" + value
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@"))
            else value
            for key, value in row.items()
        })
    return buffer.getvalue()


def _editor_revision(task):
    # Progress can change in the daily check-in or another browser window.
    fields = {
        key: task.get(key) for key in
        ("id", "updated_at", "title", "due_at", "estimated_minutes", "completed_minutes")
    }
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()[:16]


def _render_editor(store, task, editor_panel):
    revision = _editor_revision(task)
    prefix = f"task_edit_{task['id']}_{revision}"
    versions = st.session_state.setdefault("tasks_editor_versions", {})
    previous = versions.get(task["id"])
    if previous and previous != revision and not st.session_state.get("tasks_own_save"):
        st.info("This task has changed. Its latest details and progress are shown below.")
    versions[task["id"]] = revision

    st.caption("Confirmed requires a deadline and estimated study time greater than 0 minutes. "
               "You can save missing details and finish them later.")
    with st.expander("Original supporting text", expanded=False):
        evidence = task.get("sources") or [task]
        for item in evidence:
            st.text(item.get("source_quote") or "No supporting text saved.")
            st.caption("Source: " + (item.get("mail_subject") or "Saved message"))
            if item.get("source_filename"):
                source = "Attachment: " + item["source_filename"]
                if item.get("source_page") is not None:
                    source += f" · Page {item['source_page']}"
                st.caption(source)

    with st.form(prefix):
        title = st.text_input("Task name", value=task["title"], key=prefix + "_title")
        deadline = st.text_input(
            "Deadline (Singapore time)", value=_deadline_input(task.get("due_at")),
            placeholder="YYYY-MM-DD HH:MM", key=prefix + "_due",
            help="Leave blank if the deadline is not yet known.",
        )
        left, right = st.columns(2)
        with left:
            estimate = st.number_input(
                "Estimated study time (minutes)", value=int(task.get("estimated_minutes") or 0),
                min_value=0, step=15, key=prefix + "_estimate",
                help="Total study time, including completed minutes. Use 0 for unknown. "
                     "Remaining time is the total estimate minus completed minutes.",
            )
        with right:
            completed = st.number_input(
                "Completed study time (minutes)", value=int(task.get("completed_minutes") or 0),
                min_value=0, step=15, key=prefix + "_completed",
                help="Total minutes already completed, not additional minutes for today. "
                     "Cannot exceed the estimated study time. When remaining time reaches 0, "
                     "the task moves to Completed.",
            )
        reviewed = st.checkbox("I have reviewed these task details.", key=prefix + "_reviewed")
        if st.form_submit_button("Save changes", type="primary"):
            try:
                if not reviewed:
                    raise ValueError("Confirm that you have reviewed these task details before saving.")
                if not title.strip():
                    raise ValueError("Enter a task name.")
                due_at = None
                if deadline.strip():
                    try:
                        due_at = datetime.strptime(deadline.strip(), "%Y-%m-%d %H:%M").replace(
                            tzinfo=ZONE,
                        ).isoformat()
                    except ValueError:
                        raise ValueError(
                            "Enter the deadline as YYYY-MM-DD HH:MM, or leave it blank.",
                        ) from None
                if completed > estimate:
                    raise ValueError("Completed study time cannot exceed the estimated study time.")
                changes = {
                    "title": title.strip(), "due_at": due_at,
                    "estimated_minutes": int(estimate) or None,
                    "completed_minutes": int(completed),
                }
                artwork = Path(__file__).resolve().parents[2] / "assets" / "tasks" / "saving-usagi.png"
                image = b64encode(artwork.read_bytes()).decode("ascii")
                loading = editor_panel.empty()
                loading.html(
                    '<div class="sf-inbox-loading sf-task-saving" role="status">'
                    f'<img src="data:image/png;base64,{image}" alt="">'
                    '<span class="sf-inbox-loading-label">'
                    '<span class="sf-inbox-loading-mark" aria-hidden="true"></span>'
                    'Saving changes…</span></div>'
                )
                try:
                    updated = store.update_task(
                        task["id"], changes, expected_updated_at=task.get("updated_at"),
                    )
                    # Local saves can finish before the browser paints the indicator.
                    sleep(.45)
                finally:
                    loading.empty()
                category = "Completed" if _is_completed(updated) else "Unfinished"
                st.session_state.tasks_next_category = category
                st.session_state.tasks_next_selection = task["id"]
                st.session_state.tasks_own_save = True
                st.session_state.tasks_notice = (
                    "Task saved and moved to Completed." if category == "Completed"
                    else "Task saved. Status: Confirmed."
                    if updated.get("status") == "confirmed"
                    else "Task saved as Needs details. Add a deadline and estimated study time "
                         "greater than 0 minutes to confirm it."
                )
                st.rerun()
            except (ValueError, KeyError) as exc:
                st.error(str(exc))

    estimate = task.get("estimated_minutes") or 0
    if estimate:
        st.progress(min(1.0, (task.get("completed_minutes") or 0) / estimate))
    effort = task.get("effort_review")
    if effort:
        with st.expander("Study-time estimate history"):
            st.dataframe([{
                "Source-stated minutes": effort.get("stated_minutes"),
                "AI suggested minutes": effort.get("suggested_minutes"),
                "Confirmed minutes": effort.get("confirmed_minutes"),
                "Review decision": str(effort.get("decision", "")).replace("_", " "),
            }], hide_index=True)
            if effort.get("estimate_reason"):
                st.text(effort["estimate_reason"])
            for assumption in effort.get("assumptions", []):
                st.text(assumption)


@st.fragment
def _render_delete(store, task):
    prefix = f"task_delete_{task['id']}_{_editor_revision(task)}"
    with st.expander("Delete task"):
        st.write(task["title"])
        st.caption("Remove this task from your task list, saved plan and upcoming daily reviews. "
                   "Saved attachments and previously recorded study time are kept.")
        confirmed = st.checkbox("I want to delete this task.", key=prefix + "_confirm")
        st.button("Delete task", key=prefix + "_stage", disabled=not confirmed)
        if st.button("Dispose task", key=prefix + "_dispose", disabled=not confirmed) and confirmed:
            try:
                store.delete_task(task["id"], expected_updated_at=task.get("updated_at"))
            except ValueError as exc:
                st.error(str(exc))
            except (OSError, sqlite3.Error):
                st.error("The task could not be deleted. Please try again.")
            else:
                st.session_state.pop("planner_draft", None)
                st.session_state.get("tasks_editor_versions", {}).pop(task["id"], None)
                st.session_state.pop("tasks_next_selection", None)
                st.session_state.tasks_notice = "Task deleted."
                st.rerun()


def render_tasks(store):
    """Render task categories and save edits against the original stable task ID."""
    tasks = store.tasks()
    render_page_heading(
        "YOUR WORK, IN FOCUS",
        "Tasks",
        "Keep the next step clear. Review details, track progress and make room to study.",
    )
    notice = st.session_state.pop("tasks_notice", None)
    if notice:
        st.success(notice)
    if not tasks:
        with st.container(border=True, key="sf_panel_tasks_empty"):
            st.info("No tasks yet. Select messages in Inbox (Gmail) to extract or add tasks.")
        return

    unfinished = [task for task in tasks if not _is_completed(task)]
    remaining = sum(max(0, (task.get("estimated_minutes") or 0)
                        - (task.get("completed_minutes") or 0)) for task in unfinished)
    needs_details = sum(task.get("status") != "confirmed" for task in unfinished)
    first, second, third = st.columns(3)
    first.metric("Unfinished tasks", len(unfinished))
    second.metric("Known time remaining", _duration(remaining),
                  help="Total remaining time for tasks with an estimate. Unknown times are excluded.")
    third.metric("Needs details", needs_details,
                 help="Add or confirm the missing deadline and study time before planning.")
    if "tasks_next_category" in st.session_state:
        st.session_state.tasks_category = st.session_state.pop("tasks_next_category")
    if st.session_state.get("tasks_own_save"):
        # Keep the saved task visible even if its title/status no longer matches.
        st.session_state.sf_task_search = ""
        st.session_state.sf_task_status = "All tasks"
    counts = {
        "Unfinished": sum(not _is_completed(task) for task in tasks),
        "Completed": sum(_is_completed(task) for task in tasks),
    }
    browser, editor = st.columns([0.95, 1.45], gap="medium")
    with browser, st.container(border=True, key="sf_panel_tasks_browser"):
        st.subheader("Your task list")
        category = st.radio(
            "Task category", ["Unfinished", "Completed"], key="tasks_category", horizontal=True,
            format_func=lambda name: f"{name} ({counts[name]})", label_visibility="collapsed",
        )
        query = st.text_input("Search tasks", key="sf_task_search",
                              placeholder="Task name or source message").strip().casefold()
        status = st.selectbox("Task status", ["All tasks", "Ready to plan", "Needs details"],
                              key="sf_task_status")
        category_tasks = [task for task in tasks
                          if _is_completed(task) == (category == "Completed")]
        selected_tasks = [task for task in category_tasks
                          if (not query or query in task["title"].casefold()
                              or query in (task.get("mail_subject") or "").casefold())
                          and (status == "All tasks"
                               or (task.get("status") == "confirmed") == (status == "Ready to plan"))]
        by_id = {task["id"]: task for task in selected_tasks}
        selection_key = "tasks_selected_" + category
        next_selection = st.session_state.pop("tasks_next_selection", None)
        if next_selection in by_id:
            st.session_state[selection_key] = next_selection
        elif st.session_state.get(selection_key) not in by_id:
            st.session_state[selection_key] = None
        with st.container(key="sf_task_count"):
            st.caption(f"{len(selected_tasks)} of {len(category_tasks)} "
                       f"{category.lower()} task(s)")
        if selected_tasks:
            with st.container(height=430, border=False, key="sf_task_list"):
                for task in selected_tasks:
                    _render_task_card(task, task["id"] == st.session_state.get(selection_key))
        elif category_tasks:
            st.info("No tasks match these filters. Try another search or status.")
        else:
            st.info("No completed tasks yet." if category == "Completed" else "No unfinished tasks.")
    with browser, st.container(border=True, key="sf_panel_tasks_export"):
        if selected_tasks:
            with st.expander("Full task table"):
                st.dataframe(task_rows(selected_tasks), hide_index=True, width="stretch")
        st.download_button("Download tasks", tasks_csv(tasks), "studentflow_tasks.csv", "text/csv",
                           icon=":material/download:", width="stretch")
    editor_panel = editor.container(border=True, key="sf_panel_tasks_editor")
    with editor_panel:
        st.subheader("Edit task")
        task_id = None
        if by_id:
            # Use the full task list so filtering cannot renumber a selected task.
            labels = _task_choice_labels(tasks)
            task_id = st.selectbox(
                "Edit task", list(by_id), key=selection_key,
                index=None, placeholder="Select a task",
                format_func=labels.__getitem__, label_visibility="collapsed",
            )
        if task_id in by_id:
            _render_task_summary(by_id[task_id])
            _render_editor(store, by_id[task_id], editor_panel)
            _render_delete(store, by_id[task_id])
        else:
            st.html('<div class="sf-task-drop-zone">Drop a task here to edit it, '
                    'or select one from the list above.</div>' if by_id else
                    '<div class="sf-empty-state">No tasks are available in this view.</div>')
    st.session_state.pop("tasks_own_save", None)
