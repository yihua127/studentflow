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
from studentflow.effort_batch import apply_effort_suggestions, estimate_review_efforts
from studentflow.effort_review import build_effort_review
from studentflow.extraction import (
    REVIEW_NOTICE,
    SYSTEM_PROMPT,
    attachment_fingerprint,
    evidence_text,
    extract_tasks,
)
from studentflow.extraction_cache import ExtractionCache
from studentflow.fixed_event_cache import FixedEventCache, analyze_fixed_event_messages
from studentflow.fixed_events_ui import render_fixed_event_review
from studentflow.gmail import GmailAuthError, GmailError, connect
from studentflow.inbox_agents import run_inbox_agents
from studentflow.inbox_cache import UNIFIED_VERSION, InboxAnalysisCache, analysis_cache_key
from studentflow.ingestion import parse_eml_bytes
from studentflow.reply_handoff import clear_reply_handoff, queue_reply_handoff
from studentflow.source_extraction import PIPELINE_VERSION, analyze_sources
from studentflow.storage import Store
from studentflow.task_dedup import merge_duplicate_tasks, merge_sources, source_evidence
from studentflow.task_reconciliation import RECONCILIATION_VERSION, reconcile_tasks

ZONE = ZoneInfo("Asia/Singapore")


def estimate_task_efforts(*args, **kwargs):
    # Loading or reviewing extraction results must never initialize a model client.
    from studentflow.effort_estimation import estimate_task_efforts as estimate

    return estimate(*args, **kwargs)


def clear_batch_state():
    """Drop account-scoped messages, proposals and consent on account changes."""
    clear_reply_handoff(st.session_state)
    for key in list(st.session_state):
        if key.startswith("batch_"):
            del st.session_state[key]


