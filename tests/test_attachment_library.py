"""Local attachment lifecycle, source isolation, and UI persistence."""

import hashlib
import io

from docx import Document
from streamlit.testing.v1 import AppTest

from studentflow.attachment_library import AttachmentLibrary, available_documents, library_id
from studentflow.retrieval import build_chunks, search_chunks
from studentflow.storage import Store


def sample_mail(filename="Assignment.docx", account="student@example.test"):
    document = Document()
    document.add_paragraph("Submit the research report by Friday at 17:00. Study for 60 minutes.")
    buffer = io.BytesIO()
    document.save(buffer)
    content = buffer.getvalue()
    return {"id": "message-1", "subject": "Course assignment", "body": "See attached.",
            "gmail_account": account, "attachments": [{"id": hashlib.sha256(content).hexdigest(),
             "filename": filename, "content": content, "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}]}


def test_original_and_searchable_text_survive_fresh_library_and_no_open_messages(tmp_path):
    mail = sample_mail()
    path = tmp_path / "records.sqlite3"
    identifiers = AttachmentLibrary(path).save_for_message(mail)
    fresh = AttachmentLibrary(path)
    documents = available_documents(fresh, {})
    assert len(documents) == 1
    assert fresh.content(identifiers[0]) == mail["attachments"][0]["content"]
    assert documents[0]["message_subject"] == "Course assignment"
    assert search_chunks("research report", build_chunks(documents))
    assert len(fresh.save_for_message(mail)) == 1
    assert len(fresh.documents()) == 1


def test_delete_removes_original_and_text_without_removing_task_or_resurrecting(tmp_path):
    mail = sample_mail()
    store = Store(tmp_path / "records.sqlite3")
    store.save_task({"mail_id": mail["id"], "title": "Research report", "status": "pending"})
    library = AttachmentLibrary(store.path)
    identifier = library.save_for_message(mail)[0]
    library.delete(identifier)
    fresh = AttachmentLibrary(store.path)
    assert fresh.documents() == []
    assert fresh.content(identifier) is None
    assert fresh.save_for_message(mail) == []
    assert available_documents(fresh, {mail["id"]: mail}) == []
    assert len(store.tasks()) == 1


def test_deletion_is_scoped_to_message_and_account(tmp_path):
    mail = sample_mail()
    library = AttachmentLibrary(tmp_path / "records.sqlite3")
    library.delete(library_id(mail, mail["attachments"][0]))
    another_message = {**mail, "id": "message-2"}
    another_account = {**mail, "gmail_account": "other@example.test"}
    assert library.save_for_message(another_message)
    assert library.save_for_message(another_account)
    assert len(library.documents()) == 2


def test_path_like_filename_is_metadata_only_and_cleaned_for_download(tmp_path):
    library = AttachmentLibrary(tmp_path / "records.sqlite3")
    mail = sample_mail("../../outside.docx")
    library.save_for_message(mail)
    assert library.documents()[0]["filename"] == "outside.docx"
    assert not (tmp_path.parent / "outside.docx").exists()
    assert list(tmp_path.iterdir()) == [tmp_path / "records.sqlite3"]


def test_opened_and_saved_merge_without_duplicate_and_no_automatic_save(tmp_path):
    mail = sample_mail()
    library = AttachmentLibrary(tmp_path / "records.sqlite3")
    assert len(available_documents(library, {mail["id"]: mail})) == 1
    assert library.documents() == []
    library.save_for_message(mail)
    assert len(available_documents(library, {mail["id"]: mail})) == 1


APP = '''
from pathlib import Path
import os
import streamlit as st
from studentflow.attachment_ui import render_attachments
from studentflow.storage import Store
render_attachments(Store(Path(os.environ['ATTACHMENTS_TEST_DB'])), {})
'''


def test_saved_attachment_ui_previews_searches_downloads_and_deletes_offline(tmp_path, monkeypatch):
    path = tmp_path / "records.sqlite3"
    monkeypatch.setenv("ATTACHMENTS_TEST_DB", str(path))
    library = AttachmentLibrary(path)
    identifier = library.save_for_message(sample_mail())[0]
    app = AppTest.from_string(APP).run(timeout=30)
    assert not app.exception
    assert any("Assignment.docx" in option for option in app.radio(key="sf_attachment_browser").options)
    assert any("research report" in text.value for text in app.text)
    assert len(app.get("download_button")) == 1
    assert app.button(key=f"attachment_delete_{identifier}").disabled
    app.text_input(key="sf_attachment_search").set_value("research report")
    next(button for button in app.button if button.label == "Search").click()
    app.run(timeout=30)
    assert not app.exception
    assert sum("research report" in text.value for text in app.text) == 2
    app.checkbox(key=f"attachment_delete_confirm_{identifier}").check()
    app.run(timeout=30)
    app.button(key=f"attachment_delete_{identifier}").click()
    app.run(timeout=30)
    assert not app.exception
    assert library.documents() == []
    assert not app.expander
    assert any("Attachment deleted" in success.value for success in app.success)


def test_open_attachment_can_be_saved_individually_without_a_task(tmp_path, monkeypatch):
    path = tmp_path / "records.sqlite3"
    monkeypatch.setenv("ATTACHMENTS_TEST_DB", str(path))
    mail = sample_mail()
    mail["attachments"].append({**mail["attachments"][0], "filename": "second.docx"})
    identifier = library_id(mail, mail["attachments"][1])
    app = AppTest.from_string(APP.replace(")), {})", ")), st.session_state.get('mails', {}))"))
    app.session_state["mails"] = {mail["id"]: mail}
    app.run(timeout=30)
    assert not app.exception
    assert len(app.radio(key="sf_attachment_browser").options) == 2
    app.radio(key="sf_attachment_browser").set_value(identifier)
    app.run(timeout=30)
    app.button(key=f"attachment_save_{identifier}").click()
    app.run(timeout=30)
    assert not app.exception
    documents = AttachmentLibrary(path).documents()
    assert len(documents) == 1
    assert documents[0]["library_id"] == identifier
    assert any("Attachment saved" in success.value for success in app.success)


def test_reopened_message_for_old_saved_task_is_archived_without_ai_or_resave(tmp_path, monkeypatch):
    path = tmp_path / "records.sqlite3"
    monkeypatch.setenv("ATTACHMENTS_TEST_DB", str(path))
    mail = sample_mail()
    store = Store(path)
    store.save_task({"mail_id": mail["id"], "title": "Old task", "status": "pending"})
    app = AppTest.from_string(APP.replace(")), {})", ")), st.session_state.get('mails', {}))"))
    app.session_state["mails"] = {mail["id"]: mail}
    app.run(timeout=30)
    assert not app.exception
    library = AttachmentLibrary(path)
    assert len(library.documents()) == 1
    identifier = library.documents()[0]["library_id"]
    library.delete(identifier)
    app.run(timeout=30)
    assert not app.exception
    assert library.documents() == []
    assert not app.expander


def test_document_browser_keeps_same_named_sources_distinct_and_delete_is_scoped(tmp_path, monkeypatch):
    path = tmp_path / "records.sqlite3"
    monkeypatch.setenv("ATTACHMENTS_TEST_DB", str(path))
    first = sample_mail()
    second = {**first, "id": "message-2", "subject": "Another course assignment"}
    library = AttachmentLibrary(path)
    first_id = library.save_for_message(first)[0]
    second_id = library.save_for_message(second)[0]
    store = Store(path)
    store.save_task({"mail_id": second["id"], "title": "Retain this task", "status": "pending"})
    app = AppTest.from_string(APP).run(timeout=30)
    assert not app.exception
    assert len(set(app.radio(key="sf_attachment_browser").options)) == 2
    app.checkbox(key=f"attachment_delete_confirm_{first_id}").check()
    app.run(timeout=30)
    app.radio(key="sf_attachment_browser").set_value(second_id)
    app.run(timeout=30)
    assert not app.exception
    assert any("Another course assignment" in caption.value for caption in app.caption)
    assert not app.checkbox(key=f"attachment_delete_confirm_{second_id}").value
    assert app.button(key=f"attachment_delete_{second_id}").disabled
    assert len(app.get("download_button")) == 1
    app.checkbox(key=f"attachment_delete_confirm_{second_id}").check()
    app.run(timeout=30)
    app.button(key=f"attachment_delete_{second_id}").click()
    app.run(timeout=30)
    assert not app.exception
    assert [document["library_id"] for document in library.documents()] == [first_id]
    assert app.radio(key="sf_attachment_browser").value == first_id
    assert app.button(key=f"attachment_delete_{first_id}").disabled
    assert store.tasks()[0]["title"] == "Retain this task"


def test_document_filter_matches_source_but_keyword_search_still_covers_all_documents(tmp_path, monkeypatch):
    path = tmp_path / "records.sqlite3"
    monkeypatch.setenv("ATTACHMENTS_TEST_DB", str(path))
    library = AttachmentLibrary(path)
    library.save_for_message(sample_mail())
    app = AppTest.from_string(APP).run(timeout=30)
    app.text_input(key="sf_attachment_filter").set_value("COURSE")
    app.run(timeout=30)
    assert not app.exception
    assert len(app.radio(key="sf_attachment_browser").options) == 1
    app.text_input(key="sf_attachment_filter").set_value("not-a-document")
    app.run(timeout=30)
    assert not app.exception
    assert not app.radio
    assert not app.get("download_button")
    assert any("No documents match" in info.value for info in app.info)
    app.text_input(key="sf_attachment_search").set_value("research report")
    next(button for button in app.button if button.label == "Search").click()
    app.run(timeout=30)
    assert not app.exception
    assert any("research report" in text.value for text in app.text)
    app.text_input(key="sf_attachment_filter").set_value("")
    app.run(timeout=30)
    assert not app.exception
    assert len(app.radio(key="sf_attachment_browser").options) == 1


def test_same_filename_and_subject_have_unique_serialized_choices_across_reruns(tmp_path, monkeypatch):
    path = tmp_path / "records.sqlite3"
    monkeypatch.setenv("ATTACHMENTS_TEST_DB", str(path))
    first = sample_mail()
    second = {**first, "id": "message-2", "gmail_account": "another@example.test"}
    library = AttachmentLibrary(path)
    first_id = library.save_for_message(first)[0]
    second_id = library.save_for_message(second)[0]
    app = AppTest.from_string(APP).run(timeout=30)
    assert not app.exception
    options = app.radio(key="sf_attachment_browser").options
    assert options == [
        "1. Assignment.docx · Course assignment",
        "2. Assignment.docx · Course assignment",
    ]
    for identifier in (second_id, first_id, second_id):
        app.radio(key="sf_attachment_browser").set_value(identifier)
        app.run(timeout=30)
        assert not app.exception
        assert app.radio(key="sf_attachment_browser").value == identifier
        assert app.get("download_button")[0].key == f"attachment_download_{identifier}"
        app.run(timeout=30)
        assert not app.exception
        assert app.radio(key="sf_attachment_browser").value == identifier
