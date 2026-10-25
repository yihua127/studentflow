"""Assistant journeys use isolated records, scripted results and no external requests."""

import json
from datetime import date
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest
from streamlit.testing.v1.errors import AppTestError

from studentflow import assistant_ui, workspace_ui
from studentflow.assistant_context import (
    run_metadata,
    select_records,
    selectable_sources,
    snapshot_signature,
    source_key,
)
from studentflow.storage import Store

SCRIPT = """
import os
from pathlib import Path
from studentflow.storage import Store
from studentflow.assistant_ui import render_assistant
root = Path(os.environ['SF_ASSISTANT_TEST_ROOT'])
store = Store(root / 'data' / 'studentflow.sqlite3')
mails = {
    'one': {'gmail_account': 'student@example.invalid', 'subject': 'Selected report',
            'body': 'Submit the report on Friday.', 'sender': 'teacher@example.invalid'},
    'two': {'gmail_account': 'student@example.invalid', 'subject': 'Unselected private message',
            'body': 'PRIVATE-MESSAGE-NEVER-SEND', 'sender': 'private@example.invalid'},
}
render_assistant(store, root / 'data', root, mails, 'sk-offline-test', os.environ.get('SF_ASSISTANT_TEST_MODEL', 'test-model'))
"""


def find(elements, label):
    return next(item for item in elements if item.label == label)


def checked(app):
    app.run(timeout=30)
    assert not app.exception
    return app


@pytest.fixture
def setup_ui(tmp_path, monkeypatch):
    monkeypatch.setenv("SF_ASSISTANT_TEST_ROOT", str(tmp_path))
    monkeypatch.setenv("STUDENTFLOW_GMAIL_CLIENT", str(tmp_path / "client.json"))

    def forbidden(*args, **kwargs):
        raise AssertionError("No external service is called by these tests.")

    monkeypatch.setattr(workspace_ui, "connect_workspace", forbidden)
    return tmp_path


def test_source_normalization_keeps_account_boundaries_and_excludes_bytes():
    mails = {
        "same": {"gmail_account": "a@example.invalid", "body": "a", "subject": "A",
                 "api_key": "never", "attachments": [{"content": b"private"}]},
    }
    documents = [{"library_id": "document", "text": "b", "filename": "brief.pdf",
                  "content": b"private", "gmail_account": "a@example.invalid"}]
    chats = [{"name": "spaces/ONE/messages/TWO", "account": "a@example.invalid",
              "text": "c", "access_token": "never"}]
    sources = selectable_sources(mails, documents, chats)
    encoded = json.dumps(sources)
    assert len(sources) == 3
    assert "private" not in encoded and "never" not in encoded
    assert source_key("gmail", "a@example.invalid", "same") != source_key("gmail", "b@example.invalid", "same")
    assert source_key("gmail", "A@example.invalid", "same") == source_key("gmail", "a@example.invalid", "same")
    assert selectable_sources({}, [], [{"name": "spaces/a/messages/b", "text": ""}]) == []


def test_selection_is_exact_and_cannot_mutate_sources():
    sources = [{"source_id": "one", "text": "public"}, {"source_id": "two", "text": "private"}]
    selected = select_records(sources, ["one", "one"], "source_id")
    assert len(selected) == 1
    selected[0]["text"] = "edited"
    assert sources[0]["text"] == "public"
    with pytest.raises(ValueError, match="no longer available"):
        select_records(sources, ["not-selected"], "source_id")


def test_snapshot_and_usage_are_safe_for_staleness_and_logging():
    snapshot = {"now": "2030-01-01T00:00:00Z", "tasks": [{"id": "a", "completed_minutes": 2}]}
    assert snapshot_signature(snapshot) == snapshot_signature(snapshot | {"now": "2030-01-02T00:00:00Z"})
    assert snapshot_signature(snapshot) != snapshot_signature(snapshot | {"tasks": [{"id": "a", "completed_minutes": 3}]})
    result = SimpleNamespace(
        status="completed", answer="PRIVATE", artifacts={"secret": "PRIVATE"},
        events=[{"arguments": "PRIVATE"}],
        usage={"input_tokens": 10, "output_tokens": 20, "secret": "PRIVATE"},
    )
    metadata = run_metadata(result, "test-model", "now", 1.235)
    assert "PRIVATE" not in json.dumps(metadata)
    assert metadata["usage"] == {"input_tokens": 10, "output_tokens": 20}
    result.usage.update(usage_complete=False, unreported_model_calls=1)
    metadata = run_metadata(result, "test-model", "now", 1.235)
    assert metadata["usage"]["usage_complete"] is False
    assert metadata["usage"]["unreported_model_calls"] == 1


