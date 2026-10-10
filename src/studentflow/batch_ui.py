"""Explicit multi-message extraction and source-bound task review."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from studentflow.extraction import extract_tasks
from studentflow.gmail import GmailAuthError, GmailError, connect
from studentflow.ingestion import parse_eml_bytes
from studentflow.storage import Store

ZONE = ZoneInfo("Asia/Singapore")


def clear_batch_state():
    """Drop account-scoped messages, proposals and consent on account changes."""
    for key in list(st.session_state):
        if key.startswith("batch_"):
            del st.session_state[key]


def extraction_key(account: str, mail: dict, model: str) -> str:
    payload = [account.casefold(), mail["body"], mail["subject"], mail.get("sent_at"), model]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def run_batch(
    message_ids, credential_path, token_path, profile, mails, cache, candidates,
    api_key, model, *, reanalyze=False, progress=None,
):
    """Verify the account, fetch sequentially, then analyze at most 3 mails at once.

    Cached results are session-local and account/content/model scoped. Gmail is
    never shared between workers. Auth failures stop before any new model call.
    Individual message/model failures preserve other results for focused retries.
    """
    account = str((profile or {}).get("email_address", "")).casefold()
    if not account:
        raise GmailAuthError("Reconnect Gmail before analyzing messages.")
    identifiers = list(dict.fromkeys(message_ids))
    if not identifiers:
        return []
    results = {}
    pending = {}
    with closing(connect(credential_path, token_path)) as mailbox:
        if mailbox.profile() != profile:
            raise GmailAuthError("The signed-in account changed. Reconnect Gmail to continue.")
        opened = {
            mail.get("gmail_id"): mail for mail in mails.values()
            if mail.get("gmail_account") == account and mail.get("provider") == "gmail"
        }
        for number, message_id in enumerate(identifiers, start=1):
            if progress:
                progress(0.4 * (number - 1) / len(identifiers), f"Opening message {number} of {len(identifiers)}...")
            try:
                mail = opened.get(message_id)
                if mail is None:
                    raw = mailbox.fetch_eml(message_id)
                    mail = parse_eml_bytes(raw, "Gmail / " + message_id)
                    mail.update({
                        "provider": "gmail", "gmail_id": message_id, "gmail_account": account,
                        "imported_at": datetime.now(ZONE).isoformat(),
                    })
                    mails[mail["id"]] = mail
                key = extraction_key(account, mail, model)
                entry = {"gmail_id": message_id, "mail_id": mail["id"], "cache_key": key}
                if key in cache and not reanalyze:
                    entry.update(result=cache[key], cached=True)
                    results[message_id] = entry
                    candidates[mail["id"]] = cache[key]
                else:
                    pending[message_id] = (mail, entry)
            except GmailAuthError:
                raise
            except Exception:
                results[message_id] = {
                    "gmail_id": message_id,
                    "error": "This message could not be opened. Select it again to retry.",
                }

    completed = len(results)
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(
                extract_tasks, mail["body"], mail["subject"], mail.get("sent_at"), api_key, model,
            ): (message_id, mail, entry)
            for message_id, (mail, entry) in pending.items()
        }
        for future in as_completed(futures):
            message_id, mail, entry = futures[future]
            try:
                result = future.result()
                if not isinstance(result, dict) or not isinstance(result.get("tasks"), list):
                    raise ValueError("Invalid extraction response")
                cache[entry["cache_key"]] = result
                candidates[mail["id"]] = result
                entry.update(result=result, cached=False)
            except Exception:
                entry["error"] = "Task extraction failed. Check the AI connection and select this message to retry."
            results[message_id] = entry
            completed += 1
            if progress:
                progress(0.4 + 0.6 * completed / len(identifiers), f"Analyzed {completed} of {len(identifiers)} messages.")
    if progress:
        progress(1.0, "Selected messages processed.")
    return [results[message_id] for message_id in identifiers]


def review_rows(results: list[dict], mails: dict) -> tuple[list[dict], dict]:
    rows, sources = [], {}
    for entry in results:
        if "result" not in entry:
            continue
        mail = mails.get(entry["mail_id"])
        if mail is None:
            continue
        for index, task in enumerate(entry["result"]["tasks"]):
            row_id = f"{entry['cache_key']}:{index}:{entry['gmail_id']}"
            due = task.get("due_at")
            due_text = datetime.fromisoformat(due).astimezone(ZONE).strftime("%Y-%m-%d %H:%M") if due else ""
            rows.append({
                "Include": False, "Task": task["title"], "Deadline": due_text,
                "Study time (min)": int(task.get("estimated_minutes") or 0),
                "Supporting text": task.get("source_quote", ""),
                "Source message": mail["subject"] or "Untitled message",
                "Review notes": " ".join(task.get("needs_clarification", [])), "_row_id": row_id,
            })
            sources[row_id] = mail
    return rows, sources


def validate_selected_rows(rows: list[dict], sources: dict, reviewed: bool) -> list[dict]:
    """Validate every selected proposal before the caller saves any record."""
    if not reviewed:
        raise ValueError("Confirm that the selected tasks and supporting text have been reviewed.")
    selected = [row for row in rows if row.get("Include") is True]
    if not selected:
        raise ValueError("Select at least one task in the Include column.")
    records, seen, task_identities = [], set(), set()
    for row in selected:
        row_id = row.get("_row_id")
        if row_id not in sources or row_id in seen:
            raise ValueError("The task source changed. Run extraction again before saving.")
        seen.add(row_id)
        mail = sources[row_id]
        title = str(row.get("Task") or "").strip()
        if not title:
            raise ValueError("Enter a name for every selected task.")
        identity = (mail["id"], title)
        if identity in task_identities:
            raise ValueError("Give selected tasks from the same message distinct names before saving.")
        task_identities.add(identity)
        quote = str(row.get("Supporting text") or "").strip()
        if not quote or quote not in mail["body"]:
            raise ValueError(f"Supporting text for '{title}' must match its source message.")
        due_text = str(row.get("Deadline") or "").strip()
        due = None
        if due_text:
            try:
                due = datetime.strptime(due_text, "%Y-%m-%d %H:%M").replace(tzinfo=ZONE).isoformat()
            except ValueError:
                raise ValueError(f"Enter the deadline for '{title}' as YYYY-MM-DD HH:MM, or leave it blank.") from None
        minutes = row.get("Study time (min)")
        if minutes is None:
            minutes = 0
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not 0 <= minutes <= 10000 or int(minutes) != minutes:
            raise ValueError(f"Enter a whole number of minutes from 0 to 10000 for '{title}'.")
        minutes = int(minutes) or None
        records.append({
            "mail_id": mail["id"], "source_name": "Gmail", "title": title,
            "due_at": due, "estimated_minutes": minutes,
            "status": "confirmed" if due and minutes else "pending", "source_quote": quote,
            "mail_subject": mail["subject"], "mail_sent_at": mail.get("sent_at"),
            "mail_imported_at": mail.get("imported_at"),
        })
    return records


def _select_messages(identifiers):
    st.session_state.batch_selected = identifiers
    st.session_state.batch_consent = False


def _selection_changed():
    st.session_state.batch_consent = False


def _authentication_failed():
    clear_batch_state()
    st.session_state.mails = {}
    st.session_state.candidates = {}
    st.session_state.documents = []
    for key in (
        "gmail_profile", "gmail_headers", "gmail_next_page_token", "gmail_page_tokens",
        "gmail_selected_message", "active_mail", "review_message",
    ):
        st.session_state.pop(key, None)
    st.session_state.gmail_auth_required = True
    st.session_state.gmail_restore_attempted = True
    st.session_state.gmail_connection_error = "Reconnect Gmail to verify the account before analyzing messages."
    st.rerun()


def render_batch_extraction(
    headers: list[dict], credential_path: Path, token_path: Path, store: Store,
    api_key: str, model: str, ready: bool,
) -> None:
    with st.expander("Batch task extraction"):
        st.caption("Select messages, extract task suggestions, then review and save the tasks you want to keep.")
        by_id = {header["id"]: header for header in headers}
        identifiers = list(by_id)
        old_selection = st.session_state.get("batch_selected", [])
        if any(identifier not in by_id for identifier in old_selection):
            _select_messages([identifier for identifier in old_selection if identifier in by_id])
        selected = st.multiselect(
            "Messages to analyze", identifiers,
            format_func=lambda identifier: f"{by_id[identifier]['subject'] or 'Untitled message'} — {by_id[identifier].get('sender', '')}",
            key="batch_selected", on_change=_selection_changed,
        )
        left, right = st.columns(2)
        left.button("Select all loaded", key="batch_all", disabled=not identifiers,
                    on_click=_select_messages, args=(identifiers,), width="stretch")
        right.button("Clear selection", key="batch_clear", disabled=not selected,
                     on_click=_select_messages, args=([],), width="stretch")
        st.caption(f"{len(selected)} message(s) selected.")
        if not api_key or not model:
            st.info("Add your OpenAI API key in Settings to extract tasks.")
        consent = st.checkbox(
            "Allow the selected messages' subjects, text and dates to be sent to OpenAI for task extraction. Usage charges may apply.",
            key="batch_consent",
        )
        reanalyze = st.checkbox(
            "Analyze completed messages again", key="batch_reanalyze",
            help="By default, results from this session are reused for the same account, message and model. Repeating analysis may incur additional charges.",
        )
        if st.button("Extract selected messages", key="batch_extract", type="primary",
                     disabled=not (ready and api_key and model and consent and selected)):
            progress = st.progress(0.0, text="Opening selected messages...")
            try:
                cache = st.session_state.setdefault("batch_cache", {})
                results = run_batch(
                    selected, credential_path, token_path, st.session_state.get("gmail_profile"),
                    st.session_state.mails, cache, st.session_state.candidates, api_key, model,
                    reanalyze=reanalyze, progress=lambda value, text: progress.progress(value, text=text),
                )
                st.session_state.batch_results = results
                st.session_state.batch_review_version = st.session_state.get("batch_review_version", 0) + 1
            except GmailAuthError:
                _authentication_failed()
            except GmailError:
                st.error("Gmail could not be reached. Try again when your connection is available.")
            except Exception:
                st.error("The selected messages could not be processed. Try again.")
            finally:
                progress.empty()

        results = st.session_state.get("batch_results", [])
        if not results:
            return
        for entry in results:
            name = by_id.get(entry["gmail_id"], {}).get("subject") or "Message"
            if "error" in entry:
                st.error(f"{name}: {entry['error']}")
            else:
                count = len(entry["result"]["tasks"])
                suffix = " Previous result reused." if entry.get("cached") else ""
                st.caption(f"{name}: {count} task suggestion(s).{suffix}")
                for warning in entry["result"].get("warnings", []):
                    st.caption(warning)
        rows, sources = review_rows(results, st.session_state.mails)
        if not rows:
            return
        st.write("**Review task suggestions**")
        st.caption("Check Include for each task to save. Edit names, deadlines and study time. Deadlines use Singapore time; leave unknown dates blank and unknown study time as 0.")
        version = st.session_state.get("batch_review_version", 0)
        with st.form(f"batch_review_form_{version}"):
            edited = st.data_editor(
                rows, hide_index=True, num_rows="fixed", width="stretch",
                disabled=["Source message", "Review notes", "_row_id"],
                column_config={
                    "_row_id": None,
                    "Include": st.column_config.CheckboxColumn("Include", default=False),
                    "Deadline": st.column_config.TextColumn("Deadline", help="Singapore time: YYYY-MM-DD HH:MM"),
                    "Study time (min)": st.column_config.NumberColumn(min_value=0, max_value=10000, step=15),
                    "Supporting text": st.column_config.TextColumn(width="large"),
                    "Review notes": st.column_config.TextColumn(width="large"),
                }, key=f"batch_editor_{version}",
            )
            reviewed = st.checkbox(
                "I have reviewed the selected tasks and their supporting text.", key=f"batch_reviewed_{version}",
            )
            if st.form_submit_button("Save selected tasks", type="primary"):
                try:
                    records = validate_selected_rows(edited, sources, reviewed)
                    store.save_tasks(records)
                    pending = sum(record["status"] == "pending" for record in records)
                    st.success(f"Saved {len(records)} task(s)." + (f" {pending} marked as Needs details." if pending else ""))
                except ValueError as exc:
                    st.error(str(exc))
                except Exception:
                    st.error("The selected tasks could not be saved. Try again.")