def extraction_key(account: str, mail: dict, model: str) -> str:
    payload = [PIPELINE_VERSION, SYSTEM_PROMPT, account.casefold(),
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
            except Exception:  # noqa: BLE001 - report per-message failure without private provider details
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
    opened_messages=None,
):
    """Verify access, analyze unique sources, then resolve related suggestions.

    Successful source analysis can persist across sessions, scoped by account,
    content and model. Gmail is never shared between workers. Auth failures stop before any new model call.
    Individual message/model failures preserve other results for focused retries.
    """
    opened = opened_messages if opened_messages is not None else fetch_selected_messages(
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
            entry.update(result=result, cached=outcome.get("cached", False), partial=bool(outcome.get("partial")))
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


def run_unified_batch(
    message_ids, credential_path, token_path, profile, mails, cache, candidates,
    api_key, model, *, store, reanalyze=False, suggest_effort=True, progress=None,
    effort_cache=None, reconciliation_cache=None,
):
    """Open once, then use specialist tools for both kinds of source-backed proposals."""
    opened = fetch_selected_messages(message_ids, credential_path, token_path, profile, mails,
                                     progress=progress)
    account = str((profile or {}).get("email_address") or "").strip().casefold()
    selected_mails = [mails[entry["mail_id"]] for entry in opened if "mail_id" in entry]
    references = store.task_references()
    key = analysis_cache_key(account, selected_mails, model, references, suggest_effort)
    all_opened = bool(opened) and len(selected_mails) == len(opened)
    full_cache, previous, cache_warnings = None, None, []
    try:
        full_cache = InboxAnalysisCache(store.path)
        previous = full_cache.get(key) if all_opened and not reanalyze else None
    except Exception:  # noqa: BLE001 - optional cache failure must not block analysis
        full_cache = None
        cache_warnings.append("Previous complete analysis could not be loaded. Source analysis will be used where available.")
    if previous is not None:
        previous["cached"] = True
        for entry in previous["task_result"]["results"]:
            entry["cached"] = True
            if "result" in entry:
                candidates[entry["mail_id"]] = entry["result"]
        return previous
    if not selected_mails:
        return {"status": "failed", "task_result": {"results": opened, "effort": {}},
                "event_result": None, "events": [], "usage": {},
                "warnings": ["No selected messages could be opened. Try again."], "cached": False}

    def tasks():
        if progress:
            progress(0.35, "Analyzing flexible tasks and preparation work...")
        try:
            source_cache = ExtractionCache(store.path)
        except Exception:  # noqa: BLE001 - caching is optional, analysis can still run
            source_cache = None
            cache_warnings.append("Task source analysis could not be cached on this device.")
        results = run_batch(
            message_ids, credential_path, token_path, profile, mails, cache, candidates,
            api_key, model, reanalyze=reanalyze, source_cache=source_cache,
            references=references, reconciler=reconcile_tasks,
            reconciliation_cache=reconciliation_cache, opened_messages=opened,
        )
        effort = {"estimates": {}, "warnings": []}
        if suggest_effort:
            rows, sources = review_rows(results, mails)
            effort = estimate_review_efforts(rows, sources, api_key, model,
                                            estimator=estimate_task_efforts, cache=effort_cache)
        complete = all("error" not in item and not item.get("partial") for item in results)
        return {"results": results, "effort": effort, "status": "completed" if complete else "partial"}

    def events():
        if progress:
            progress(0.7, "Analyzing fixed exams, presentations and appointments...")
        try:
            event_cache = FixedEventCache(store.path)
        except Exception:  # noqa: BLE001 - retain extracted events if local caching fails
            event_cache = None
            cache_warnings.append("Fixed-event analysis could not be cached on this device.")
        result = analyze_fixed_event_messages(selected_mails, account, api_key, model,
                                             event_cache, reanalyze=reanalyze)
        return result | {"status": "completed" if result["complete"] else "partial"}

    outcome = run_inbox_agents(tasks, events, api_key, model)
    outcome["cached"] = False
    task_result, event_result = outcome.get("task_result") or {}, outcome.get("event_result") or {}
    task_warnings = [warning for item in task_result.get("results", [])
                     for warning in item.get("result", {}).get("warnings", [])
                     if warning != REVIEW_NOTICE]
    if (all_opened and full_cache is not None and outcome["status"] == "completed" and event_result.get("complete")
            and task_result.get("status") == "completed" and not task_warnings
            and not event_result.get("warnings") and not task_result.get("effort", {}).get("warnings")):
        try:
            full_cache.put(key, outcome)
        except Exception:  # noqa: BLE001 - retain successful paid analysis for review
            cache_warnings.append("Analysis is ready for review, but could not be cached for reuse after reopening.")
    outcome.setdefault("warnings", []).extend(cache_warnings)
    if progress:
        progress(1.0, "Analysis ready for review.")
    return outcome


def reconcile_batch_results(results, mails, references, api_key, model, *, reconciler, cache=None):
    """Resolve source-linked ambiguity only within an explicit extraction action."""
    proposals = []
    for entry in results:
        mail = mails.get(entry.get("mail_id"))
        if mail is None:
            continue
        for task in entry.get("result", {}).get("tasks", []):
            proposals.append({**task, "sources": [source_evidence(task, mail)]})
    payload = [RECONCILIATION_VERSION, model, proposals, references]
    key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    verdict = cache.get(key) if cache is not None else None
    if verdict is None:
        try:
            verdict = reconciler(proposals, references, api_key, model)
        except Exception:  # noqa: BLE001 - failed comparison must not discard valid proposals
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
            "_stated_minutes": task.get("estimated_minutes"),
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
        if "_effort_suggestion" in mail:
            suggestion = mail["_effort_suggestion"]
            records[-1]["effort_review"] = build_effort_review(
                suggestion, minutes, model=suggestion.get("model", ""),
            )
    return records


def _review_version(rows: list[dict]) -> int:
    """Invalidate row-index widget edits when grouping changes, including old sessions."""
    signature = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    previous = st.session_state.get("batch_review_signature")
    schema_changed = st.session_state.get("batch_review_schema") != "effort-review-v1"
    old_keys = [key for key in st.session_state
                if key.startswith(("batch_editor_", "batch_reviewed_"))]
    if (schema_changed and old_keys) or (previous is not None and previous != signature):
        for key in old_keys:
            del st.session_state[key]
        st.session_state.batch_review_version = st.session_state.get("batch_review_version", 0) + 1
    st.session_state.batch_review_schema = "effort-review-v1"
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
    st.caption("Analyze selected messages once to find flexible study tasks and fixed appointments. "
               "Messages and readable attachments open automatically; review both kinds before saving.")
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
    if st.button("Reply with assistant", key="batch_reply", disabled=not (ready and len(selected) == 1)):
        with st.spinner("Opening the selected email for your reply..."):
            try:
                opened = fetch_selected_messages(
                    selected, credential_path, token_path, st.session_state.get("gmail_profile"),
                    st.session_state.mails,
                )
                if len(opened) != 1 or "mail_id" not in opened[0]:
                    st.error("This email could not be opened. Select it again to retry.")
                else:
                    queue_reply_handoff(
                        st.session_state,
                        st.session_state.get("gmail_profile", {}).get("email_address", ""),
                        opened[0]["mail_id"], st.session_state.mails,
                    )
                    st.rerun()
            except GmailAuthError:
                _authentication_failed()
            except GmailError:
                st.error("Gmail could not be reached. Try again when your connection is available.")
            except Exception:  # noqa: BLE001 - keep provider details out of the interface
                st.error("This email could not be opened for a reply. Select it again to retry.")
    if len(selected) != 1:
        st.caption("Choose one message to reply with the assistant.")
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
            except Exception:  # noqa: BLE001 - UI errors must not reveal private provider details
                st.error("The selected messages could not be opened. Try again.")
    if not api_key or not model:
        st.info("Add your OpenAI API key in Settings to analyze messages.")
    # Reconfirm the scope when upgrading from selected-message-only analysis.
    if (st.session_state.get("batch_consent_scope") != UNIFIED_VERSION
            or st.session_state.get("batch_consent_model") != model):
        st.session_state.batch_consent = False
        st.session_state.batch_consent_scope = UNIFIED_VERSION
        st.session_state.batch_consent_model = model
    suggest_effort = st.checkbox(
        "Suggest study time when it is not stated", value=True, key="batch_suggest_effort",
        on_change=_selection_changed,
        help="Suggest total active work time and explain the assumptions. You can accept, edit or clear each suggestion before saving.",
    )
    consent = st.checkbox(
        "Allow selected messages, readable attachments and related saved task details to be sent to OpenAI to distinguish study tasks from fixed events, check duplicates and suggest study time when enabled. Usage charges may apply.",
        key="batch_consent",
    )
    reanalyze = st.checkbox(
        "Analyze completed messages again", key="batch_reanalyze",
        help="Saved source analysis is reused by default. Select this to analyze the chosen messages and attachments again; usage charges may apply.",
    )
    account = str(st.session_state.get("gmail_profile", {}).get("email_address") or "").strip().casefold()
    signature = hashlib.sha256(json.dumps(
        [UNIFIED_VERSION, PIPELINE_VERSION, RECONCILIATION_VERSION, account, selected, model, suggest_effort],
    ).encode()).hexdigest()
    if st.button("Analyze selected messages", key="batch_extract", type="primary",
                 disabled=not (ready and api_key and model and consent and selected)):
        # A failed new attempt must not leave a previous successful preview saveable.
        for key in ("batch_results", "batch_fixed_result", "batch_analysis_signature", "batch_analysis_run"):
            st.session_state.pop(key, None)
        progress = st.progress(0.0, text="Opening selected messages...")
        try:
            cache = st.session_state.setdefault("batch_cache", {})
            if reanalyze:
                st.session_state.batch_effort_cache = {}
            analysis = run_unified_batch(
                selected, credential_path, token_path, st.session_state.get("gmail_profile"),
                st.session_state.mails, cache, st.session_state.candidates, api_key, model,
                store=store, reanalyze=reanalyze, suggest_effort=suggest_effort,
                progress=lambda value, text: progress.progress(value, text=text),
                effort_cache=st.session_state.setdefault("batch_effort_cache", {}),
                reconciliation_cache=st.session_state.setdefault("batch_reconciliation_cache", {}),
            )
            task_result = analysis.get("task_result") or {}
            results = task_result.get("results", [])
            st.session_state.batch_results = results
            st.session_state.batch_fixed_result = analysis.get("event_result")
            st.session_state.batch_analysis_run = analysis
            st.session_state.batch_analysis_signature = signature
            st.session_state.batch_results_version = PIPELINE_VERSION
            st.session_state.batch_reconciliation_version = RECONCILIATION_VERSION
            effort = task_result.get("effort", {})
            st.session_state.batch_effort_suggestions = effort.get("estimates", {})
            st.session_state.batch_effort_warnings = effort.get("warnings", [])
            try:
                library = AttachmentLibrary(store.path)
                for entry in results:
                    if entry.get("result", {}).get("tasks"):
                        library.save_for_message(st.session_state.mails[entry["mail_id"]])
                for entry in (analysis.get("event_result") or {}).get("results", []):
                    if entry.get("events") and entry.get("mail_id") in st.session_state.mails:
                        library.save_for_message(st.session_state.mails[entry["mail_id"]])
            except Exception:  # noqa: BLE001 - optional attachment retention must not discard suggestions
                st.warning("Suggestions are ready, but some attachments could not be kept. Use Save attachment to keep them.")
            st.session_state.batch_review_version = st.session_state.get("batch_review_version", 0) + 1
            successful = [entry for entry in results if "mail_id" in entry]
            if successful:
                st.session_state.active_mail = successful[0]["mail_id"]
                st.session_state.pop("review_message", None)
        except GmailAuthError:
            _authentication_failed()
        except GmailError:
            st.error("Gmail could not be reached. Try again when your connection is available.")
        except Exception:  # noqa: BLE001 - sanitize unexpected provider or local processing errors
            st.error("The selected messages could not be processed. Try again.")
        finally:
            progress.empty()

    results = st.session_state.get("batch_results", [])
    analysis = st.session_state.get("batch_analysis_run")
    if not results and analysis is None:
        return
    if st.session_state.get("batch_results_version") != PIPELINE_VERSION:
        st.info("Task extraction has been updated. Choose Analyze selected messages to refresh both kinds of suggestions.")
        return
    if st.session_state.get("batch_reconciliation_version") != RECONCILIATION_VERSION:
        st.info("Duplicate checking has been updated. Choose Analyze selected messages to refresh the suggestions; saved source analysis can be reused.")
        return
    if st.session_state.get("batch_analysis_signature") != signature:
        st.info("The selected messages, model or analysis options changed. Analyze selected messages to review the current selection.")
        return
    for warning in (analysis or {}).get("warnings", []):
        st.warning(warning)
    if (analysis or {}).get("status") != "completed":
        st.warning("Some analysis could not finish. Available suggestions are shown below; choose Analyze selected messages to retry.")
    fixed_result = st.session_state.get("batch_fixed_result") or {}
    fixed_events = fixed_result.get("events", [])
    for warning in fixed_result.get("warnings", []):
        st.warning(warning)
    for entry in fixed_result.get("results", []):
        if entry.get("error"):
            mail = st.session_state.mails.get(entry.get("mail_id"), {})
            st.error(f"{mail.get('subject') or 'Message'}: {entry['error']}")
    with st.expander("Analysis details"):
        if (analysis or {}).get("cached"):
            st.caption("Previous complete analysis reused. No new agent analysis was requested.")
        st.caption("Study tasks can be scheduled before their deadlines. Fixed appointments keep their exact times. "
                   "The same message can contain both; missing information remains for review.")
        activity = (analysis or {}).get("events", [])
        if activity:
            st.dataframe(activity, hide_index=True, width="stretch")

    def fixed_review():
        if not fixed_result.get("complete"):
            st.info("Fixed-event analysis is incomplete. Analyze selected messages again to retry.")
        if fixed_events:
            render_fixed_event_review(
                fixed_events, store, account=account,
                key=f"batch_unified_fixed_{signature}_{st.session_state.get('batch_review_version', 0)}",
            )
        elif fixed_result.get("complete"):
            st.caption("No fixed appointments found in these messages.")
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
    st.write(f"**{len(rows)} flexible study task(s) · {len(fixed_events)} fixed appointment(s)**")
    if not rows:
        if ((analysis or {}).get("task_result") is None
                or (analysis or {}).get("task_result", {}).get("status") != "completed"):
            st.info("Study-task analysis is incomplete. Analyze selected messages again to retry.")
        else:
            st.caption("No flexible study tasks found in these messages.")
        fixed_review()
        return
    rows, sources = apply_effort_suggestions(
        rows, sources, st.session_state.get("batch_effort_suggestions", {}),
    )
    for warning in st.session_state.get("batch_effort_warnings", []):
        st.warning(warning)
    st.write("**Review task suggestions**")
    total = sum(len(entry.get("result", {}).get("tasks", [])) for entry in results)
    if total > len(rows):
        st.caption(f"{total - len(rows)} duplicate mention(s) combined. {len(rows)} distinct task suggestion(s) to review.")
    st.caption("Check Include for each task to save. Edit names, deadlines and study time. Deadlines use Singapore time; leave unknown dates blank and unknown study time as 0.")
    st.caption("Confirmed requires a deadline and estimated study time greater than 0. Otherwise the task is saved as Needs details; you can complete or edit it on the Tasks page.")
    st.caption("AI suggestions are estimates of total active work time, not facts from the source. Review the basis, edit Study time (min), or set it to 0 to leave it unknown. Only the value you confirm and save is used for scheduling.")
    version = _review_version(rows)
    with st.form(f"batch_review_form_{version}"):
        edited = st.data_editor(
            rows, hide_index=True, num_rows="fixed", width="stretch",
            column_order=["Include", "Task", "Deadline", "Study time (min)",
                          "AI suggestion (min)", "Estimate basis", "Source-stated time (min)",
                          "Supporting text", "Source message", "Source file", "Review notes"],
            disabled=["Source message", "Source file", "Review notes", "_row_id",
                      "Source-stated time (min)", "AI suggestion (min)", "Estimate basis"],
            column_config={
                "_row_id": None,
                "Include": st.column_config.CheckboxColumn("Include", default=False),
                "Deadline": st.column_config.TextColumn("Deadline", help="Singapore time: YYYY-MM-DD HH:MM"),
                "Study time (min)": st.column_config.NumberColumn(min_value=0, max_value=10000, step=15,
                    help="Total active work minutes to save after review. Includes any work already completed; 0 means unknown."),
                "Source-stated time (min)": st.column_config.NumberColumn(),
                "AI suggestion (min)": st.column_config.NumberColumn(),
                "Estimate basis": st.column_config.TextColumn(width="large"),
                "Supporting text": st.column_config.TextColumn(width="large"),
                "Review notes": st.column_config.TextColumn(width="large"),
            }, key=f"batch_editor_{version}",
        )
        reviewed = st.checkbox(
            "I have reviewed the selected tasks, supporting text and study-time estimates.", key=f"batch_reviewed_{version}",
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
                except Exception:  # noqa: BLE001 - tasks remain saved if attachment storage fails
                    st.warning("Tasks were saved, but some attachments could not be kept. Save again to retry.")
                pending = sum(task["status"] == "pending" for task in store.tasks()
                              if task["id"] in identifiers)
                kept = len(identifiers & previous_ids)
                st.success(f"Saved {len(identifiers)} task(s)."
                           + (f" {pending} marked as Needs details." if pending else "")
                           + (f" {kept} already saved; existing details and progress kept. Edit these on the Tasks page." if kept else ""))
            except ValueError as exc:
                st.error(str(exc))
            except Exception:  # noqa: BLE001 - keep unexpected storage errors out of the product UI
                st.error("The selected tasks could not be saved. Try again.")
    fixed_review()