def test_academic_references_require_complete_ordered_dates():
    assert assistant_ui.academic_dates([{"Name": "", "Start": None, "End": None}]) == []
    references = assistant_ui.academic_dates([
        {"Name": "Reading week", "Start": date(2030, 10, 7), "End": date(2030, 10, 13), "Reference": "Course calendar"}
    ])
    assert references[0]["start"] == "2030-10-07"
    assert references[0]["source"] == "Course calendar"
    with pytest.raises(ValueError):
        assistant_ui.academic_dates([{"Name": "Reading week", "Start": None, "End": None}])
    with pytest.raises(ValueError):
        assistant_ui.academic_dates([{"Name": "Week", "Start": "2030-10-13", "End": "2030-10-07"}])


def test_assistant_requires_scope_bound_consent_and_only_reads_selected_sources(setup_ui, monkeypatch):
    calls = []

    def fake_run(request, toolbox, **kwargs):
        calls.append((request, list(toolbox._sources), list(toolbox._tasks)))
        source_id = next(iter(toolbox._sources))
        recorded = toolbox.invoke("record_reply_draft", {
            "source_id": source_id, "subject": "Re: Selected report", "body": "Thank you.",
            "recipient": "teacher@example.invalid",
            "citations": [{"source_id": source_id, "quote": "Submit the report on Friday."}],
        }, role="email")
        assert recorded["ok"]
        return SimpleNamespace(answer="A reply draft is ready for review.", status="completed",
                               events=toolbox.events, usage={"model_calls": 3}, artifacts=toolbox.artifacts)

    monkeypatch.setattr(assistant_ui, "run_assistant", fake_run)
    app = checked(AppTest.from_string(SCRIPT))
    assert calls == []
    assert app.multiselect(key="sf_assistant_sources").value == []
    assert not any(item.label == "Saved tasks to use" for item in app.multiselect)
    assert find(app.button, "Run assistant").disabled
    sid = source_key("gmail", "student@example.invalid", "one")
    app.multiselect(key="sf_assistant_sources").set_value([sid])
    app.text_area(key="sf_assistant_request").set_value("Draft a reply.")
    checked(app)
    assert calls == [] and find(app.button, "Run assistant").disabled
    find(app.checkbox, "Send this request and only the selected information to OpenAI.").check()
    checked(app)
    find(app.button, "Run assistant").click()
    checked(app)
    assert calls == [("Draft a reply.", [sid], [])]
    assert find(app.text_area, "Draft message").value == "Thank you."
    assert Store(setup_ui / "data" / "studentflow.sqlite3").tasks() == []
    assert not any(button.label in {"Send", "Send email"} for button in app.button)
    log = (setup_ui / "data" / "assistant_runs.jsonl").read_text(encoding="utf-8")
    assert "Submit the report" not in log and "Thank you" not in log and "sk-offline" not in log
    app.text_area(key="sf_assistant_request").set_value("Now change the request.")
    checked(app)
    assert not find(app.checkbox, "Send this request and only the selected information to OpenAI.").value
    assert find(app.button, "Run assistant").disabled
    assert any("changed" in item.value for item in app.warning)
    assert not any(item.label == "Draft message" for item in app.text_area)
    app.text_area(key="sf_assistant_request").set_value("Draft a reply.")
    monkeypatch.setenv("SF_ASSISTANT_TEST_MODEL", "changed-model")
    checked(app)
    assert not any(item.label == "Draft message" for item in app.text_area)
    assert find(app.button, "Run assistant").disabled


