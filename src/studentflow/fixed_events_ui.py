"""Explicit fixed appointment extraction, human review and local event editing."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from studentflow.extraction import attachment_fingerprint
from studentflow.fixed_events import (
    FIXED_EVENTS_VERSION,
    KINDS,
    extract_fixed_events,
    merge_fixed_event_proposals,
    normalize_account,
    validate_fixed_event_for_save,
)
from studentflow.storage import Store

ZONE = ZoneInfo("Asia/Singapore")


def _local_time(value) -> str:
    return datetime.fromisoformat(value).astimezone(ZONE).strftime("%Y-%m-%d %H:%M") if value else ""


def parse_event_time(value: str) -> str | None:
    if not str(value or "").strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
        # Dates alone are not an exact appointment time.
        if "T" not in str(value) and " " not in str(value).strip():
            raise ValueError
        return (parsed.replace(tzinfo=ZONE) if parsed.utcoffset() is None else parsed).isoformat()
    except (ValueError, TypeError, OverflowError):
        raise ValueError("Use YYYY-MM-DD HH:MM for event dates and times, for example 2026-10-28 14:00.") from None


def event_review_rows(events: list[dict]) -> list[dict]:
    return [{"Include": False, "Event": event["title"], "Start (Singapore)": _local_time(event.get("start_at")),
             "End (Singapore)": _local_time(event.get("end_at")), "Type": event["kind"],
             "Location": event.get("location") or "", "In person": bool(event.get("in_person")),
             "Travel each way (min)": event.get("travel_minutes", 0),
             "Review notes": "; ".join(event.get("needs_clarification", []))}
            for event in events]


def reviewed_fixed_events(rows: list[dict], originals: list[dict]) -> list[dict]:
    if len(rows) != len(originals):
        raise ValueError("The event suggestions changed. Extract and review them again.")
    selected = []
    for row, original in zip(rows, originals, strict=True):
        if row.get("Include") is not True:
            continue
        event = deepcopy(original)
        event.update(title=row.get("Event"), start_at=parse_event_time(row.get("Start (Singapore)")),
                     end_at=parse_event_time(row.get("End (Singapore)")), location=row.get("Location") or "",
                     in_person=bool(row.get("In person")), travel_minutes=row.get("Travel each way (min)", 0))
        selected.append(validate_fixed_event_for_save(event))
    return selected


def _show_evidence(events: list[dict]) -> None:
    with st.expander("Supporting text for fixed events"):
        for index, event in enumerate(events, start=1):
            st.markdown(f"**{index}. {event['title']}**")
            for source in event.get("sources", []):
                label = source.get("source_filename") or "Email body"
                if source.get("source_page") is not None:
                    label += f" · Page {source['source_page']}"
                st.caption(f"{source.get('mail_subject') or 'Selected message'} · {label}")
                st.text(source.get("source_quote") or "")


def render_fixed_event_review(events: list[dict], store: Store, *, account: str, key: str) -> None:
    """Reusable proposal review for inbox or assistant; saves only after explicit review."""
    account = normalize_account(account)
    if any(event.get("account") != account for event in events):
        st.error("These event suggestions belong to another account. Extract them again.")
        return
    if not events:
        st.info("No fixed appointments were found in the selected sources.")
        return
    st.markdown("**Review fixed events**")
    st.caption("Check the source text and enter the exact start and end in Singapore time. "
               "Incomplete suggestions are not saved or used to block study time. "
               "Preparation time belongs in Tasks; these times are for attending the event.")
    _show_evidence(events)
    rows = st.data_editor(event_review_rows(events), hide_index=True, width="stretch",
                          num_rows="fixed", key=key + "_rows",
                          disabled=["Type", "Review notes"],
                          column_config={
                              "Include": st.column_config.CheckboxColumn("Include"),
                              "Travel each way (min)": st.column_config.NumberColumn(
                                  "Travel each way (min)", min_value=0, max_value=240, step=5),
                          })
    reviewed = st.checkbox("I have checked the event dates, times and supporting text.", key=key + "_reviewed")
    if st.button("Save selected fixed events", key=key + "_save", disabled=not reviewed):
        try:
            if not reviewed:
                raise ValueError("Review and confirm the event details before saving.")
            selected = reviewed_fixed_events(rows, events)
            if not selected:
                st.warning("Select at least one fixed event to save.")
                return
            before = {item["id"] for item in store.list_fixed_events(account)}
            identifiers = store.save_fixed_events(selected)
            reused = sum(identifier in before for identifier in set(identifiers))
            st.success(f"Saved {len(set(identifiers))} fixed event(s); {reused} already saved. "
                       "Existing edits were kept. Generate a new study plan to include these commitments.")
        except ValueError as exc:
            st.error(str(exc))


def render_fixed_event_extraction(
    selected_mails: list[dict], store: Store, data_dir: Path, api_key: str, model: str,
    *, account: str, ready: bool = True,
) -> None:
    """Render only; the selected messages have already been opened and account-verified."""
    del data_dir  # No new filesystem or credential access is needed for this explicit action.
    if not account:
        return
    account = normalize_account(account)
    st.markdown("**Exams, presentations and other fixed events**")
    st.caption("Extract appointments from the selected messages and readable attachments. "
               "Review exact times before saving them as commitments.")
    if not selected_mails:
        st.caption("Open selected messages first to extract their fixed events.")
        return
    if any(str(mail.get("gmail_account") or "").strip().casefold() != account for mail in selected_mails):
        st.warning("Reopen the selected messages with the currently connected account.")
        return
    signature = hashlib.sha256(json.dumps([
        FIXED_EVENTS_VERSION, account, model,
        [(mail.get("id"), mail.get("body"), mail.get("subject"), mail.get("sent_at"),
          attachment_fingerprint(mail.get("attachments", []))) for mail in selected_mails],
    ], ensure_ascii=False).encode()).hexdigest()[:24]
    key = "batch_fixed_events_" + signature
    consent = st.checkbox("Allow these selected messages and readable attachments to be sent to OpenAI "
                          "to suggest fixed events. Usage charges may apply.", key=key + "_consent")
    if st.button("Extract fixed events", key=key + "_extract",
                 disabled=not (ready and api_key and model and consent)):
        if not (ready and api_key and model and consent):
            st.warning("Connect AI and allow analysis of the selected sources first.")
            return
        events, warnings = [], []
        with st.spinner("Reading fixed event details..."):
            for mail in selected_mails:
                try:
                    result = extract_fixed_events(mail, api_key, model, account=account)
                    events.extend(result["events"])
                    warnings.extend(result["warnings"])
                except (RuntimeError, ValueError) as exc:
                    warnings.append(str(exc))
        st.session_state[key + "_result"] = {"events": merge_fixed_event_proposals(events),
                                             "warnings": list(dict.fromkeys(warnings))}
        for suffix in ("_rows", "_reviewed"):
            st.session_state.pop(key + "_review" + suffix, None)
    result = st.session_state.get(key + "_result")
    if result is not None:
        for warning in result["warnings"]:
            st.warning(warning)
        render_fixed_event_review(result["events"], store, account=account, key=key + "_review")


def render_fixed_events(store: Store, *, account: str) -> None:
    """Edit/remove only the active account's locally saved fixed appointments."""
    if not account:
        return
    account = normalize_account(account)
    st.markdown("**Saved fixed events**")
    events = store.list_fixed_events(account)
    if not events:
        st.caption("No fixed events saved. Extract and review appointments from Inbox or Assistant.")
        return
    st.dataframe([{ "Event": event["title"], "Start": _local_time(event["start_at"]),
                   "End": _local_time(event["end_at"]), "Location": event.get("location") or "",
                   "Travel each way (min)": event.get("travel_minutes", 0)} for event in events],
                 hide_index=True, width="stretch")
    scope = hashlib.sha256(account.encode()).hexdigest()[:16]
    with st.expander("Edit or remove a fixed event"):
        chosen = st.selectbox("Fixed event", [event["id"] for event in events],
                              format_func=lambda identifier: next(
                                  f"{event['title']} · {_local_time(event['start_at'])}"
                                  for event in events if event["id"] == identifier),
                              key="sf_fixed_event_choice_" + scope)
        event = next(event for event in events if event["id"] == chosen)
        key = "sf_fixed_event_" + chosen + "_" + str(event.get("updated_at"))
        with st.form(key + "_edit"):
            title = st.text_input("Event title", value=event["title"])
            kind = st.selectbox("Event type", sorted(KINDS), index=sorted(KINDS).index(event["kind"]))
            start = st.text_input("Start (Singapore)", value=_local_time(event["start_at"]))
            end = st.text_input("End (Singapore)", value=_local_time(event["end_at"]))
            location = st.text_input("Location", value=event.get("location") or "")
            in_person = st.checkbox("In person", value=bool(event.get("in_person")))
            travel = st.number_input("Travel each way (min)", min_value=0, max_value=240,
                                     value=event.get("travel_minutes", 0), step=5)
            reviewed = st.checkbox("I have reviewed these event details.")
            if st.form_submit_button("Save event changes"):
                try:
                    if not reviewed:
                        raise ValueError("Review and confirm the event details before saving.")
                    store.update_fixed_event(chosen, {
                        "title": title, "kind": kind, "start_at": parse_event_time(start),
                        "end_at": parse_event_time(end), "location": location,
                        "in_person": in_person, "travel_minutes": int(travel),
                    }, account=account)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        remove = st.checkbox("Remove this fixed event from StudentFlow.", key=key + "_remove_check")
        if st.button("Remove fixed event", key=key + "_remove", disabled=not remove):
            store.delete_fixed_event(chosen, account=account)
            st.rerun()
        st.caption("After changes, generate a new study plan and sync again if you use Calendar.")
