"""Weekly routines and date-specific changes for the study planner."""

from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, time
from pathlib import Path

import streamlit as st

from studentflow.availability import (
    MAX_HORIZON_DAYS,
    WEEKDAYS,
    ZONE,
    dated_override,
    resolve_availability,
    weekly_rule,
)

Save = Callable[[Path, dict, str], None]


def _rule_label(rule: dict) -> str:
    days = ", ".join(WEEKDAYS[day][:3] for day in sorted(set(rule["weekdays"])))
    return f"{days} · {rule['start']}–{rule['end']}"


def _period_label(entry: dict) -> str:
    start = datetime.fromisoformat(entry["start"]).astimezone(ZONE)
    end = datetime.fromisoformat(entry["end"]).astimezone(ZONE)
    kind = {"add": "Extra time", "remove": "Unavailable", "off": "Day off"}.get(
        entry.get("mode"), "Extra time"
    )
    if entry.get("mode") == "off":
        return f"{start:%d %b %Y} · Day off"
    return f"{start:%d %b %Y, %H:%M}–{end:%H:%M} · {kind}"


def render_availability(preferences: dict, path: Path, save: Save, now: datetime) -> None:
    st.markdown("**Available study time**")
    st.caption("Set your usual week once, then adjust individual dates when plans change.")
    rules = preferences["weekly_availability"]
    if rules:
        st.dataframe(
            [
                {
                    "Days": ", ".join(WEEKDAYS[day] for day in sorted(set(rule["weekdays"]))),
                    "Start": rule["start"],
                    "End": rule["end"],
                }
                for rule in rules
            ],
            hide_index=True,
            width="stretch",
        )
    with st.expander("Add weekly availability", expanded=not rules), st.form("sf_plan_weekly_add"):
        days = st.multiselect("Weekdays", options=list(WEEKDAYS), key="sf_plan_weekdays")
        left, right = st.columns(2)
        start = left.time_input("Start", value=time(9), key="sf_plan_weekly_start")
        end = right.time_input("End", value=time(18), key="sf_plan_weekly_end")
        if st.form_submit_button("Add weekly rule"):
            try:
                rule = weekly_rule([WEEKDAYS.index(day) for day in days], start, end)
                updated = deepcopy(preferences)
                if any(
                    set(existing["weekdays"]) == set(rule["weekdays"])
                    and existing["start"] == rule["start"]
                    and existing["end"] == rule["end"]
                    for existing in rules
                ):
                    st.info("This weekly rule is already saved.")
                else:
                    updated["weekly_availability"].append(rule)
                    save(
                        path, updated, "Weekly availability saved. Generate a new plan to apply it."
                    )
            except ValueError as exc:
                st.error(str(exc))
    if rules:
        with st.expander("Edit or remove weekly rules"):
            by_id = {rule["id"]: rule for rule in rules}
            chosen = st.selectbox(
                "Weekly rule",
                options=list(by_id),
                format_func=lambda key: _rule_label(by_id[key]),
                key="sf_plan_edit_weekly",
            )
            selected = by_id[chosen]
            with st.form("sf_plan_weekly_edit_" + chosen):
                days = st.multiselect(
                    "Weekdays",
                    options=list(WEEKDAYS),
                    default=[WEEKDAYS[day] for day in selected["weekdays"]],
                    key="sf_plan_edit_days_" + chosen,
                )
                left, right = st.columns(2)
                start = left.time_input(
                    "Start",
                    value=time.fromisoformat(selected["start"]),
                    key="sf_plan_edit_start_" + chosen,
                )
                end = right.time_input(
                    "End",
                    value=time.fromisoformat(selected["end"]),
                    key="sf_plan_edit_end_" + chosen,
                )
                if st.form_submit_button("Save weekly rule"):
                    try:
                        updated = deepcopy(preferences)
                        replacement = weekly_rule(
                            [WEEKDAYS.index(day) for day in days], start, end, rule_id=chosen
                        )
                        updated["weekly_availability"] = [
                            replacement if rule["id"] == chosen else rule for rule in rules
                        ]
                        save(
                            path,
                            updated,
                            "Weekly availability updated. Generate a new plan to apply it.",
                        )
                    except ValueError as exc:
                        st.error(str(exc))
            remove_ids = st.multiselect(
                "Weekly rules to remove",
                options=list(by_id),
                format_func=lambda key: _rule_label(by_id[key]),
                key="sf_plan_remove_weekly",
            )
            if st.button("Remove selected weekly rules", disabled=not remove_ids):
                updated = deepcopy(preferences)
                updated["weekly_availability"] = [
                    rule for rule in rules if rule["id"] not in remove_ids
                ]
                save(path, updated, "Selected weekly rules removed.")

    with st.expander("Change a particular day", expanded=not rules):
        st.caption(
            "Add extra hours, remove part of a day, or take the whole day off. Removed hours override all additions until you remove that dated change."
        )
        kind = st.selectbox(
            "Change",
            ["Add available time", "Remove available time", "Whole day unavailable"],
            key="sf_plan_override_kind",
        )
        with st.form("sf_plan_add_availability"):
            day = st.date_input(
                "Date", value=now.date(), min_value=now.date(), key="sf_plan_slot_day"
            )
            left, right = st.columns(2)
            start = left.time_input(
                "Start",
                value=time(9),
                disabled=kind == "Whole day unavailable",
                key="sf_plan_slot_start",
            )
            end = right.time_input(
                "End",
                value=time(11),
                disabled=kind == "Whole day unavailable",
                key="sf_plan_slot_end",
            )
            if st.form_submit_button(kind):
                try:
                    mode = {
                        "Add available time": "add",
                        "Remove available time": "remove",
                        "Whole day unavailable": "off",
                    }[kind]
                    entry = dated_override(day, mode, start, end)
                    if datetime.fromisoformat(entry["end"]) <= now:
                        raise ValueError("Choose a time that has not already ended.")
                    updated = deepcopy(preferences)
                    if any(
                        all(existing.get(key) == entry[key] for key in ("start", "end", "mode"))
                        for existing in updated["availability_overrides"]
                    ):
                        st.info("This dated change is already saved.")
                    else:
                        updated["availability_overrides"].append(entry)
                        save(
                            path,
                            updated,
                            "Date-specific availability saved. Generate a new plan to apply it.",
                        )
                except ValueError as exc:
                    st.error(str(exc))

    dated = {
        **{"slot:" + item["id"]: item for item in preferences["available_slots"]},
        **{"change:" + item["id"]: item for item in preferences["availability_overrides"]},
    }
    if dated:
        with st.expander("Review or remove dated changes"):
            st.caption(
                "Removing an unavailable period restores the weekly routine for those hours. Expired dated entries are removed automatically; weekly rules stay."
            )
            st.dataframe(
                [
                    {"Dated change": _period_label(item)}
                    for item in sorted(dated.values(), key=lambda item: item["start"])
                ],
                hide_index=True,
                width="stretch",
            )
            chosen = st.multiselect(
                "Dated changes to remove",
                options=list(dated),
                format_func=lambda key: _period_label(dated[key]),
                key="sf_plan_remove_slots",
            )
            if st.button(
                "Remove selected time", disabled=not chosen, key="sf_plan_remove_slot_button"
            ):
                updated = deepcopy(preferences)
                updated["available_slots"] = [
                    item
                    for item in updated["available_slots"]
                    if "slot:" + item["id"] not in chosen
                ]
                updated["availability_overrides"] = [
                    item
                    for item in updated["availability_overrides"]
                    if "change:" + item["id"] not in chosen
                ]
                save(path, updated, "Selected dated changes removed.")

    with st.expander("Upcoming available time"):
        with st.form("sf_plan_horizon"):
            horizon = st.number_input(
                "Plan ahead (days)",
                min_value=1,
                max_value=MAX_HORIZON_DAYS,
                value=preferences["availability_horizon_days"],
                step=1,
                key="sf_plan_horizon_days",
            )
            if st.form_submit_button("Save planning horizon"):
                updated = deepcopy(preferences)
                updated["availability_horizon_days"] = int(horizon)
                save(path, updated, "Planning horizon saved. Generate a new plan to apply it.")
        resolved = resolve_availability(preferences, now)
        st.caption(
            f"Next {preferences['availability_horizon_days']} days including today. Study preferences and commitments are applied when generating the plan."
        )
        if resolved:
            st.dataframe(
                [
                    {
                        "Date": datetime.fromisoformat(item["start"]).strftime("%d %b %Y"),
                        "Start": datetime.fromisoformat(item["start"]).strftime("%H:%M"),
                        "End": datetime.fromisoformat(item["end"]).strftime("%H:%M"),
                    }
                    for item in resolved
                ],
                hide_index=True,
                width="stretch",
            )
        else:
            st.info(
                "No available study time in this period. Add weekly hours or extra time for a date."
            )