def test_assistant_failure_does_not_expose_provider_exception(setup_ui, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("SECRET-KEY PRIVATE-MESSAGE-NEVER-SEND")

    monkeypatch.setattr(assistant_ui, "run_assistant", fail)
    app = checked(AppTest.from_string(SCRIPT))
    app.text_area(key="sf_assistant_request").set_value("Help with my tasks.")
    checked(app)
    find(app.checkbox, "Send this request and only the selected information to OpenAI.").check()
    checked(app)
    find(app.button, "Run assistant").click()
    checked(app)
    assert any("could not complete" in item.value for item in app.error)
    assert "SECRET-KEY" not in " ".join(item.value for item in app.error)
    assert not (setup_ui / "data" / "assistant_runs.jsonl").exists()


def test_email_and_approved_style_survive_separate_input_reruns(setup_ui, monkeypatch):
    captured = []

    def fake_run(request, toolbox, **kwargs):
        captured.append({
            "request": request, "sources": dict(toolbox._sources),
            "samples": toolbox.invoke("get_style_samples", {}, role="email"),
        })
        return SimpleNamespace(answer="Ready for review.", status="completed",
                               events=toolbox.events, usage={}, artifacts=toolbox.artifacts)

    monkeypatch.setattr(assistant_ui, "run_assistant", fake_run)
    app = checked(AppTest.from_string(SCRIPT))
    sid = source_key("gmail", "student@example.invalid", "one")
    app.multiselect(key="sf_assistant_sources").set_value([sid])
    checked(app)
    sample = "Dear Professor,\nThank you for your guidance.\nBest regards,\nStudent Example"
    app.text_area(key="sf_assistant_style").set_value(sample)
    checked(app)
    app.checkbox(key="sf_assistant_style_consent").check()
    checked(app)
    app.text_area(key="sf_assistant_request").set_value("Draft an English reply using my writing style.")
    checked(app)
    assert app.multiselect(key="sf_assistant_sources").value == [sid]
    assert app.checkbox(key="sf_assistant_style_consent").value
    assert any("1 email(s) · 0 other source(s) · 1 approved writing sample(s)" in item.value
               for item in app.caption)
    assert not captured and find(app.button, "Run assistant").disabled
    find(app.checkbox, "Send this request and only the selected information to OpenAI.").check()
    checked(app)
    find(app.button, "Run assistant").click()
    checked(app)
    assert len(captured) == 1
    assert list(captured[0]["sources"]) == [sid]
    assert captured[0]["sources"][sid]["text"] == "Submit the report on Friday."
    assert "PRIVATE-MESSAGE-NEVER-SEND" not in json.dumps(captured)
    assert captured[0]["samples"]["ok"]
    assert captured[0]["samples"]["samples"][0]["text"] == sample


def test_attachment_only_selection_still_explains_how_to_select_reply_email(setup_ui, monkeypatch):
    monkeypatch.setattr(assistant_ui, "available_documents", lambda *args: [{
        "library_id": "brief", "filename": "brief.pdf", "text": "Synthetic assignment brief.",
        "gmail_account": "student@example.invalid",
    }])
    script = SCRIPT.replace("root, mails, 'sk-offline-test'", "root, {}, 'sk-offline-test'")
    app = checked(AppTest.from_string(script))
    assert app.multiselect(key="sf_assistant_sources").value == []
    assert any("View selected messages" in item.value and "Gmail entry" in item.value
               for item in app.caption)
    app.multiselect(key="sf_assistant_sources").set_value([
        source_key("attachment", "student@example.invalid", "brief"),
    ])
    checked(app)
    assert any("0 email(s) · 1 other source(s) · 0 approved writing sample(s)" in item.value
               for item in app.caption)
    assert any("Saved attachments and writing samples do not include the email itself." in item.value
               for item in app.caption)


def test_unapproved_style_is_excluded_and_general_requests_remain_available(setup_ui, monkeypatch):
    captured = []

    def fake_run(request, toolbox, **kwargs):
        captured.append(list(toolbox._styles))
        return SimpleNamespace(answer="Style guidance.", status="completed",
                               events=toolbox.events, usage={}, artifacts=toolbox.artifacts)

    monkeypatch.setattr(assistant_ui, "run_assistant", fake_run)
    app = checked(AppTest.from_string(SCRIPT))
    app.text_area(key="sf_assistant_style").set_value("Thanks for your help. Best, Student Example")
    app.text_area(key="sf_assistant_request").set_value("Explain how to make an email concise.")
    checked(app)
    assert any("Your writing sample is not included" in item.value for item in app.warning)
    assert any("0 email(s) · 0 other source(s) · 0 approved writing sample(s)" in item.value
               for item in app.caption)
    find(app.checkbox, "Send this request and only the selected information to OpenAI.").check()
    checked(app)
    assert not find(app.button, "Run assistant").disabled
    find(app.button, "Run assistant").click()
    checked(app)
    assert captured == [[]]
    app.checkbox(key="sf_assistant_style_consent").check()
    app.text_area(key="sf_assistant_request").set_value("Analyze the tone of my writing sample.")
    checked(app)
    assert any("No email or other source is selected" in item.value for item in app.info)
    assert any("0 email(s) · 0 other source(s) · 1 approved writing sample(s)" in item.value
               for item in app.caption)
    assert not find(app.checkbox, "Send this request and only the selected information to OpenAI.").value
    find(app.checkbox, "Send this request and only the selected information to OpenAI.").check()
    checked(app)
    assert not find(app.button, "Run assistant").disabled
    find(app.button, "Run assistant").click()
    checked(app)
    assert len(captured) == 2 and len(captured[1]) == 1


def _account_script():
    return SCRIPT.replace("store = Store", "import streamlit as st\nst.session_state['gmail_profile'] = "
                          "{'email_address': os.environ.get('SF_REPLY_TEST_ACCOUNT', 'student@example.invalid')}\nstore = Store")


def test_reply_preferences_survive_new_browser_session_without_openai_consent(setup_ui):
    app = checked(AppTest.from_string(_account_script()))
    sample = "Thank you for your guidance.\n\nBest regards,\nALEX TAN"
    app.text_area(key="sf_assistant_style").set_value(sample)
    app.checkbox(key="sf_assistant_style_consent").check()
    app.text_area(key="sf_assistant_request").set_value("Draft an English reply without template placeholders.")
    checked(app)
    find(app.checkbox, "Send this request and only the selected information to OpenAI.").check()
    checked(app)
    find(app.button, "Save reply preferences").click()
    checked(app)
    assert any("Reply preferences saved" in item.value for item in app.success)

    reopened = checked(AppTest.from_string(_account_script()))
    assert reopened.text_area(key="sf_assistant_style").value == sample
    assert reopened.checkbox(key="sf_assistant_style_consent").value
    assert reopened.text_area(key="sf_assistant_request").value == "Draft an English reply without template placeholders."
    assert not find(reopened.checkbox, "Send this request and only the selected information to OpenAI.").value
    assert find(reopened.button, "Run assistant").disabled
    assert reopened.multiselect(key="sf_assistant_sources").value == []


def test_reply_preferences_load_clear_and_account_switch_are_isolated(setup_ui, monkeypatch):
    app = checked(AppTest.from_string(_account_script()))
    app.text_area(key="sf_assistant_style").set_value("Best regards, ALEX TAN")
    app.text_area(key="sf_assistant_request").set_value("Use my saved reply request.")
    checked(app)
    find(app.button, "Save reply preferences").click()
    checked(app)
    assert not app.checkbox(key="sf_assistant_style_consent").value
    app.text_area(key="sf_assistant_request").set_value("A temporary request.")
    checked(app)
    find(app.checkbox, "Send this request and only the selected information to OpenAI.").check()
    checked(app)
    find(app.button, "Load saved reply preferences").click()
    checked(app)
    assert app.text_area(key="sf_assistant_request").value == "Use my saved reply request."
    assert not find(app.checkbox, "Send this request and only the selected information to OpenAI.").value

    monkeypatch.setenv("SF_REPLY_TEST_ACCOUNT", "another@example.invalid")
    checked(app)
    assert app.text_area(key="sf_assistant_style").value == ""
    assert app.text_area(key="sf_assistant_request").value == ""
    app.text_area(key="sf_assistant_request").set_value("Another account request.")
    checked(app)
    find(app.button, "Save reply preferences").click()
    checked(app)
    monkeypatch.setenv("SF_REPLY_TEST_ACCOUNT", "student@example.invalid")
    checked(app)
    assert app.text_area(key="sf_assistant_request").value == "Use my saved reply request."
    find(app.button, "Clear saved reply preferences").click()
    checked(app)
    assert app.text_area(key="sf_assistant_request").value == ""
    assert app.text_area(key="sf_assistant_style").value == ""
    assert not app.checkbox(key="sf_assistant_style_consent").value
    reopened = checked(AppTest.from_string(_account_script()))
    assert reopened.text_area(key="sf_assistant_request").value == ""
    monkeypatch.setenv("SF_REPLY_TEST_ACCOUNT", "another@example.invalid")
    another = checked(AppTest.from_string(_account_script()))
    assert another.text_area(key="sf_assistant_request").value == "Another account request."


def test_saved_preferences_reload_after_leaving_assistant_page(setup_ui):
    from studentflow.reply_preferences import save_reply_preferences

    save_reply_preferences(setup_ui / "data", "student@example.invalid", style="Best, ALEX TAN",
                           request="Draft a concise reply.", style_approved=True)
    script = _account_script().replace("render_assistant(store", "if st.checkbox('Show assistant', value=True):\n    render_assistant(store")
    app = checked(AppTest.from_string(script))
    assert app.text_area(key="sf_assistant_request").value == "Draft a concise reply."
    find(app.checkbox, "Show assistant").uncheck()
    checked(app)
    assert not any(item.label == "What would you like help with?" for item in app.text_area)
    find(app.checkbox, "Show assistant").check()
    checked(app)
    assert app.text_area(key="sf_assistant_request").value == "Draft a concise reply."
    assert app.text_area(key="sf_assistant_style").value == "Best, ALEX TAN"
    assert app.checkbox(key="sf_assistant_style_consent").value
    assert not find(app.checkbox, "Send this request and only the selected information to OpenAI.").value


def test_unreadable_reply_preferences_are_reported_and_preserved(setup_ui):
    from studentflow.reply_preferences import reply_preferences_path

    path = reply_preferences_path(setup_ui / "data", "student@example.invalid")
    path.parent.mkdir(parents=True)
    path.write_text("BROKEN PREFERENCES", encoding="utf-8")
    app = checked(AppTest.from_string(_account_script()))
    assert any("saved file has been kept" in item.value for item in app.warning)
    app.text_area(key="sf_assistant_request").set_value("My new request.")
    checked(app)
    find(app.button, "Save reply preferences").click()
    checked(app)
    assert path.read_text(encoding="utf-8") == "BROKEN PREFERENCES"
    assert any("Existing saved data has been kept" in item.value for item in app.warning)


def _handoff_script():
    return _account_script().replace("render_assistant(store", """
from studentflow.reply_handoff import queue_reply_handoff
if not st.session_state.get('test_handoff_queued'):
    mails['one']['provider'] = 'gmail'
    queue_reply_handoff(st.session_state, 'student@example.invalid', 'one', mails)
    st.session_state['test_handoff_queued'] = True
render_assistant(store""")


def test_reply_handoff_uses_saved_request_and_selects_only_target(setup_ui):
    from studentflow.reply_preferences import save_reply_preferences

    save_reply_preferences(setup_ui / "data", "student@example.invalid", style="Best, ALEX TAN",
                           request="Use my preferred reply wording.", style_approved=True)
    app = checked(AppTest.from_string(_handoff_script()))
    assert app.multiselect(key="sf_assistant_sources").value == [source_key("gmail", "student@example.invalid", "one")]
    assert app.text_area(key="sf_assistant_request").value == "Use my preferred reply wording."
    assert app.checkbox(key="sf_assistant_style_consent").value
    assert not find(app.checkbox, "Send this request and only the selected information to OpenAI.").value


def test_reply_handoff_supplies_default_request_when_blank(setup_ui):
    from studentflow.reply_preferences import DEFAULT_REPLY_REQUEST

    app = checked(AppTest.from_string(_handoff_script()))
    assert app.text_area(key="sf_assistant_request").value == DEFAULT_REPLY_REQUEST


def test_reply_send_controls_receive_current_edited_subject_and_body(setup_ui, monkeypatch):
    import sys

    captured = []

    def capture(root, data_dir, item, source, subject, body, report_id):
        captured.append((subject, body, source["resource_id"], report_id))

    monkeypatch.setitem(sys.modules, "studentflow.reply_send_ui", SimpleNamespace(render_reply_send=capture))
    app = checked(AppTest.from_string("""
import os
from pathlib import Path
from studentflow.assistant_ui import _render_artifacts
root = Path(os.environ['SF_ASSISTANT_TEST_ROOT'])
_render_artifacts({'reply_drafts': [{'id': 'draft-one', 'source_id': 'mail-one',
                  'subject': 'Original subject', 'body': 'Original message',
                  'recipient': 'teacher@example.invalid'}]},
                  {'mail-one': {'resource_id': 'message-one'}}, 'report-one',
                  root=root, data_dir=root / 'data')
"""))
    assert captured[-1] == ("Original subject", "Original message", "message-one", "report-one")
    find(app.text_input, "Draft subject").set_value("Reviewed subject")
    find(app.text_area, "Draft message").set_value("Reviewed message.\n\nBest regards,\nALEX TAN")
    checked(app)
    assert captured[-1] == (
        "Reviewed subject", "Reviewed message.\n\nBest regards,\nALEX TAN", "message-one", "report-one",
    )


def test_google_reads_are_explicit_and_disconnecting_clears_loaded_sources(setup_ui, monkeypatch):
    (setup_ui / "client.json").write_text("{}", encoding="utf-8")
    calls = []

    class Client:
        def close(self):
            pass

        def profile(self):
            return {"email_address": "student@example.invalid"}

        def list_spaces(self):
            calls.append("spaces")
            return [{"name": "spaces/ONE", "display_name": "Project", "space_type": "SPACE"}]

        def list_messages(self, name):
            calls.append(("messages", name))
            return [{"name": "spaces/ONE/messages/TWO", "source_id": "chat-one",
                     "account": "student@example.invalid", "title": "Project discussion",
                     "text": "We agreed to submit the report on Friday."}]

    def connect(client_path, token_path, interactive=False):
        calls.append(("connect", interactive))
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text("{}", encoding="utf-8")
        return Client()

    monkeypatch.setattr(workspace_ui, "connect_workspace", connect)
    app = checked(AppTest.from_string(SCRIPT))
    assert calls == []
    app.radio(key="sf_assistant_workspace").set_value("Other assistance")
    checked(app)
    find(app.checkbox, "Allow StudentFlow to read my Google Chat and Calendar.").check()
    checked(app)
    find(app.button, "Connect Google").click()
    checked(app)
    assert calls == [("connect", False)]
    find(app.checkbox, "Allow StudentFlow to read my Google Chat and Calendar.").uncheck()
    checked(app)
    assert find(app.button, "Load Google Chat spaces").disabled
    assert find(app.button, "Load Google calendars").disabled
    find(app.checkbox, "Allow StudentFlow to read my Google Chat and Calendar.").check()
    checked(app)
    find(app.button, "Load Google Chat spaces").click()
    checked(app)
    assert "spaces" in calls and not any(isinstance(call, tuple) and call[0] == "messages" for call in calls)
    find(app.button, "Read selected discussion").click()
    checked(app)
    assert ("messages", "spaces/ONE") in calls
    assert any("We agreed" in option for option in app.multiselect(key="sf_general_messages").options)
    find(app.button, "Disconnect Google").click()
    checked(app)
    assert not app.multiselect(key="sf_general_messages").options
    assert not (setup_ui / "data" / "auth" / "google_workspace_token.json").exists()


def test_invalid_previous_google_consent_falls_back_to_sign_in(setup_ui, monkeypatch):
    (setup_ui / "client.json").write_text("{}", encoding="utf-8")
    prior = setup_ui / "credentials" / "google_workspace_probe_token.json"
    prior.parent.mkdir()
    prior.write_text("invalid synthetic token", encoding="utf-8")
    calls = []

    def reject_import(*args, **kwargs):
        raise workspace_ui.GoogleWorkspaceAuthError("The previous authorization is invalid.")

    def connect(client_path, token_path, interactive=False):
        calls.append(interactive)
        if not interactive:
            raise workspace_ui.GoogleWorkspaceAuthError("Not connected.")
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text("{}", encoding="utf-8")
        return SimpleNamespace(profile=lambda: {"email_address": "new@example.invalid"}, close=lambda: None)

    monkeypatch.setattr(workspace_ui, "import_workspace_token", reject_import)
    monkeypatch.setattr(workspace_ui, "connect_workspace", connect)
    app = checked(AppTest.from_string(SCRIPT))
    app.radio(key="sf_assistant_workspace").set_value("Other assistance")
    checked(app)
    find(app.checkbox, "Allow StudentFlow to read my Google Chat and Calendar.").check()
    checked(app)
    find(app.button, "Connect Google").click()
    checked(app)
    assert calls == [False, True]
    assert not app.error
    assert prior.read_text(encoding="utf-8") == "invalid synthetic token"


def test_calendar_scope_changes_and_account_switch_clear_cached_information(setup_ui, monkeypatch):
    (setup_ui / "client.json").write_text("{}", encoding="utf-8")
    account = {"email": "first@example.invalid"}

    class Client:
        def close(self):
            pass

        def profile(self):
            return {"email_address": account["email"]}

        def list_calendars(self):
            return [{"id": "first", "title": "Class", "time_zone": "Asia/Singapore"}]

        def list_events(self, *args):
            return []

    def connect(client_path, token_path, interactive=False):
        if interactive:
            account["email"] = "second@example.invalid"
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text("{}", encoding="utf-8")
        return Client()

    monkeypatch.setattr(workspace_ui, "connect_workspace", connect)
    app = checked(AppTest.from_string(SCRIPT))
    app.radio(key="sf_assistant_workspace").set_value("Other assistance")
    checked(app)
    find(app.checkbox, "Allow StudentFlow to read my Google Chat and Calendar.").check()
    checked(app)
    find(app.button, "Connect Google").click()
    checked(app)
    find(app.button, "Load Google calendars").click()
    checked(app)
    app.multiselect(key="sf_workspace_calendar_selection").set_value(["first"])
    checked(app)
    find(app.button, "Read selected calendars").click()
    checked(app)
    assert not find(app.checkbox, "Include the loaded calendar commitments").disabled
    assert app.session_state["sf_workspace_coverage"]["account"] == "first@example.invalid"
    app.multiselect(key="sf_workspace_calendar_selection").set_value([])
    checked(app)
    assert find(app.checkbox, "Include the loaded calendar commitments").disabled
    find(app.button, "Switch Google account").click()
    checked(app)
    assert app.session_state["sf_workspace_profile"]["email_address"] == "second@example.invalid"
    assert not any(item.label == "Calendars to read" for item in app.multiselect)
    # A disconnect in another window invalidates this session's cached account.
    (setup_ui / "data" / "auth" / "google_workspace_token.json").unlink()
    checked(app)
    assert not any("Google connected" in item.value for item in app.success)


@pytest.fixture
def effort_review(setup_ui, monkeypatch):
    """Create a real saved task and a scripted, source-grounded AI estimate."""
    store = Store(setup_ui / "data" / "studentflow.sqlite3")
    task_id = store.save_task({
        "title": "Write report", "due_at": "2030-10-25T17:00:00+08:00",
        "estimated_minutes": 90, "completed_minutes": 30, "status": "confirmed",
        "mail_id": "one", "source_quote": "Submit the report on Friday.",
    })
    original = store.tasks()[0]

    def fake_run(request, toolbox, **kwargs):
        source_id = next(iter(toolbox._sources))
        proposal = {
            "title": "A proposed title that must not be saved", "relation": "update",
            "existing_task_id": task_id, "due_at": "2030-10-30T17:00:00+08:00",
            "suggested_minutes": 150,
            "estimate_reason": "Allow time to research, draft and proofread; review the complexity.",
            "citations": [{"source_id": source_id, "quote": "Submit the report on Friday."}],
        }
        recorded = toolbox.invoke("record_task_proposals", {"proposals": [proposal]}, role="task")
        assert recorded["ok"]
        return SimpleNamespace(answer="Review the suggested total study time.", status="completed",
                               events=toolbox.events, usage={}, artifacts=toolbox.artifacts)

    monkeypatch.setattr(assistant_ui, "run_assistant", fake_run)
    app = checked(AppTest.from_string(SCRIPT))
    app.radio(key="sf_assistant_workspace").set_value("Other assistance")
    checked(app)
    app.multiselect(key="sf_general_sources").set_value([
        source_key("gmail", "student@example.invalid", "one"),
    ])
    app.multiselect(key="sf_general_tasks").set_value([task_id])
    app.text_area(key="sf_general_request").set_value("Suggest a total study time for this task.")
    checked(app)
    find(app.checkbox, "Send this request and only the selected information to OpenAI.").check()
    checked(app)
    find(app.button, "Run assistant").click()
    checked(app)
    return app, store, original


@pytest.mark.parametrize("final_minutes", [150, 165])
def test_reviewed_assistant_effort_saves_only_total_and_clears_result(effort_review, final_minutes):
    app, store, original = effort_review
    assert store.tasks()[0] == original
    assert find(app.number_input, "Total study time (min)").value == 150
    assert any("Already completed: 30 min" in item.value for item in app.caption)
    assert any("Remaining after this change: 120 min" in item.value for item in app.caption)
    find(app.number_input, "Total study time (min)").set_value(final_minutes)
    checked(app)
    find(app.checkbox, "I have reviewed this total study time and its reasoning.").check()
    checked(app)
    find(app.button, "Save study time").click()
    checked(app)
    updated = store.tasks()[0]
    assert updated["estimated_minutes"] == final_minutes
    assert updated["remaining_minutes"] == final_minutes - 30
    assert updated["completed_minutes"] == original["completed_minutes"]
    assert updated["title"] == original["title"] and updated["due_at"] == original["due_at"]
    assert updated["source_quote"] == original["source_quote"]
    assert updated["updated_at"] != original["updated_at"]
    assert updated["effort_review"]["model"] == "test-model"
    assert updated["effort_review"]["decision"] == ("accepted" if final_minutes == 150 else "adjusted")
    with pytest.raises(KeyError):
        app.session_state["sf_general_result"]
    assert any("Study time saved" in item.value for item in app.success)
    assert not any(item.label == "Save study time" for item in app.button)
    checked(app)
    assert store.tasks()[0] == updated  # Rerunning cannot apply the old suggestion twice.


def test_effort_review_is_required_and_editing_minutes_resets_it(effort_review):
    app, store, original = effort_review
    assert find(app.button, "Save study time").disabled
    # AppTest follows the browser: an unreviewed update cannot be submitted.
    with pytest.raises(AppTestError, match="disabled"):
        find(app.button, "Save study time").click()
    checked(app)
    assert store.tasks()[0] == original
    find(app.checkbox, "I have reviewed this total study time and its reasoning.").check()
    checked(app)
    assert not find(app.button, "Save study time").disabled
    find(app.number_input, "Total study time (min)").set_value(180)
    checked(app)
    assert not find(app.checkbox, "I have reviewed this total study time and its reasoning.").value
    assert find(app.button, "Save study time").disabled
    find(app.number_input, "Total study time (min)").set_value(15)
    checked(app)
    assert find(app.button, "Save study time").disabled
    assert any("less than the time already completed" in item.value for item in app.warning)
    assert store.tasks()[0] == original


def test_stale_effort_cannot_overwrite_concurrent_progress(effort_review, monkeypatch):
    app, store, original = effort_review
    real_review = Store.review_task_effort

    def concurrent_review(self, task_id, final_minutes, suggestion, *, expected_updated_at):
        self.update_task(task_id, {"completed_minutes": 45}, expected_updated_at=expected_updated_at)
        return real_review(
            self, task_id, final_minutes, suggestion, expected_updated_at=expected_updated_at,
        )

    monkeypatch.setattr(Store, "review_task_effort", concurrent_review)
    find(app.checkbox, "I have reviewed this total study time and its reasoning.").check()
    checked(app)
    find(app.button, "Save study time").click()
    checked(app)
    updated = store.tasks()[0]
    assert updated["completed_minutes"] == 45
    assert updated["estimated_minutes"] == original["estimated_minutes"]
    assert updated.get("effort_review") is None
    assert any("task may have changed" in item.value for item in app.error)
    checked(app)
    assert any("selected information changed" in item.value for item in app.warning)
    assert not any(item.label == "Save study time" for item in app.button)


@pytest.mark.parametrize("case", ["unselected", "missing_version", "different_previous_task", "new"])
def test_effort_editor_requires_selected_existing_version(setup_ui, monkeypatch, case):
    store = Store(setup_ui / "data" / "studentflow.sqlite3")
    task_id = store.save_task({"title": "Saved task", "estimated_minutes": 90})
    original = store.tasks()[0]
    item = {
        "id": "proposal-one", "title": "Saved task", "relation": "update",
        "existing_task_id": task_id, "previous": original, "suggested_minutes": 150,
        "estimate_reason": "Research and drafting time.",
    }
    selected_ids = [task_id]
    if case == "unselected":
        selected_ids = []
    elif case == "missing_version":
        item["previous"] = original | {"updated_at": None}
    elif case == "different_previous_task":
        item["previous"] = original | {"id": "another-task"}
    else:
        item.pop("previous")
        item.pop("existing_task_id")
        item["relation"] = "new"
    monkeypatch.setenv("SF_EFFORT_ARTIFACTS", json.dumps({"task_proposals": [item]}))
    monkeypatch.setenv("SF_EFFORT_SELECTED", json.dumps(selected_ids))
    app = checked(AppTest.from_string("""
import json
import os
from pathlib import Path
from studentflow.assistant_ui import _render_artifacts
from studentflow.storage import Store
root = Path(os.environ['SF_ASSISTANT_TEST_ROOT'])
_render_artifacts(json.loads(os.environ['SF_EFFORT_ARTIFACTS']), {}, 'report-one',
                  store=Store(root / 'data' / 'studentflow.sqlite3'), model='test-model',
                  selected_task_ids=json.loads(os.environ['SF_EFFORT_SELECTED']))
"""))
    if case == "new":
        assert not any(button.label == "Save study time" for button in app.button)
        assert any("Inbox task review" in item.value for item in app.caption)
    else:
        assert find(app.button, "Save study time").disabled
        assert find(app.number_input, "Total study time (min)").disabled
        with pytest.raises(AppTestError, match="disabled"):
            find(app.button, "Save study time").click()
        checked(app)
    assert store.tasks()[0] == original
