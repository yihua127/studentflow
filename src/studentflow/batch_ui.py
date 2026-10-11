"""Explicit multi-message extraction and source-bound task review."""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from studentflow.attachment_library import AttachmentLibrary
from studentflow.extraction import (
    SYSTEM_PROMPT,
    attachment_fingerprint,
    evidence_text,
    extract_tasks,
)
from studentflow.extraction_cache import ExtractionCache
from studentflow.gmail import GmailAuthError, GmailError, connect
from studentflow.ingestion import parse_eml_bytes
from studentflow.source_extraction import analyze_sources
from studentflow.storage import Store
from studentflow.task_dedup import merge_duplicate_tasks, merge_sources, source_evidence
from studentflow.task_reconciliation import reconcile_tasks

ZONE = ZoneInfo("Asia/Singapore")


def clear_batch_state():
    """Drop account-scoped messages, proposals and consent on account changes."""
    for key in list(st.session_state):
        if key.startswith("batch_"):
            del st.session_state[key]


def extraction_key(account: str, mail: dict, model: str) -> str:
    payload = ["source-pipeline-v1", SYSTEM_PROMPT, account.casefold(),
               mail["body"], mail["subject"], mail.get("sent_at"), model,
               attachment_fingerprint(mail.get("attachments", []))]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def fetch_selected_messages(
    message_ids, credential_path, token_path, profile, mails, *, progress=None,
):
    """Open selected messages after checking the account, without invoking AI."""
    account = str((profile or {}).get("email_address", "")).casefold()
    if not account:
        raise GmailAuthError("Reconnect Gmail before opening messages.")
    identifiers = list(dict.fromkeys(message_ids))
    if not identifiers:
        return []
    results = []
    with closing(connect(credential_path, token_path)) as mailbox:
        if mailbox.profile() != profile:
            raise GmailAuthError("The signed-in account changed. Reconnect Gmail to continue.")
        opened = {
            mail.get("gmail_id"): mail for mail in mails.values()
            if mail.get("gmail_account") == account and mail.get("provider") == "gmail"
        }
        for number, message_id in enumerate(identifiers, start=1):
            if progress:
                progress((number - 1) / len(identifiers), f"Opening message {number} of {len(identifiers)}...")
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
                results.append({"gmail_id": message_id, "mail_id": mail["id"]})
            except GmailAuthError:
                raise
            except Exception:
                results.append({
                    "gmail_id": message_id,
                    "error": "This message could not be opened. Select it again to retry.",
                })
    if progress:
        progress(1.0, "Selected messages opened.")
    return results


def run_batch(
    message_ids, credential_path, token_path, profile, mails, cache, candidates,
    api_key, model, *, reanalyze=False, progress=None, source_cache=None,
    references=None, reconciler=None, reconciliation_cache=None,
):
    """Verify access, analyze unique sources, then resolve related suggestions.

    Successful source analysis can persist across sessions, scoped by account,
    content and model. Gmail is never shared between workers. Auth failures stop before any new model call.
    Individual message/model failures preserve other results for focused retries.
    """
    opened = fetch_selected_messages(
        message_ids, credential_path, token_path, profile, mails,
        progress=(lambda value, text: progress(0.4 * value, text)) if progress else None,
    )
    if not opened:
        return []
    account = str(profile["email_address"]).casefold()
    identifiers = [entry["gmail_id"] for entry in opened]
    results, pending = {}, {}
    for entry in opened:
        message_id = entry["gmail_id"]
        if "error" in entry:
            results[message_id] = entry
            continue
        mail = mails[entry["mail_id"]]
        key = extraction_key(account, mail, model)
        entry["cache_key"] = key
        # Resolve persistent sources together so a fresh analysis of one copy
        # cannot leave another copy using older session-only item identities.
        if source_cache is None and key in cache and not reanalyze:
            entry.update(result=cache[key], cached=True)
            results[message_id] = entry
            candidates[mail["id"]] = cache[key]
        else:
            pending[message_id] = (mail, entry)

    analyzed = analyze_sources(
        [mail for mail, _entry in pending.values()], account, api_key, model,
        extractor=extract_tasks, cache=source_cache, reanalyze=reanalyze,
        progress=(lambda value, text: progress(0.4 + 0.5 * value, text)) if progress else None,
    ) if pending else {}
    for message_id, (mail, entry) in pending.items():
        outcome = analyzed.get(mail["id"], {})
        if "result" in outcome:
            result = outcome["result"]
            if not outcome.get("partial"):
                cache[entry["cache_key"]] = result
            candidates[mail["id"]] = result
            entry.update(result=result, cached=outcome.get("cached", False))
        else:
            entry["error"] = outcome.get("error") or "Task extraction failed. Select this message to retry."
        results[message_id] = entry
    ordered = [results[message_id] for message_id in identifiers]
    if reconciler is not None:
        if progress:
            progress(0.95, "Checking related task suggestions...")
        ordered = reconcile_batch_results(
            ordered, mails, references or [], api_key, model,
            reconciler=reconciler, cache=reconciliation_cache,
        )
    if progress:
        progress(1.0, "Selected messages processed.")
    return ordered


