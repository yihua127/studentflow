"""Review, edit and track saved tasks without changing their source evidence."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

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


def _is_completed(task):
    estimate = task.get("estimated_minutes") or 0
    return bool(estimate > 0 and (task.get("completed_minutes") or 0) >= estimate)


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
            "Remaining (min)": max(0, estimate - completed) if estimate else None,
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


def _render_editor(store, task):
    revision = _editor_revision(task)
    prefix = f"task_edit_{task['id']}_{revision}"
    versions = st.session_state.setdefault("tasks_editor_versions", {})
    previous = versions.get(task["id"])
    if previous and previous != revision and not st.session_state.get("tasks_own_save"):
        st.info("This task has changed. Its latest details and progress are shown below.")
    versions[task["id"]] = revision

    st.write("**Edit task details**")
    st.caption("Confirmed requires a deadline and estimated study time greater than 0 minutes. "
               "You can save missing details and finish them later.")
    st.caption("Estimated study time is the total time needed, including time already completed. "
               "Remaining time is the estimate minus completed minutes. "
               "When remaining time reaches 0, the task moves to Completed.")
    with st.expander("Original supporting text", expanded=False):
        st.text(task.get("source_quote") or "No supporting text saved.")
        st.caption("Source: " + (task.get("mail_subject") or "Saved message"))
        if task.get("source_filename"):
            source = "Attachment: " + task["source_filename"]
            if task.get("source_page") is not None:
                source += f" · Page {task['source_page']}"
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
                help="Total study time, including completed minutes. Use 0 for unknown.",
            )
        with right:
            completed = st.number_input(
                "Completed study time (minutes)", value=int(task.get("completed_minutes") or 0),
                min_value=0, step=15, key=prefix + "_completed",
                help="Total minutes already completed, not additional minutes for today. "
                     "Cannot exceed the estimated study time.",
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
                updated = store.update_task(
                    task["id"], changes, expected_updated_at=task.get("updated_at"),
                )
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


def _render_delete(store, task):
    prefix = f"task_delete_{task['id']}_{_editor_revision(task)}"
    with st.expander("Delete task"):
        st.write(task["title"])
        st.caption("Remove this task from your task list, saved plan and upcoming daily reviews. "
                   "Saved attachments and previously recorded study time are kept.")
        confirmed = st.checkbox("I want to delete this task.", key=prefix + "_confirm")
        if st.button("Delete task", key=prefix + "_button", disabled=not confirmed):
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
    st.subheader("Tasks")
    st.caption("Edit task details and track remaining study time here. "
               "Confirmed means the task is ready for planning; Completed means no study time remains.")
    notice = st.session_state.pop("tasks_notice", None)
    if notice:
        st.success(notice)
    if not tasks:
        st.info("No tasks yet. Select messages in Inbox (Gmail) to extract or add tasks.")
        return

    if "tasks_next_category" in st.session_state:
        st.session_state.tasks_category = st.session_state.pop("tasks_next_category")
    counts = {
        "Unfinished": sum(not _is_completed(task) for task in tasks),
        "Completed": sum(_is_completed(task) for task in tasks),
    }
    category = st.radio(
        "Task category", ["Unfinished", "Completed"], key="tasks_category", horizontal=True,
        format_func=lambda name: f"{name} ({counts[name]})",
    )
    selected_tasks = [task for task in tasks if _is_completed(task) == (category == "Completed")]
    if selected_tasks:
        st.dataframe(task_rows(selected_tasks), hide_index=True, width="stretch")
        by_id = {task["id"]: task for task in selected_tasks}
        selection_key = "tasks_selected_" + category
        next_selection = st.session_state.pop("tasks_next_selection", None)
        if next_selection in by_id:
            st.session_state[selection_key] = next_selection
        elif st.session_state.get(selection_key) not in by_id:
            st.session_state[selection_key] = next(iter(by_id))
        task_id = st.selectbox(
            "Edit task", list(by_id), key=selection_key,
            format_func=lambda identifier: (
                by_id[identifier]["title"] + " · "
                + (by_id[identifier].get("mail_subject") or "Saved message")
            ),
        )
        _render_editor(store, by_id[task_id])
        _render_delete(store, by_id[task_id])
    else:
        st.info("No completed tasks yet." if category == "Completed" else "No unfinished tasks.")
    st.session_state.pop("tasks_own_save", None)
    st.download_button("Download tasks", tasks_csv(tasks), "studentflow_tasks.csv", "text/csv")
