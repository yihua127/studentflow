"""Inbox classification, local review and explicitly confirmed Gmail labels."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import streamlit as st

from studentflow.email_label_catalog import LabelCatalog
from studentflow.email_label_manager_ui import render_label_manager
from studentflow.gmail import GmailAuthError, GmailError
from studentflow.gmail_labels import TOKEN_FILENAME, connect_labels
from studentflow.inbox_triage import (
    CATEGORY_LABELS,
    CHUNK_SIZE,
    InboxTriageStore,
    classify_messages,
)
from studentflow.inbox_triage import fetch_classification_messages as fetch_selected_messages
from studentflow.reply_handoff import queue_reply_handoff
from studentflow.triage_sync import apply_label_sync, prepare_label_sync

PREFIX = "sf_triage_"


def _signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def clear_triage_state():
    for key in list(st.session_state):
        if str(key).startswith(PREFIX):
            st.session_state.pop(key, None)


def _select(identifiers):
    st.session_state.batch_selected = list(identifiers)


def _analyze_tasks(identifiers):
    _select(identifiers)
    st.session_state["sf_inbox_workflow"] = "Tasks & events"
    st.session_state["batch_consent"] = False


def _storage_error():
    st.error("Categories could not be saved on this device. Check local storage and try again.")


def _items_for_sync(rows, records, names=None):
    names = names or CATEGORY_LABELS
    reverse = {names[key]: key for key in CATEGORY_LABELS}
    items = []
    for row, record in zip(rows, records, strict=True):
        if not row.get("Use"):
            continue
        category = reverse.get(row.get("Category"))
        if category is None:
            raise ValueError("Choose a category for every selected row.")
        items.append({
            "gmail_id": record["gmail_id"], "category": category,
            "subject": record["subject"],
            "tag_ids": list(record.get("tag_ids", [])),
            "source_fingerprint": _signature([record["fingerprint"], record["updated_at"]]),
        })
    return items


def _verify_current(triage_store, account, items, records):
    by_id = {record["gmail_id"]: record for record in records}
    for item in items:
        previous = by_id[item["gmail_id"]]
        current = triage_store.get(account, item["gmail_id"])
        if (current is None or current["fingerprint"] != previous["fingerprint"]
                or current["updated_at"] != previous["updated_at"]):
            raise ValueError("A saved category changed in another window. Reload and review the latest categories.")


def _save_categories(triage_store, account, items, records):
    _verify_current(triage_store, account, items, records)
    by_id = {record["gmail_id"]: record for record in records}
    for item in items:
        record = by_id[item["gmail_id"]]
        triage_store.review(
            account, item["gmail_id"], item["category"],
            expected_fingerprint=record["fingerprint"], expected_updated_at=record["updated_at"],
            tag_ids=item.get("tag_ids"),
        )


def _verify_catalog(catalog_store, account, catalog):
    if catalog_store.get(account)["version"] != catalog["version"]:
        raise ValueError("Your label names changed in another window. Reload and preview the latest labels.")


def _render_label_sync(account, items, records, triage_store, credential_path, data_dir, ready,
                       catalog_store, catalog):
    st.write("**Sync labels to Gmail**")
    st.caption(
        "Preview changes, then confirm to apply StudentFlow labels to the checked messages. "
        "Other labels, inbox placement and read status stay unchanged."
    )
    token_path = Path(data_dir) / "auth" / TOKEN_FILENAME
    if not token_path.is_file():
        st.caption("Connect once to allow Gmail label updates for this account: " + account)
    left, right = st.columns(2)
    if left.button(
        "Connect Gmail labels" if not token_path.is_file() else "Change label connection",
        key=PREFIX + "connect", disabled=not ready or not Path(credential_path).is_file(),
    ):
        st.session_state.pop(PREFIX + "preview", None)
        try:
            with st.spinner("Connecting Gmail labels..."), closing(connect_labels(
                credential_path, token_path, interactive=True,
            )) as client:
                if client.account().strip().casefold() != account:
                    raise GmailError("Choose the same Google account as the connected inbox.")
            st.session_state[PREFIX + "notice"] = "Gmail labels connected. Preview the changes below."
            st.rerun()
        except (GmailError, OSError) as error:
            st.error(str(error) if isinstance(error, GmailError) else "The label connection could not be saved.")
    if right.button("Disconnect Gmail labels", key=PREFIX + "disconnect", disabled=not token_path.is_file()):
        try:
            token_path.unlink(missing_ok=True)
            st.session_state.pop(PREFIX + "preview", None)
            st.session_state[PREFIX + "notice"] = "Gmail label connection removed from this device."
            st.rerun()
        except OSError:
            st.error("The label connection could not be removed from this device.")

    signature = _signature([account, items, catalog])
    saved = st.session_state.get(PREFIX + "preview")
    if saved and saved["signature"] != signature:
        st.session_state.pop(PREFIX + "preview", None)
        saved = None
    if st.button("Preview Gmail label changes", key=PREFIX + "preview_button",
                 disabled=not (ready and items and token_path.is_file())):
        st.session_state.pop(PREFIX + "preview", None)
        saved = None
        try:
            _verify_current(triage_store, account, items, records)
            _verify_catalog(catalog_store, account, catalog)
            with st.spinner("Checking Gmail labels..."), closing(connect_labels(credential_path, token_path)) as client:
                preview = prepare_label_sync(client, account=account, items=items, catalog=catalog)
            saved = {"signature": signature, "preview": preview}
            st.session_state[PREFIX + "preview"] = saved
        except (GmailError, ValueError, OSError, sqlite3.Error) as error:
            st.error(str(error) if isinstance(error, (GmailError, ValueError)) else "The preview could not be prepared. Try again.")
    if saved is None:
        return
    preview = saved["preview"]
    st.caption("Gmail account: " + account)
    st.dataframe([
        {"Message": item.subject, "Add labels": ", ".join(item.add_labels) or "Already applied",
         "Remove label": ", ".join(item.remove_labels) or "None"}
        for item in preview.items
    ], hide_index=True, width="stretch")
    if preview.missing_labels:
        st.caption("Labels to create: " + ", ".join(preview.missing_labels))
    approved = st.checkbox(
        "I have reviewed these label changes for the selected messages.",
        key=PREFIX + "approved_" + _signature([signature, preview.fingerprint]),
    )
    if st.button("Apply Gmail labels", type="primary", key=PREFIX + "apply",
                 disabled=not (approved and ready and token_path.is_file())):
        if not approved or saved["signature"] != _signature([account, items, catalog]):
            return
        try:
            _verify_current(triage_store, account, items, records)
            _verify_catalog(catalog_store, account, catalog)
            with st.spinner("Applying your reviewed Gmail labels..."), closing(connect_labels(credential_path, token_path)) as client:
                outcome = apply_label_sync(
                    client, preview, account=account, items=items, data_dir=data_dir, confirmed=True,
                    catalog=catalog,
                )
        except (GmailError, ValueError, OSError, sqlite3.Error) as error:
            st.session_state.pop(PREFIX + "preview", None)
            st.error(str(error) if isinstance(error, (GmailError, ValueError)) else "Label updates could not finish. Preview again to check the latest Gmail state.")
            return
        st.session_state.pop(PREFIX + "preview", None)
        completed = outcome.get("completed_ids", [])
        try:
            _save_categories(triage_store, account, [item for item in items if item["gmail_id"] in completed], records)
        except (ValueError, OSError, sqlite3.Error):
            st.session_state[PREFIX + "sync_warning"] = "Gmail updates completed for some messages, but their local categories could not be saved. Reload and review them."
        st.session_state[PREFIX + "sync_result"] = outcome
        st.rerun()


def render_inbox_triage(
    headers, credential_path, token_path, store, data_dir, api_key, model, ready,
    *, selected_ids: list[str] | None = None,
):
    """Review categories for the built-in picker or a caller's selected Gmail IDs.

    With an external selection, the category filter only narrows review results;
    classification still uses every selected message. The caller owns selection
    changes, including the pending selection queued after a manual label save.
    """
    account = str(st.session_state.get("gmail_profile", {}).get("email_address") or "").strip().casefold()
    if st.session_state.get(PREFIX + "account") != account:
        clear_triage_state()
        st.session_state[PREFIX + "account"] = account
    st.caption(
        "Needs reply: a response is requested. Needs action: work or attendance is required. "
        "No action needed: informational mail. When both apply, Needs reply takes priority; "
        "the explanation keeps the other action."
    )
    st.caption("Classification uses message text, not attachments. Unreadable or incomplete results stay unclassified.")
    if not headers or not account:
        st.info("Refresh the inbox to choose messages.")
        return
    try:
        triage_store = InboxTriageStore(store.path)
        catalog_store = LabelCatalog(store.path)
        by_id = {header["id"]: header for header in headers}
        saved = triage_store.list(account, list(by_id))
    except (OSError, sqlite3.Error, ValueError):
        st.error("Saved categories could not be loaded. Check local storage and try again.")
        return
    try:
        catalog = render_label_manager(catalog_store, account)
    except (ValueError, OSError, sqlite3.Error):
        st.error("Saved label names could not be loaded. Check local storage and try again.")
        return
    names = catalog["names"]
    notice = st.session_state.pop(PREFIX + "notice", None)
    if notice:
        st.success(notice)
    sync_result = st.session_state.get(PREFIX + "sync_result")
    if sync_result:
        if sync_result.get("status") == "synced":
            st.success(f"Last sync completed for {len(sync_result.get('completed_ids', []))} message(s). Preview again after changing categories.")
        else:
            st.warning("Some Gmail labels could not be updated. Preview again to retry the remaining changes.")
        for error in sync_result.get("errors", []):
            st.warning(error)
    if warning := st.session_state.pop(PREFIX + "sync_warning", None):
        st.warning(warning)
    filters = {"All messages": None, "Unclassified": "unclassified",
               **{key: key for key in CATEGORY_LABELS},
               **{key: key for key in catalog["custom_ids"]}}
    if st.session_state.get(PREFIX + "filter", "All messages") not in filters:
        st.session_state[PREFIX + "filter"] = "All messages"
    if st.session_state.pop(PREFIX + "show_results", False):
        st.session_state[PREFIX + "filter"] = "All messages"
    category_filter = st.selectbox(
        "Show emails" if selected_ids is None else "Show category results", list(filters), key=PREFIX + "filter",
        format_func=lambda key: "Label: " + names[key] if key in catalog["custom_ids"] else names.get(key, key),
    )
    wanted = filters[category_filter]
    selected = None if selected_ids is None else list(dict.fromkeys(
        identifier for identifier in selected_ids if identifier in by_id
    ))
    review_ids = list(by_id) if selected is None else selected
    visible_ids = [identifier for identifier in review_ids if wanted is None or (
        (wanted in saved.get(identifier, {}).get("tag_ids", []) if wanted in catalog["custom_ids"]
         else saved.get(identifier, {}).get("category") == wanted)
        if wanted != "unclassified" else saved.get(identifier, {}).get("category") not in CATEGORY_LABELS
    )]
    if selected is None:
        pending_selection = st.session_state.pop(PREFIX + "pending_selection", None)
        if pending_selection is not None:
            st.session_state.batch_selected = pending_selection
        # Hidden messages are removed from selection before any action can use it.
        st.session_state.batch_selected = [sid for sid in st.session_state.get("batch_selected", []) if sid in visible_ids]
        selected = st.multiselect(
            "Messages to analyze", visible_ids, key="batch_selected",
            format_func=lambda sid: f"{by_id[sid].get('subject') or 'Untitled message'} — {by_id[sid].get('sender', '')}",
        )
        left, right = st.columns(2)
        left.button(
            "Select all shown",
            key=PREFIX + "all", on_click=_select, args=(visible_ids,),
        )
        right.button("Clear selection", key=PREFIX + "clear", on_click=_select, args=([],), disabled=not selected)
    elif st.session_state.get(PREFIX + "external_selection") != selected:
        st.session_state.pop(PREFIX + "preview", None)
        st.session_state[PREFIX + "external_selection"] = selected
    st.caption(f"{len(selected)} message(s) selected. Messages are classified {CHUNK_SIZE} at a "
               "time, so larger selections take longer.")
    scope = _signature([account, selected, model])
    consent = st.checkbox(
        "Send the selected email text to OpenAI for classification. API usage charges apply.",
        key=PREFIX + "consent_" + scope,
    )
    if not api_key:
        st.info("Add your OpenAI API key in Settings to classify messages.")
    reanalyze = st.checkbox("Classify again", key=PREFIX + "reanalyze", help="Reuse saved results by default. Your reviewed categories are kept.")
    if st.button("Classify selected messages", key=PREFIX + "run", type="primary",
                 disabled=not (ready and api_key and model and consent and selected)):
        st.session_state.pop(PREFIX + "preview", None)
        st.session_state.pop(PREFIX + "sync_result", None)
        progress = st.progress(0.0, text="Opening selected emails...")
        try:
            opened = fetch_selected_messages(
                selected, credential_path, token_path, st.session_state.get("gmail_profile"),
                st.session_state.mails,
            )
            failures = [entry for entry in opened if "error" in entry]
            mails = [st.session_state.mails[entry["mail_id"]] for entry in opened if "mail_id" in entry]
            result = classify_messages(
                mails, account, api_key, model, store=triage_store, reanalyze=reanalyze,
                progress=lambda value, text: progress.progress(value, text=text),
            ) if mails else {"warnings": [], "status": "failed"}
            st.session_state[PREFIX + "warnings"] = [entry["error"] for entry in failures] + result.get("warnings", [])
            st.session_state[PREFIX + "unsaved"] = [
                entry for entry in result.get("results", []) if entry.get("persistence_failed")
            ]
            st.session_state[PREFIX + "show_results"] = True
            if result.get("status") != "completed":
                st.session_state[PREFIX + "warnings"].append("Some messages remain unclassified. Review the available results and retry those messages.")
            st.rerun()
        except GmailAuthError:
            st.error("The Gmail account or authorization changed. Reconnect Gmail before classifying.")
        except (GmailError, OSError, sqlite3.Error, ValueError):
            st.error("The selected emails could not be classified. Check the connection and try again.")
        finally:
            progress.empty()
    for warning in st.session_state.get(PREFIX + "warnings", []):
        st.warning(warning)
    unsaved = st.session_state.get(PREFIX + "unsaved", [])
    if selected_ids is not None:
        unsaved = [record for record in unsaved if record.get("gmail_id") in selected]
    if unsaved:
        st.warning("These suggestions are available for review but were not saved. Classify again after fixing local storage to save or sync them.")
        st.dataframe([
            {"Message": record["subject"], "Category": names[record["category"]],
             "Reason": record["reason"]} for record in unsaved
        ], hide_index=True)
        with st.expander("Supporting text for unsaved suggestions"):
            for record in unsaved:
                st.text(record["subject"])
                for citation in record["citations"]:
                    st.text(citation["quote"])

    records = [saved[sid] for sid in visible_ids if saved.get(sid, {}).get("category") in CATEGORY_LABELS]
    if not records:
        st.info("No classified messages in this view yet.")
        return
    st.write("**Review email categories**")
    st.caption("Click a Category cell to change it, or use Edit labels below for a selected email. Tick Use for the messages to save or sync.")
    identity = _signature([[record["gmail_id"], record["updated_at"]] for record in records] + [selected, catalog["version"]])
    rows = st.data_editor([
        {"Use": record["gmail_id"] in selected, "Message": record["subject"],
         "Category": names[record["category"]], "Custom labels": ", ".join(
             names[key] for key in record.get("tag_ids", []) if key in names), "Reason": record["reason"],
         "Reviewed": bool(record.get("reviewed"))}
        for record in records
    ], key=PREFIX + "editor_" + identity, hide_index=True, width="stretch",
        disabled=["Message", "Custom labels", "Reason", "Reviewed"], column_config={
            "Category": st.column_config.SelectboxColumn("Category", options=[names[key] for key in CATEGORY_LABELS], required=True),
            "Use": st.column_config.CheckboxColumn("Use", default=False),
        })
    try:
        items = _items_for_sync(rows, records, names)
    except ValueError as error:
        st.warning(str(error))
        items = []
    if st.button("Save categories on this device", key=PREFIX + "save", disabled=not items):
        try:
            _save_categories(triage_store, account, items, records)
            st.session_state.pop(PREFIX + "preview", None)
            st.session_state[PREFIX + "notice"] = f"{len(items)} reviewed category/categories saved."
            st.rerun()
        except ValueError as error:
            st.error(str(error))
        except (OSError, sqlite3.Error):
            _storage_error()
    identifiers = [record["gmail_id"] for record in records]
    record_map = {record["gmail_id"]: record for record in records}
    pending_detail = st.session_state.pop(PREFIX + "pending_detail", None)
    if pending_detail in record_map:
        st.session_state[PREFIX + "detail"] = pending_detail
    detail = st.selectbox("Message details", identifiers, format_func=lambda sid: record_map[sid]["subject"], key=PREFIX + "detail")
    record = record_map[detail]
    pending_edits = False
    with st.container(border=True):
        st.write("**Edit labels**")
        st.text(record["subject"])
        edit_key = PREFIX + "manual_" + _signature([account, detail, record["updated_at"], catalog["version"]])
        category = st.selectbox(
            "Primary category", list(CATEGORY_LABELS),
            index=list(CATEGORY_LABELS).index(record["category"]),
            format_func=names.get, key=edit_key + "_category",
        )
        tags = st.multiselect(
            "Custom labels", catalog["custom_ids"],
            default=[tag for tag in record.get("tag_ids", []) if tag in catalog["custom_ids"]],
            format_func=names.get, key=edit_key + "_tags",
            help="Add or remove any custom labels for this email. Create labels under Manage labels above.",
        )
        pending_edits = category != record["category"] or sorted(tags) != sorted(record.get("tag_ids", []))
        if pending_edits:
            st.session_state.pop(PREFIX + "preview", None)
        if not catalog["custom_ids"]:
            st.caption("Use Manage labels above to create labels such as Coursework or Urgent.")
        st.caption("Save applies to this email on this device. Preview and confirm below to update Gmail.")
        if st.button("Save label changes", type="primary", key=PREFIX + "manual_save"):
            try:
                _verify_catalog(catalog_store, account, catalog)
                triage_store.review(
                    account, detail, category, expected_fingerprint=record["fingerprint"],
                    expected_updated_at=record["updated_at"], tag_ids=tags,
                )
                st.session_state.pop(PREFIX + "preview", None)
                st.session_state.pop(PREFIX + "sync_result", None)
                st.session_state[PREFIX + "notice"] = "Label changes saved. This email is selected for Gmail preview below."
                st.session_state[PREFIX + "pending_selection"] = [detail]
                st.session_state[PREFIX + "pending_detail"] = detail
                st.session_state[PREFIX + "show_results"] = True
                st.rerun()
            except ValueError as error:
                st.error(str(error))
            except (OSError, sqlite3.Error):
                _storage_error()
    with st.expander("Classification explanation and supporting text"):
        st.text("Suggested category: " + names[record["suggested_category"]])
        st.text(record["reason"])
        if record.get("reviewed") and record["category"] != record["suggested_category"]:
            st.caption("The saved category was changed during your review. The explanation above belongs to the AI suggestion.")
        for citation in record.get("citations", []):
            st.text(citation["quote"])
    left, right = st.columns(2)
    if left.button("Reply with assistant", key=PREFIX + "reply", disabled=not ready):
        try:
            opened = fetch_selected_messages([detail], credential_path, token_path,
                                             st.session_state.get("gmail_profile"), st.session_state.mails)
            if len(opened) != 1 or "mail_id" not in opened[0]:
                raise ValueError("This email could not be opened. Try again.")
            queue_reply_handoff(st.session_state, account, opened[0]["mail_id"], st.session_state.mails)
            st.rerun()
        except (GmailError, ValueError):
            st.error("This email could not be opened for a reply. Reconnect Gmail and try again.")
    right.button("Find tasks and events", key=PREFIX + "tasks", on_click=_analyze_tasks, args=([detail],), disabled=not ready)
    st.divider()
    if pending_edits:
        st.info("Save label changes above before previewing Gmail updates.")
    _render_label_sync(account, items, records, triage_store, credential_path, data_dir, ready and not pending_edits,
                       catalog_store, catalog)
