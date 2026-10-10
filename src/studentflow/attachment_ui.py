"""Attachment browsing works from saved records even after Gmail is disconnected."""

from __future__ import annotations

import sqlite3

import streamlit as st

from studentflow.attachment_library import AttachmentLibrary, available_documents, library_id
from studentflow.retrieval import build_chunks, search_chunks

STATUS_LABELS = {
    "ok": "Ready", "partial": "Partially readable", "needs_ocr": "Text recognition needed",
    "error": "Could not read", "blocked": "Unavailable", "encrypted": "Password protected",
    "unsupported": "Unsupported file type", "empty": "No text found",
}


def render_attachments(store, mails: dict) -> None:
    st.subheader("Attachments")
    st.caption("Attachments from messages with extracted or saved tasks are kept on this device until you delete them.")
    st.caption("Readable PDF and Word text is included in task extraction. Images and scanned pages need text recognition.")
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
    if not documents:
        st.info("No attachments available. Open a message with a PDF or Word attachment in Inbox (Gmail).")
        return
    st.caption(f"{sum(doc['saved'] for doc in documents)} saved attachment(s) · {len(documents)} available")
    opened_content = {
        library_id(mail, attachment): attachment.get("content", b"")
        for mail in mails.values() for attachment in mail.get("attachments", [])
    }
    for document in documents:
        identifier = document["library_id"]
        status = STATUS_LABELS.get(document["status"], "Not available")
        with st.expander(f"{document['filename']} · {status}"):
            st.caption("Source message: " + document["message_subject"])
            st.caption("Saved on this device" if document["saved"] else "Opened in this session; saved automatically when tasks are extracted or saved.")
            if not document["saved"] and st.button("Save attachment", key=f"attachment_save_{identifier}"):
                try:
                    library.save_for_message(mails[document["mail_id"]], only_library_id=identifier)
                except (OSError, sqlite3.Error):
                    st.error("This attachment could not be saved. Check that the app's data folder is available and try again.")
                else:
                    st.session_state.attachment_notice = "Attachment saved on this device."
                    st.rerun()
            for warning in document.get("warnings", []):
                st.warning(warning)
            content = library.content(identifier) if document["saved"] else opened_content.get(identifier)
            if content:
                st.download_button("Download original", content, document["filename"],
                                   document.get("content_type", "application/octet-stream"), key=f"attachment_download_{identifier}")
            else:
                st.caption("The original file was unavailable or exceeded the supported limits.")
            if document.get("text"):
                st.text(document["text"])
            confirmed = st.checkbox("Delete this attachment from the library. Saved tasks will remain.",
                                    key=f"attachment_delete_confirm_{identifier}")
            if st.button("Delete attachment", key=f"attachment_delete_{identifier}", disabled=not confirmed):
                try:
                    library.delete(identifier)
                except (OSError, sqlite3.Error):
                    st.error("This attachment could not be deleted. Try again.")
                else:
                    st.session_state.attachment_notice = "Attachment deleted. Saved tasks were kept."
                    st.rerun()
    with st.form("attachment_search"):
        question = st.text_input("Search attachments", placeholder="Enter keywords from the document")
        search = st.form_submit_button("Search")
    if search:
        hits = search_chunks(question, build_chunks(documents))
        if not hits:
            st.info("No matching text found. Try different keywords.")
        subjects = {document["mail_id"]: document["message_subject"] for document in documents}
        for hit in hits:
            with st.container(border=True):
                st.write(hit["filename"])
                page = f"Page {hit['page']}" if hit["page"] else "Document text"
                st.caption(f"{subjects.get(hit['mail_id'], 'Saved message')} · {page}")
                st.text(hit["text"])
