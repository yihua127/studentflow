"""Offline AppTest journeys for isolated email, discussion and general workspaces."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from studentflow import assistant_ui
from studentflow.assistant_context import source_key
from studentflow.reply_preferences import reply_preferences_path, save_reply_preferences

ACCOUNT = "student@example.invalid"
MAIL_ID = source_key("gmail", ACCOUNT, "selected-email")
CHAT_ID = source_key("google_chat", ACCOUNT, "spaces/team/messages/one")
DOCUMENT_ID = source_key("attachment", ACCOUNT, "discussion-brief")
MAIL_TEXT = "Please confirm receipt of the report instructions."
CHAT_TEXT = "Alex will prepare the outline. The group agreed to review it on Friday."
SEND_CONSENT = "Send this request and only the selected information to OpenAI."

SCRIPT = """
import os
from pathlib import Path
import streamlit as st
from studentflow.storage import Store
from studentflow.assistant_ui import render_assistant
from studentflow.workspace_ui import clear_workspace_state
root = Path(os.environ['SF_WORKSPACES_TEST_ROOT'])
store = Store(root / 'data' / 'studentflow.sqlite3')
account = os.environ.get('SF_WORKSPACES_TEST_ACCOUNT', 'student@example.invalid')
st.session_state['gmail_profile'] = {'email_address': account} if account else {}
if not st.session_state.get('test_sources_seeded'):
    st.session_state['sf_workspace_profile'] = {'email_address': 'student@example.invalid'}
    st.session_state['sf_workspace_messages'] = [{
        'name': 'spaces/team/messages/one', 'account': 'student@example.invalid',
        'title': 'Project discussion',
        'text': 'Alex will prepare the outline. The group agreed to review it on Friday.',
    }]
    st.session_state['test_sources_seeded'] = True