def reconcile_batch_results(results, mails, references, api_key, model, *, reconciler, cache=None):
    """Resolve source-linked ambiguity only within an explicit extraction action."""
    proposals = []
    for entry in results:
        mail = mails.get(entry.get("mail_id"))
        if mail is None:
            continue
        for task in entry.get("result", {}).get("tasks", []):
            proposals.append({**task, "sources": [source_evidence(task, mail)]})
    payload = ["semantic-review-v1", model, proposals, references]
    key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    verdict = cache.get(key) if cache is not None else None
    if verdict is None:
        try:
            verdict = reconciler(proposals, references, api_key, model)
        except Exception:
            verdict = {"links": {}, "warnings": [
                "Some related suggestions need review. Check for repeated tasks before saving."
            ]}
        if cache is not None and not verdict.get("warnings"):
            cache[key] = verdict
    output = deepcopy(results)
    for entry in output:
        if "result" not in entry:
            continue
        for task in entry["result"]["tasks"]:
            linked = verdict.get("links", {}).get(task.get("_source_item_id"))
            if linked:
                task["_linked_sources"] = linked
        entry["result"].setdefault("warnings", []).extend(verdict.get("warnings", []))
    return output


def review_rows(results: list[dict], mails: dict) -> tuple[list[dict], dict]:
    proposals = []
    for entry in results:
        if "result" not in entry:
            continue
        mail = mails.get(entry["mail_id"])
        if mail is None:
            continue
        for index, task in enumerate(entry["result"]["tasks"]):
            row_id = f"{entry['cache_key']}:{index}:{entry['gmail_id']}"
            proposals.append({
                **task, "_row_id": row_id, "_primary_mail": mail,
                "sources": merge_sources([source_evidence(task, mail)], task.get("_linked_sources", [])),
            })
    rows, sources = [], {}
    for task in merge_duplicate_tasks(proposals):
        row_id, mail = task["_row_id"], task["_primary_mail"]
        due = task.get("due_at")
        due_text = datetime.fromisoformat(due).astimezone(ZONE).strftime("%Y-%m-%d %H:%M") if due else ""
        evidence = task["sources"]
        notes = list(task.get("needs_clarification", []))
        count = task.get("source_duplicate_count", 1)
        if count > 1:
            notes.insert(0, f"Duplicate mentions combined ({count}).")
        rows.append({
            "Include": False, "Task": task["title"], "Deadline": due_text,
            "Study time (min)": int(task.get("estimated_minutes") or 0),
            "Supporting text": task.get("source_quote", ""),
            "Source message": "; ".join(dict.fromkeys(
                item.get("mail_subject") or "Untitled message" for item in evidence)),
            "Source file": "; ".join(dict.fromkeys(
                item.get("source_filename") or "Message text" for item in evidence)),
            "Review notes": " ".join(notes), "_row_id": row_id,
        })
        sources[row_id] = {
            **mail, "_source_attachment_id": task.get("source_attachment_id"),
            "_source_filename": task.get("source_filename"), "_task_sources": evidence,
        }
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
        attachment_id = mail.get("_source_attachment_id")
        if not quote or quote not in evidence_text(mail, attachment_id):
            raise ValueError(f"Supporting text for '{title}' must match its source message or attachment.")
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
            "gmail_account": mail.get("gmail_account"),
            "due_at": due, "estimated_minutes": minutes,
            "status": "confirmed" if due and minutes else "pending", "source_quote": quote,
            "mail_subject": mail["subject"], "mail_sent_at": mail.get("sent_at"),
            "mail_imported_at": mail.get("imported_at"),
            "source_attachment_id": attachment_id,
            "source_filename": mail.get("_source_filename"),
            "sources": merge_sources(
                [source_evidence({"source_quote": quote, "source_attachment_id": attachment_id,
                                  "source_filename": mail.get("_source_filename")}, mail)],
                mail.get("_task_sources", []),
            ),
        })
    return records


