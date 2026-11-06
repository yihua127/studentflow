"""Attachment browsing works from saved records even after Gmail is disconnected."""

from __future__ import annotations

from html import escape
import sqlite3

import streamlit as st

from studentflow.page_heading import render_page_heading

from studentflow.attachment_library import AttachmentLibrary, available_documents, library_id
from studentflow.retrieval import build_chunks, search_chunks

STATUS_LABELS = {
    "ok": "Ready", "partial": "Partially readable", "needs_ocr": "Text recognition needed",
    "error": "Could not read", "blocked": "Unavailable", "encrypted": "Password protected",
    "unsupported": "Unsupported file type", "empty": "No text found",
}


def _document_labels(documents: list[dict]) -> dict[str, str]:
    """Keep native selection labels distinct, including same-named message sources."""
    labels = {
        document["library_id"]: document["filename"] + " · " + document["message_subject"]
        for document in documents
    }
    if len(set(labels.values())) != len(labels):
        # Prefix every choice when ambiguity exists, so a source title that
        # already ends in a distinguishing suffix cannot collide with it.
        labels = {identifier: f"{index}. {label}"
                  for index, (identifier, label) in enumerate(labels.items(), 1)}
    return labels


def render_attachments(store, mails: dict) -> None:
    render_page_heading(
        "YOUR REFERENCE LIBRARY",
        "Attachments",
        "Keep course documents close, find the details you need and revisit the original source.",
    )
    try:
        library = AttachmentLibrary(store.path)
        retained = {doc["library_id"] for doc in library.documents()} | library.deleted_ids()
        task_messages = {task.get("mail_id") for task in store.tasks()}
        for mail in mails.values():
            if mail["id"] not in task_messages:
                continue
            expected = {library_id(mail, attachment) for attachment in mail.get("attachments", [])}
            if expected <= retained:
                continue
            try:
                retained.update(library.save_for_message(mail))
            except (OSError, sqlite3.Error):
                st.warning("Some attachments for saved tasks could not be kept. Use Save attachment below to retry.")
        documents = available_documents(library, mails)
    except (OSError, sqlite3.Error):
        st.error("The attachment library could not be opened. Check that the app's data folder is available and try again.")
        return
    notice = st.session_state.pop("attachment_notice", None)
    if notice:
        st.success(notice)
    with st.container(key="sf_attachment_metrics"):
        columns = st.columns(3)
        for column, label, value in zip(columns, (
            "Available documents", "Saved on this device", "With readable text",
        ), (
            len(documents), sum(doc["saved"] for doc in documents),
            sum(bool(doc.get("text")) for doc in documents),
        ), strict=True):
            column.metric(label, value)
    if not documents:
        with st.container(border=True, key="sf_panel_attachments_empty"):
            st.html('<div class="sf-empty-state"><strong>Your documents, in one place</strong>'
                    '<p>Open a message with a PDF or Word attachment in Inbox to start your library.</p></div>')
            st.caption("Attachments from messages with extracted or saved tasks are kept on this device until you delete them.")
        return
    opened_content = {
        library_id(mail, attachment): attachment.get("content", b"")
        for mail in mails.values() for attachment in mail.get("attachments", [])
    }
    document_labels = _document_labels(documents)
    browser, preview = st.columns([1, 1.7], gap="medium")
    with browser, st.container(border=True, key="sf_panel_attachments_browser"):
        st.html('<div class="sf-attachment-panel-heading"><h3>Your documents</h3></div>')
        query = st.text_input(
            "Filter documents", placeholder="Find a filename or source message", key="sf_attachment_filter",
        ).strip().casefold()
        matches = [document for document in documents if not query or query in
                   (document["filename"] + " " + document["message_subject"]).casefold()]
        choices = {document["library_id"]: document for document in matches}
        selection_key = "sf_attachment_browser"
        if st.session_state.get(selection_key) not in choices:
            st.session_state.pop(selection_key, None)
        selected = None
        if choices:
            with st.container(
                height=300 if len(choices) > 4 else "content", border=False,
                key="sf_attachment_choices",
            ):
                selected = st.radio(
                    "Document to preview", list(choices), key=selection_key,
                    label_visibility="collapsed",
                    format_func=document_labels.__getitem__,
                )
            st.caption(f"{len(matches)} of {len(documents)} document(s)")
        else:
            st.info("No documents match this filter. Try a filename or source message.")
        with st.expander("About saved documents"):
            st.caption("Attachments from messages with extracted or saved tasks are kept on this device until you delete them.")
            st.caption("Readable PDF and Word text is included in task extraction. Images and scanned pages need text recognition.")
    with browser, st.container(border=True, key="sf_panel_attachments_search"):
        st.subheader("Search document text")
        st.caption("Search readable text across all available documents, with the source message and page for each match.")
        with st.form("attachment_search", border=False):
            question = st.text_input("Search attachments", placeholder="Enter keywords from the document", key="sf_attachment_search")
            search = st.form_submit_button("Search", width="stretch")
        hits = search_chunks(question, build_chunks(documents)) if search else None
        if hits is not None and not hits:
            st.info("No matching text found. Try different keywords.")
    with preview, st.container(border=True, key="sf_panel_attachments_preview"):
        document = choices.get(selected)
        filename = f'<span class="sf-attachment-title">{escape(document["filename"])}</span>' if document else ""
        st.html(
            '<div class="sf-attachment-panel-heading">'
            f'<h3>Document preview</h3>{filename}'
            '</div>'
        )
        if document is None:
            st.html('<div class="sf-empty-state"><strong>Find a document to preview</strong>'
                    '<p>Change the filter to browse your available documents.</p></div>')
        else:
            with st.container(key="sf_attachment_metadata"):
                identifier = document["library_id"]
                status = STATUS_LABELS.get(document["status"], "Not available")
                st.caption(status + " · " + ("Saved on this device" if document["saved"] else "Opened in this session"))
                st.caption("Source message: " + document["message_subject"])
                if not document["saved"]:
                    st.caption("Saved automatically when tasks are extracted or saved. You can also keep this document now.")
                if not document["saved"]:
                    if st.button("Save attachment", key=f"attachment_save_{identifier}"):
                        try:
                            library.save_for_message(mails[document["mail_id"]], only_library_id=identifier)
                        except (OSError, sqlite3.Error):
                            st.error("This attachment could not be saved. Check that the app's data folder is available and try again.")
                        else:
                            st.session_state.attachment_notice = "Attachment saved on this device."
                            st.rerun()
                content = library.content(identifier) if document["saved"] else opened_content.get(identifier)
                if content:
                    with st.container(horizontal=True, horizontal_alignment="center"):
                        st.download_button(
                            "Download original", content, document["filename"],
                            document.get("content_type", "application/octet-stream"),
                            key=f"attachment_download_{identifier}", icon=":material/download:", width="content",
                        )
                else:
                    st.caption("The original file was unavailable or exceeded the supported limits.")
                for warning in document.get("warnings", []):
                    st.warning(warning)
            with st.container(gap="small", key="sf_attachment_reading"):
                st.html('<hr class="sf-attachment-divider">')
                st.write("**Document text**")
                if document.get("text"):
                    with st.container(border=False, key="sf_attachment_text"):
                        st.text(document["text"])
                else:
                    st.caption("No readable text is available. Check the document status and any reading notes above.")
            with st.container(key="sf_attachment_manage"), st.expander("Manage attachment"):
                confirmed = st.checkbox("Delete this attachment from the library. Saved tasks will remain.",
                                        key=f"attachment_delete_confirm_{identifier}")
                with st.container(horizontal=True, horizontal_alignment="center"):
                    delete_requested = st.button(
                        "Delete attachment", key=f"attachment_delete_{identifier}", disabled=not confirmed,
                    )
                if delete_requested:
                    try:
                        library.delete(identifier)
                    except (OSError, sqlite3.Error):
                        st.error("This attachment could not be deleted. Try again.")
                    else:
                        st.session_state.attachment_notice = "Attachment deleted. Saved tasks were kept."
                        st.rerun()
    if hits:
        with st.container(border=True, key="sf_panel_attachments_matches"):
            st.html('<div class="sf-eyebrow">MATCHING PASSAGES</div>')
            st.subheader("Search results")
            subjects = {document["mail_id"]: document["message_subject"] for document in documents}
            for index, hit in enumerate(hits):
                with st.container(border=True, key=f"sf_attachment_search_hit_{index}"):
                    st.text(hit["filename"])
                    page = f"Page {hit['page']}" if hit["page"] else "Document text"
                    st.caption(f"{subjects.get(hit['mail_id'], 'Saved message')} · {page}")
                    st.text(hit["text"])