mails = {'selected-email': {
    'gmail_account': account, 'subject': 'Report instructions',
    'body': 'Please confirm receipt of the report instructions.',
    'sender': 'teacher@example.invalid', 'provider': 'gmail',
}}
st.button('Disconnect test discussion', on_click=clear_workspace_state)
render_assistant(store, root / 'data', root, mails, 'offline-api-key', 'offline-model')
"""


def checked(app):
    app.run(timeout=30)
    assert not app.exception
    return app


def find(elements, label):
    return next(element for element in elements if element.label == label)


def switch(app, name):
    app.radio(key="sf_assistant_workspace").set_value(name)
    return checked(app)


def run_workspace(app, prefix):
    find(app.checkbox, SEND_CONSENT).check()
    checked(app)
    app.button(key=prefix + "_run").click()
    return checked(app)


@pytest.fixture
def workspace_ui(tmp_path, monkeypatch):
    import streamlit as st

    monkeypatch.setenv("SF_WORKSPACES_TEST_ROOT", str(tmp_path))
    monkeypatch.setenv("SF_WORKSPACES_TEST_ACCOUNT", ACCOUNT)
    monkeypatch.setenv("STUDENTFLOW_GMAIL_CLIENT", str(tmp_path / "absent-client.json"))
    monkeypatch.setattr(assistant_ui, "available_documents", lambda *_args: [{
        "library_id": "discussion-brief", "filename": "discussion-brief.pdf",
        "gmail_account": ACCOUNT, "text": "The project brief supplies background context.",
    }])
    monkeypatch.setattr(assistant_ui, "render_workspace_sources", lambda *_args, **_kwargs: (
        st.session_state.get("sf_workspace_messages", []), [], {},
    ))
    captured = []

    def fake_run(request, toolbox, **_kwargs):
        captured.append({
            "request": request, "sources": deepcopy(toolbox._sources),
            "styles": deepcopy(toolbox._styles), "preferences": deepcopy(toolbox._preferences),
        })
        mail = next((item for item in toolbox._sources.values()
                     if item.get("provider") == "gmail"), None)
        chat = next((item for item in toolbox._sources.values()
                     if item.get("provider") == "google_chat"), None)
        if mail:
            result = toolbox.invoke("record_reply_draft", {
                "source_id": mail["source_id"], "subject": "Re: Report instructions",
                "body": "Hello,\n\nThank you. I confirm receipt.\n\nBest regards,\nTest Student",
                "recipient": "teacher@example.invalid",
                "citations": [{"source_id": mail["source_id"], "quote": MAIL_TEXT}],
                "style_source_ids": [sample["source_id"] for sample in toolbox._styles],
            }, role="email")
            assert result["ok"]
            answer = "EMAIL RESULT: the editable reply is ready."
        elif chat:
            result = toolbox.invoke("record_meeting_summary", {
                "title": "Synthetic project discussion", "summary": "MEETING SUMMARY: review the outline.",
                "citations": [{"source_id": chat["source_id"], "quote": CHAT_TEXT}],
                "agenda": ["Review the outline"], "follow_up": "Please review the outline.",
            }, role="meeting")
            assert result["ok"]
            answer = "MEETING RESULT: review the discussion summary."
        else:
            answer = "GENERAL RESULT: the request has been reviewed."
        return SimpleNamespace(answer=answer, status="completed", events=toolbox.events,
                               usage={"model_calls": 1}, artifacts=toolbox.artifacts)

    monkeypatch.setattr(assistant_ui, "run_assistant", fake_run)
    return tmp_path, captured


def test_modes_offer_relevant_sources_and_meeting_has_an_english_default(workspace_ui):
    app = checked(AppTest.from_string(SCRIPT))
    selector = app.radio(key="sf_assistant_workspace")
    assert selector.label == "Assistant workspace"
    assert selector.options == ["Email replies", "Meetings & discussions", "Other assistance"]
    assert selector.value == "Email replies"
    email_choices = app.multiselect(key="sf_assistant_sources").options
    assert any("Report instructions" in option for option in email_choices)
    assert any("discussion-brief.pdf" in option for option in email_choices)
    assert not any("Project discussion" in option for option in email_choices)

    switch(app, "Meetings & discussions")
    meeting_choices = app.multiselect(key="sf_meeting_sources").options
    assert any("Alex will prepare" in option for option in meeting_choices)
    assert not any("discussion-brief.pdf" in option for option in meeting_choices)
    assert any("discussion-brief.pdf" in option
               for option in app.multiselect(key="sf_meeting_attachments").options)
    assert not any("Report instructions" in option for option in meeting_choices)
    request = app.text_area(key="sf_meeting_request").value
    assert "summar" in request.casefold() and "action" in request.casefold()
    assert "English" in request
    assert all(element.key != "sf_assistant_style" for element in app.text_area)
    assert all(element.key != "sf_assistant_preferences" for element in app.checkbox)
    assert not any("reply preferences" in button.label.casefold() for button in app.button)
    assert not find(app.checkbox, SEND_CONSENT).value

    switch(app, "Other assistance")
    general_choices = app.multiselect(key="sf_general_sources").options
    assert any("Report instructions" in option for option in general_choices)
    assert not any("Project discussion" in option for option in general_choices)
    assert any("Alex will prepare" in option
               for option in app.multiselect(key="sf_general_messages").options)
    assert any("discussion-brief.pdf" in option for option in general_choices)
    assert all(element.key != "sf_assistant_style" for element in app.text_area)
    assert not any("reply preferences" in button.label.casefold() for button in app.button)


@pytest.mark.parametrize("change", ["request", "sources"])
def test_minutes_downloads_disappear_when_their_inputs_change(workspace_ui, change):
    _, captured = workspace_ui
    app = checked(AppTest.from_string(SCRIPT))
    switch(app, "Meetings & discussions")
    app.multiselect(key="sf_meeting_sources").set_value([CHAT_ID])
    checked(app)
    run_workspace(app, "sf_meeting")
    assert len(app.get("download_button")) == 3
    assert len(captured) == 1

    if change == "request":
        app.text_area(key="sf_meeting_request").set_value("Summarize only the agreed actions.")
    else:
        app.multiselect(key="sf_meeting_sources").set_value([])
    checked(app)
    assert not app.get("download_button")
    assert len(captured) == 1


def test_failed_meeting_displays_and_logs_safe_validation_reason(workspace_ui, monkeypatch):
    root, _ = workspace_ui
    monkeypatch.setattr(assistant_ui, "run_assistant", lambda *_args, **_kwargs: SimpleNamespace(
        answer="The discussion result still needs correction before recording.", status="partial",
        events=[
            {"role": "meeting", "status": "error", "tool": "record_meeting_summary",
             "error_code": "invalid_citation", "arguments": "PRIVATE SOURCE TEXT"},
            {"role": "meeting", "status": "error", "error_code": "meeting_needs_correction"},
        ], usage={"model_calls": 8}, artifacts={},
    ))
    app = checked(AppTest.from_string(SCRIPT))
    switch(app, "Meetings & discussions")
    app.multiselect(key="sf_meeting_sources").set_value([CHAT_ID])
    checked(app)
    run_workspace(app, "sf_meeting")
    table = next(frame.value for frame in app.dataframe if "error_code" in frame.value.columns)
    assert table.iloc[0]["error_code"] == "invalid_citation"
    assert "quotation" in table.iloc[0]["reason"]
    assert "PRIVATE" not in table.to_json()
    assert not app.get("download_button")
    logged = json.loads((root / "data" / "assistant_runs.jsonl").read_text().splitlines()[-1])
    assert logged["errors"][0]["error_code"] == "invalid_citation"
    assert "PRIVATE" not in json.dumps(logged)


def test_unsaved_inputs_and_source_choices_survive_multiple_workspace_switches(workspace_ui):
    app = checked(AppTest.from_string(SCRIPT))
    email_request = "Draft a short receipt acknowledgment in English."
    sample = "Thank you for your guidance.\nBest regards,\nTest Student"
    app.multiselect(key="sf_assistant_sources").set_value([MAIL_ID])
    app.text_area(key="sf_assistant_request").set_value(email_request)
    app.text_area(key="sf_assistant_style").set_value(sample)
    app.checkbox(key="sf_assistant_style_consent").check()
    checked(app)

    switch(app, "Meetings & discussions")
    meeting_request = "Summarize the discussion and list unresolved questions in English."
    app.multiselect(key="sf_meeting_sources").set_value([CHAT_ID])
    app.text_area(key="sf_meeting_request").set_value(meeting_request)
    checked(app)
    switch(app, "Other assistance")
    general_request = "Explain the selected project brief in English."
    app.multiselect(key="sf_general_sources").set_value([DOCUMENT_ID])
    app.text_area(key="sf_general_request").set_value(general_request)
    checked(app)

    switch(app, "Email replies")
    assert app.multiselect(key="sf_assistant_sources").value == [MAIL_ID]
    assert app.text_area(key="sf_assistant_request").value == email_request
    assert app.text_area(key="sf_assistant_style").value == sample
    assert app.checkbox(key="sf_assistant_style_consent").value
    switch(app, "Meetings & discussions")
    assert app.multiselect(key="sf_meeting_sources").value == [CHAT_ID]
    assert app.text_area(key="sf_meeting_request").value == meeting_request
    switch(app, "Other assistance")
    assert app.multiselect(key="sf_general_sources").value == [DOCUMENT_ID]
    assert app.text_area(key="sf_general_request").value == general_request


def test_meeting_run_excludes_email_style_and_keeps_saved_reply_preferences(workspace_ui):
    root, captured = workspace_ui
    save_reply_preferences(root / "data", ACCOUNT, style="EMAIL-STYLE-PRIVATE: Best, Test Student",
                           request="EMAIL-REQUEST-PRIVATE: write a reply.", style_approved=True)
    preference_file = reply_preferences_path(root / "data", ACCOUNT)
    before = preference_file.read_bytes()
    app = checked(AppTest.from_string(SCRIPT))
    app.multiselect(key="sf_assistant_sources").set_value([MAIL_ID])
    checked(app)
    find(app.checkbox, SEND_CONSENT).check()
    checked(app)

    switch(app, "Meetings & discussions")
    assert not find(app.checkbox, SEND_CONSENT).value
    assert app.button(key="sf_meeting_run").disabled
    app.multiselect(key="sf_meeting_sources").set_value([CHAT_ID])
    checked(app)
    run_workspace(app, "sf_meeting")
    assert len(captured) == 1
    assert list(captured[0]["sources"]) == [CHAT_ID]
    assert captured[0]["styles"] == [] and captured[0]["preferences"] == {}
    assert "EMAIL-REQUEST-PRIVATE" not in captured[0]["request"]
    assert "MEETING RESULT" in app.session_state["sf_meeting_result"]["result"].answer
    assert preference_file.read_bytes() == before

    app.text_area(key="sf_meeting_request").set_value("Now list open questions only.")
    checked(app)
    assert not find(app.checkbox, SEND_CONSENT).value
    assert app.button(key="sf_meeting_run").disabled
    assert preference_file.read_bytes() == before


def test_email_edits_and_independent_results_survive_running_the_meeting_workspace(workspace_ui):
    _, captured = workspace_ui
    app = checked(AppTest.from_string(SCRIPT))
    app.multiselect(key="sf_assistant_sources").set_value([MAIL_ID])
    app.text_area(key="sf_assistant_request").set_value("Draft a receipt acknowledgment.")
    checked(app)
    run_workspace(app, "sf_assistant")
    email_report_id = app.session_state["sf_assistant_result"]["id"]
    edited_body = "Hello,\nI have received the instructions.\nBest regards,\nReviewed Student"
    find(app.text_input, "Draft subject").set_value("Reviewed reply subject")
    find(app.text_area, "Draft message").set_value(edited_body)
    checked(app)

    switch(app, "Meetings & discussions")
    assert not any(element.label == "Draft message" for element in app.text_area)
    assert not any("EMAIL RESULT" in element.value for element in app.text)
    app.multiselect(key="sf_meeting_sources").set_value([CHAT_ID])
    checked(app)
    run_workspace(app, "sf_meeting")
    meeting_report_id = app.session_state["sf_meeting_result"]["id"]
    assert any("MEETING SUMMARY" in element.value for element in app.text)

    switch(app, "Other assistance")
    app.multiselect(key="sf_general_sources").set_value([DOCUMENT_ID])
    app.text_area(key="sf_general_request").set_value("Explain the brief.")
    checked(app)
    run_workspace(app, "sf_general")
    general_report_id = app.session_state["sf_general_result"]["id"]
    assert any("GENERAL RESULT" in element.value for element in app.text)

    switch(app, "Email replies")
    assert app.session_state["sf_assistant_result"]["id"] == email_report_id
    assert find(app.text_input, "Draft subject").value == "Reviewed reply subject"
    assert find(app.text_area, "Draft message").value == edited_body
    assert not any("MEETING RESULT" in element.value for element in app.text)
    switch(app, "Meetings & discussions")
    assert app.session_state["sf_meeting_result"]["id"] == meeting_report_id
    assert any("MEETING SUMMARY" in element.value for element in app.text)
    switch(app, "Other assistance")
    assert app.session_state["sf_general_result"]["id"] == general_report_id
    assert any("GENERAL RESULT" in element.value for element in app.text)
    assert len(captured) == 3


def test_disconnect_clears_discussion_result_and_does_not_restore_removed_sources(workspace_ui):
    app = checked(AppTest.from_string(SCRIPT))
    switch(app, "Meetings & discussions")
    app.multiselect(key="sf_meeting_sources").set_value([CHAT_ID])
    checked(app)
    run_workspace(app, "sf_meeting")
    assert "sf_meeting_result" in app.session_state
    find(app.button, "Disconnect test discussion").click()
    checked(app)
    assert "sf_meeting_result" not in app.session_state
    assert app.multiselect(key="sf_meeting_sources").value == []
    assert not any("Project discussion" in option
                   for option in app.multiselect(key="sf_meeting_sources").options)
    assert not any("MEETING RESULT" in element.value for element in app.text)
    assert not find(app.checkbox, SEND_CONSENT).value
    switch(app, "Email replies")
    switch(app, "Meetings & discussions")
    assert "sf_meeting_result" not in app.session_state
    assert app.multiselect(key="sf_meeting_sources").value == []


def test_gmail_account_change_while_email_is_hidden_invalidates_the_old_reply(workspace_ui, monkeypatch):
    app = checked(AppTest.from_string(SCRIPT))
    app.multiselect(key="sf_assistant_sources").set_value([MAIL_ID])
    app.text_area(key="sf_assistant_request").set_value("Draft the reply for account one.")
    app.text_area(key="sf_assistant_style").set_value("Private signature for account one")
    app.checkbox(key="sf_assistant_style_consent").check()
    checked(app)
    run_workspace(app, "sf_assistant")
    switch(app, "Meetings & discussions")
    monkeypatch.setenv("SF_WORKSPACES_TEST_ACCOUNT", "another@example.invalid")
    checked(app)
    switch(app, "Email replies")
    assert not any(element.label == "Draft message" for element in app.text_area)
    assert "sf_assistant_result" not in app.session_state
    assert app.multiselect(key="sf_assistant_sources").value == []
    assert app.text_area(key="sf_assistant_style").value == ""
    assert not app.checkbox(key="sf_assistant_style_consent").value
    assert not find(app.checkbox, SEND_CONSENT).value


def test_inbox_reply_handoff_activates_email_workspace_without_overwriting_meeting_request(workspace_ui):
    script = SCRIPT.replace("render_assistant(store", """