def _review_version(rows: list[dict]) -> int:
    """Invalidate row-index widget edits when grouping changes, including old sessions."""
    signature = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    previous = st.session_state.get("batch_review_signature")
    schema_changed = st.session_state.get("batch_review_schema") != "sources-v1"
    old_keys = [key for key in st.session_state
                if key.startswith(("batch_editor_", "batch_reviewed_"))]
    if (schema_changed and old_keys) or (previous is not None and previous != signature):
        for key in old_keys:
            del st.session_state[key]
        st.session_state.batch_review_version = st.session_state.get("batch_review_version", 0) + 1
    st.session_state.batch_review_schema = "sources-v1"
    st.session_state.batch_review_signature = signature
    return st.session_state.get("batch_review_version", 0)


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
    """Render the shared selection and extraction controls inside the inbox section."""
    if not headers:
        st.info("Refresh the inbox to choose messages.")
        return
    st.caption("Select one or more messages to view or extract tasks, then review and save the tasks you want to keep.")
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
    if st.button("View selected messages", key="batch_view", disabled=not (ready and selected)):
        with st.spinner("Opening selected messages..."):
            try:
                opened = fetch_selected_messages(
                    selected, credential_path, token_path, st.session_state.get("gmail_profile"),
                    st.session_state.mails,
                )
                successful = [entry for entry in opened if "mail_id" in entry]
                if successful:
                    st.session_state.active_mail = successful[0]["mail_id"]
                    st.session_state.pop("review_message", None)
                    st.success(f"{len(successful)} message(s) ready in Message review below.")
                for entry in opened:
                    if "error" in entry:
                        name = by_id[entry["gmail_id"]].get("subject") or "Message"
                        st.error(f"{name}: {entry['error']}")
            except GmailAuthError:
                _authentication_failed()
            except GmailError:
                st.error("Gmail could not be reached. Try again when your connection is available.")
            except Exception:
                st.error("The selected messages could not be opened. Try again.")
    if not api_key or not model:
        st.info("Add your OpenAI API key in Settings to extract tasks.")
    # Reconfirm the scope when upgrading from selected-message-only analysis.
    if st.session_state.get("batch_consent_scope") != "sources-and-related-tasks-v2":
        st.session_state.batch_consent = False
        st.session_state.batch_consent_scope = "sources-and-related-tasks-v2"
    consent = st.checkbox(
        "Allow selected messages, readable attachments and related saved task details to be sent to OpenAI to extract tasks and check for duplicates. Usage charges may apply.",
        key="batch_consent",
    )
    reanalyze = st.checkbox(
        "Analyze completed messages again", key="batch_reanalyze",
        help="Saved source analysis is reused by default. Select this to analyze the chosen messages and attachments again; usage charges may apply.",
    )
    if st.button("Extract tasks", key="batch_extract", type="primary",
                 disabled=not (ready and api_key and model and consent and selected)):
        progress = st.progress(0.0, text="Opening selected messages...")
        try:
            cache = st.session_state.setdefault("batch_cache", {})
            results = run_batch(
                selected, credential_path, token_path, st.session_state.get("gmail_profile"),
                st.session_state.mails, cache, st.session_state.candidates, api_key, model,
                reanalyze=reanalyze, progress=lambda value, text: progress.progress(value, text=text),
                source_cache=ExtractionCache(store.path), references=store.task_references(),
                reconciler=reconcile_tasks,
                reconciliation_cache=st.session_state.setdefault("batch_reconciliation_cache", {}),
            )
            st.session_state.batch_results = results
            st.session_state.batch_results_version = "source-pipeline-v1"
            try:
                library = AttachmentLibrary(store.path)
                for entry in results:
                    if entry.get("result", {}).get("tasks"):
                        library.save_for_message(st.session_state.mails[entry["mail_id"]])
            except Exception:
                st.warning("Task suggestions are ready, but some attachments could not be kept. Save the tasks to retry saving their attachments.")
            st.session_state.batch_review_version = st.session_state.get("batch_review_version", 0) + 1
            successful = [entry for entry in results if "mail_id" in entry]
            if successful:
                st.session_state.active_mail = successful[0]["mail_id"]
                st.session_state.pop("review_message", None)
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
    if any(not task.get("_source_item_id") for entry in results
           for task in entry.get("result", {}).get("tasks", [])):
        st.info("Select Extract tasks to refresh these suggestions with duplicate checking.")
    warnings = []
    for entry in results:
        name = by_id.get(entry["gmail_id"], {}).get("subject") or "Message"
        if "error" in entry:
            st.error(f"{name}: {entry['error']}")
        else:
            count = len(entry["result"]["tasks"])
            suffix = " Previous result reused." if entry.get("cached") else ""
            st.caption(f"{name}: {count} task suggestion(s).{suffix}")
            warnings.extend(entry["result"].get("warnings", []))
    for warning in dict.fromkeys(warnings):
        st.caption(warning)
    rows, sources = review_rows(results, st.session_state.mails)
    if not rows:
        return
    st.write("**Review task suggestions**")
    total = sum(len(entry.get("result", {}).get("tasks", [])) for entry in results)
    if total > len(rows):
        st.caption(f"{total - len(rows)} duplicate mention(s) combined. {len(rows)} distinct task suggestion(s) to review.")
    st.caption("Check Include for each task to save. Edit names, deadlines and study time. Deadlines use Singapore time; leave unknown dates blank and unknown study time as 0.")
    st.caption("Confirmed requires a deadline and estimated study time greater than 0. Otherwise the task is saved as Needs details; you can complete or edit it on the Tasks page.")
    version = _review_version(rows)
    with st.form(f"batch_review_form_{version}"):
        edited = st.data_editor(
            rows, hide_index=True, num_rows="fixed", width="stretch",
            disabled=["Source message", "Source file", "Review notes", "_row_id"],
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
                previous_ids = {task["id"] for task in store.tasks()}
                identifiers = set(store.save_tasks(records))
                try:
                    library = AttachmentLibrary(store.path)
                    mail_ids = {source["mail_id"] for record in records
                                for source in record.get("sources", [])}
                    mail_ids.update(record["mail_id"] for record in records)
                    for mail_id in mail_ids:
                        if mail_id in st.session_state.mails:
                            library.save_for_message(st.session_state.mails[mail_id])
                except Exception:
                    st.warning("Tasks were saved, but some attachments could not be kept. Save again to retry.")
                pending = sum(task["status"] == "pending" for task in store.tasks()
                              if task["id"] in identifiers)
                kept = len(identifiers & previous_ids)
                st.success(f"Saved {len(identifiers)} task(s)."
                           + (f" {pending} marked as Needs details." if pending else "")
                           + (f" {kept} already saved; existing details and progress kept. Edit these on the Tasks page." if kept else ""))
            except ValueError as exc:
                st.error(str(exc))
            except Exception:
                st.error("The selected tasks could not be saved. Try again.")