from studentflow.reply_handoff import queue_reply_handoff
if not st.session_state.get('test_reply_queued'):
    st.session_state['sf_assistant_workspace'] = 'Meetings & discussions'
    st.session_state['sf_meeting_request'] = 'Keep my unfinished meeting request.'
    queue_reply_handoff(st.session_state, account, 'selected-email', mails)
    st.session_state['test_reply_queued'] = True
render_assistant(store""")
    app = checked(AppTest.from_string(script))
    assert app.radio(key="sf_assistant_workspace").value == "Email replies"
    assert app.multiselect(key="sf_assistant_sources").value == [MAIL_ID]
    assert app.text_area(key="sf_assistant_request").value.strip()
    assert not find(app.checkbox, SEND_CONSENT).value
    switch(app, "Meetings & discussions")
    assert app.text_area(key="sf_meeting_request").value == "Keep my unfinished meeting request."


@pytest.mark.parametrize("handoff_matches_current_account", [True, False])
def test_account_change_preserves_only_a_matching_already_queued_handoff(
    workspace_ui, monkeypatch, handoff_matches_current_account,
):
    root, captured = workspace_ui
    current_account = "new-account@example.invalid"
    save_reply_preferences(root / "data", ACCOUNT, style="Old account signature",
                           request="Old account reply request.", style_approved=True)
    new_style = "New account signature"
    new_request = "Use the new account's saved reply request."
    save_reply_preferences(root / "data", current_account, style=new_style,
                           request=new_request, style_approved=True)
    old_path = reply_preferences_path(root / "data", ACCOUNT)
    new_path = reply_preferences_path(root / "data", current_account)
    old_bytes, new_bytes = old_path.read_bytes(), new_path.read_bytes()
    monkeypatch.setenv("SF_WORKSPACES_TEST_ACCOUNT", current_account)
    setup = """
from studentflow.reply_handoff import queue_reply_handoff
if not st.session_state.get('test_account_handoff_queued'):
    st.session_state['sf_reply_preferences_scope'] = (
        str((root / 'data').resolve()) + ':student@example.invalid'
    )
    st.session_state['sf_assistant_style'] = 'Old unsaved account signature'
    st.session_state['sf_assistant_style_consent'] = True
    st.session_state['sf_assistant_request'] = 'Old unsaved account reply request.'
    st.session_state['sf_meeting_request'] = 'Preserve the unfinished discussion request.'
    pending_account = account if MATCHES_CURRENT_ACCOUNT else 'student@example.invalid'
    pending_mail = dict(mails['selected-email'], gmail_account=pending_account)
    queue_reply_handoff(st.session_state, pending_account, 'selected-email',
                        {'selected-email': pending_mail})
    st.session_state['test_account_handoff_queued'] = True
""".replace("MATCHES_CURRENT_ACCOUNT", str(handoff_matches_current_account))
    script = SCRIPT.replace("render_assistant(store", setup + "\nrender_assistant(store")
    app = checked(AppTest.from_string(script))
    expected = ([source_key("gmail", current_account, "selected-email")]
                if handoff_matches_current_account else [])
    assert app.radio(key="sf_assistant_workspace").value == "Email replies"
    assert app.multiselect(key="sf_assistant_sources").value == expected
    assert app.text_area(key="sf_assistant_request").value == new_request
    assert app.text_area(key="sf_assistant_style").value == new_style
    assert app.checkbox(key="sf_assistant_style_consent").value
    assert not find(app.checkbox, SEND_CONSENT).value
    assert "sf_reply_handoff" not in app.session_state
    assert captured == []
    assert old_path.read_bytes() == old_bytes and new_path.read_bytes() == new_bytes
    switch(app, "Meetings & discussions")
    assert app.text_area(key="sf_meeting_request").value == "Preserve the unfinished discussion request."
