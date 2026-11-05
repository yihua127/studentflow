"""Product interface journeys with isolated records and a fake Gmail service."""

import re
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import dotenv
import pytest
from streamlit.testing.v1 import AppTest

from studentflow import assistant_ui, batch_ui, gmail
from studentflow.storage import Store

APP = Path(__file__).resolve().parents[1] / "app.py"


def find(elements, label):
    return next(element for element in elements if element.label == label)


def checked(app):
    app.run(timeout=30)
    assert not app.exception
    return app


def message_ids(app):
    """Read the selectable inbox rows without confusing consent checkboxes."""
    prefix = "sf_mail_check_"
    return [box.key.removeprefix(prefix) for box in app.checkbox if box.key and box.key.startswith(prefix)]


def select_messages(app, selected):
    """Use the same per-message checkboxes as the email workspace."""
    assert set(selected) <= set(message_ids(app))
    for message_id in message_ids(app):
        checkbox = app.checkbox(key="sf_mail_check_" + message_id)
        if checkbox.value != (message_id in selected):
            checkbox.set_value(message_id in selected)
            checked(app)


def navigate(app, page):
    app.radio(key="sf_app_tab").set_value(page)
    return checked(app)


def choose_workflow(app, workflow):
    app.radio(key="sf_inbox_workflow").set_value(workflow)
    return checked(app)


def isolate(tmp_path, monkeypatch, *, configured=True):
    """Never read a real account configuration or reuse personal saved records."""
    client_path = tmp_path / "client.json"
    if configured:
        client_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("STUDENTFLOW_DATA_DIR", str(tmp_path / "records"))
    monkeypatch.setenv("STUDENTFLOW_GMAIL_CLIENT", str(client_path))
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("OPENAI_MODEL", "")
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *_args, **_kwargs: {})
    return client_path


def interface_text(app):
    values = []
    for element_type in (
        "title", "header", "subheader", "caption", "markdown", "text", "info",
        "success", "warning", "error",
    ):
        values.extend(str(item.value) for item in app.get(element_type))
    for element_type in (
        "button", "checkbox", "selectbox", "multiselect", "text_input", "text_area", "number_input",
        "date_input", "time_input", "expander", "tab", "radio", "download_button", "link_button",
    ):
        values.extend(str(item.label) for item in app.get(element_type))
    return "\n".join(values)


def test_empty_interface_is_english_and_gmail_first(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch, configured=False)

    def unexpected_connection(*_args, **_kwargs):
        raise AssertionError("Opening StudentFlow must not connect to an external service.")

    monkeypatch.setattr(gmail, "connect", unexpected_connection)
    monkeypatch.setattr(batch_ui, "connect", unexpected_connection)
    app = checked(AppTest.from_file(str(APP)))
    assert [re.sub(r"^:material/\w+: ", "", label) for label in app.radio(key="sf_app_tab").options] == [
        "Overview", "Inbox", "Tasks", "Study plan", "Discussions", "Assistant", "Attachments", "Settings",
    ]
    assert app.radio(key="sf_app_tab").value == "Inbox (Gmail)"
    assert not app.tabs
    assert app.radio(key="sf_inbox_workflow").options == ["Read email", "Tasks & events", "Email categories"]
    assert app.radio(key="sf_inbox_workflow").value == "Read email"
    assert not app.get("file_uploader")
    assert find(app.button, "Connect Gmail").disabled
    assert find(app.button, "Refresh inbox").disabled
    assert not any(box.label == "Messages per batch" for box in app.selectbox)
    assert all(button.disabled for button in app.button if button.label == "View selected messages")
    assert not any(button.label == "Analyze selected messages" for button in app.button)
    assert not any(box.key == "batch_selected" for box in app.multiselect)
    assert not any(box.label == "Choose a message" for box in app.selectbox)
    assert not any(button.label == "Open message" for button in app.button)
    assert not any(item.label == "Batch task extraction" for item in app.expander)
    assert not any(item.label == "Extract tasks with AI" for item in app.expander)
    text = interface_text(app)
    assert not re.search(r"[\u3400-\u9fff]", text)
    for development_reference in (
        "AX01", "AX02", "AX03", "StudentFlow_B", "Sprint 1", "LangChain", "Streamlit",
        "Scenario A", "Scenario B", "EML",
    ):
        assert development_reference not in text
    assert "Connect Gmail to view your inbox." in text


def test_mail_consent_review_task_save_and_disconnect(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    calls = []
    original_body = "Please submit the report by October 15 at 17:00.\n请保留这段邮件原文。\n"
    message = EmailMessage()
    message["From"] = "teacher@example.com"
    message["Subject"] = "Assignment"
    message["Date"] = "Fri, 9 Oct 2026 10:00:00 +0800"
    message.set_content(original_body)

    class FakeMailbox:
        def profile(self):
            return {"email_address": "student@example.com"}

        def list_message_page(self, limit=20, page_token=None):
            calls.append(("list", limit, page_token))
            return {"messages": [{
                "id": "demo123", "subject": "Assignment", "sender": "teacher@example.com",
                "sent_at": "Fri, 9 Oct 2026 10:00:00 +0800",
            }], "next_page_token": None}

        def fetch_eml(self, message_id):
            calls.append(("fetch", message_id))
            return message.as_bytes()

        def close(self):
            pass

    def fake_connect(credentials_path, token_path, interactive=False):
        calls.append(("connect", interactive))
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text("{}", encoding="utf-8")
        return FakeMailbox()

    monkeypatch.setattr(gmail, "connect", fake_connect)
    monkeypatch.setattr(batch_ui, "connect", fake_connect)
    app = checked(AppTest.from_file(str(APP)))
    assert calls == []
    assert find(app.button, "Connect Gmail").disabled
    find(app.checkbox, "Allow StudentFlow to read my Gmail messages.").check()
    checked(app)
    assert calls == []
    find(app.button, "Connect Gmail").click()
    checked(app)
    assert calls == [("connect", True)]
    find(app.button, "Refresh inbox").click()
    checked(app)
    assert ("list", 20, None) in calls
    assert not any(call[0] == "fetch" for call in calls)
    select_messages(app, ["demo123"])
    checked(app)
    assert not any(call[0] == "fetch" for call in calls)
    assert not any(box.key == "batch_consent" for box in app.checkbox)
    find(app.button, "View selected messages").click()
    checked(app)
    assert ("fetch", "demo123") in calls
    mail = next(iter(app.session_state["mails"].values()))
    assert mail["body"].strip() == original_body.strip()
    reader = next(element.proto.body for element in app.get("html") if 'class="sf-mail-body"' in element.proto.body)
    assert original_body.strip() in reader
    assert not any(box.key == "batch_consent" for box in app.checkbox)
    assert not any(item.label.startswith("Allow this message") for item in app.checkbox)
    assert not any(button.label == "Analyze selected messages" for button in app.button)
    assert len(app.session_state["mails"]) == 1

    # A real task must still be reviewed and supported by its source message.
    find(app.text_input, "Task name").set_value("Submit report")
    find(app.text_input, "Deadline (Singapore time)").set_value("2026-10-15 17:00")
    find(app.number_input, "Estimated study time (minutes)").set_value(60)
    find(app.text_area, "Supporting text").set_value("Please submit the report by October 15 at 17:00.")
    find(app.button, "Save task").click()
    checked(app)
    assert any("confirm that the task has been reviewed" in item.value for item in app.error)
    assert Store(tmp_path / "records" / "studentflow.sqlite3").tasks() == []
    find(app.checkbox, "I have reviewed this task and its supporting text.").check()
    find(app.button, "Save task").click()
    checked(app)
    assert any(item.value == "Task saved." for item in app.success)
    tasks = Store(tmp_path / "records" / "studentflow.sqlite3").tasks()
    assert len(tasks) == 1 and tasks[0]["title"] == "Submit report"
    navigate(app, "Tasks")
    task_table = next(table.value for table in app.dataframe if "Task" in table.value.columns)
    assert list(task_table.columns) == [
        "Task", "Deadline", "Study time (min)", "Status", "Source message",
        "Completed (min)", "Remaining (min)", "Progress",
    ]
    assert task_table.iloc[0]["Status"] == "Confirmed"
    assert task_table.iloc[0]["Source message"] == "Assignment"
    assert task_table.iloc[0]["Remaining (min)"] == 60
    assert task_table.iloc[0]["Progress"] == "Unfinished"

    navigate(app, "Inbox (Gmail)")
    assert app.session_state["batch_selected"] == ["demo123"]
    fetches_before = [call for call in calls if call[0] == "fetch"]
    find(app.button, "View selected messages").click()
    checked(app)
    assert len(app.session_state["mails"]) == 1
    assert [call for call in calls if call[0] == "fetch"] == fetches_before
    find(app.button, "Disconnect Gmail").click()
    checked(app)
    assert app.session_state["mails"] == {}
    assert not (tmp_path / "records" / "auth" / "gmail_token.json").exists()
    assert len(Store(tmp_path / "records" / "studentflow.sqlite3").tasks()) == 1


def message_headers(first, last, *, prefix="message"):
    return [{
        "id": f"{prefix}{number:03}",
        "subject": f"Message {number}",
        "sender": "teacher@example.com",
        "sent_at": "Fri, 9 Oct 2026 10:00:00 +0800",
    } for number in range(first, last + 1)]


def page(headers, next_token=None):
    return {"messages": headers, "next_page_token": next_token}


def connected_inbox(tmp_path, monkeypatch, pages):
    """Start a consented fake connection; listing and message opening remain explicit."""
    isolate(tmp_path, monkeypatch)
    state = {"account": "student@example.com", "pages": pages, "calls": []}

    class FakeMailbox:
        def profile(self):
            return {"email_address": state["account"]}

        def list_message_page(self, limit=20, page_token=None):
            state["calls"].append(("list", limit, page_token))
            result = state["pages"][page_token]
            if isinstance(result, Exception):
                raise result
            return result

        def fetch_eml(self, message_id):
            state["calls"].append(("fetch", message_id))
            message = EmailMessage()
            message["From"] = "teacher@example.com"
            message["Subject"] = message_id
            message["Date"] = "Fri, 9 Oct 2026 10:00:00 +0800"
            message.set_content("Please submit the report by October 15 at 17:00.")
            return message.as_bytes()

        def close(self):
            pass

    def fake_connect(credentials_path, token_path, interactive=False):
        state["calls"].append(("connect", interactive))
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text("{}", encoding="utf-8")
        return FakeMailbox()

    monkeypatch.setattr(gmail, "connect", fake_connect)
    monkeypatch.setattr(batch_ui, "connect", fake_connect)
    app = checked(AppTest.from_file(str(APP)))
    assert state["calls"] == []
    assert not any(box.label == "Messages per batch" for box in app.selectbox)
    find(app.checkbox, "Allow StudentFlow to read my Gmail messages.").check()
    checked(app)
    assert state["calls"] == []
    find(app.button, "Connect Gmail").click()
    checked(app)
    assert state["calls"] == [("connect", True)]
    return app, state


def test_sidebar_navigation_only_renders_the_chosen_workspace(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch, configured=False)

    def unexpected_connection(*_args, **_kwargs):
        raise AssertionError("Changing workspaces must not contact Gmail.")

    monkeypatch.setattr(gmail, "connect", unexpected_connection)
    monkeypatch.setattr(batch_ui, "connect", unexpected_connection)
    app = checked(AppTest.from_file(str(APP)))
    assert app.sidebar.radio(key="sf_app_tab").value == "Inbox (Gmail)"
    for destination in ("Overview", "Tasks", "Study Plan", "Discussions", "Assistant", "Attachments", "Settings"):
        navigate(app, destination)
        assert app.sidebar.radio(key="sf_app_tab").value == destination
        assert [tab.label for tab in app.tabs] == (
            ["Schedule", "Availability & preferences", "Commitments & calendar"]
            if destination == "Study Plan" else []
        )
        assert not any(button.label == "Refresh inbox" for button in app.button)
        assert not any(radio.key == "sf_inbox_workflow" for radio in app.radio)
    navigate(app, "Inbox (Gmail)")
    assert find(app.button, "Refresh inbox").disabled
    assert app.radio(key="sf_inbox_workflow").value == "Read email"


def test_subject_click_opens_reader_without_changing_multi_selection_or_refetching(tmp_path, monkeypatch):
    app, state = connected_inbox(tmp_path, monkeypatch, {None: page(message_headers(1, 3))})
    find(app.button, "Refresh inbox").click()
    checked(app)
    select_messages(app, ["message001", "message003"])
    assert not any(call[0] == "fetch" for call in state["calls"])

    app.button(key="sf_mail_open_message002").click()
    checked(app)
    assert [call for call in state["calls"] if call[0] == "fetch"] == [("fetch", "message002")]
    assert app.session_state["batch_selected"] == ["message001", "message003"]
    opened_id = find(app.selectbox, "Opened messages").value
    assert app.session_state["mails"][opened_id]["gmail_id"] == "message002"
    assert any('class="sf-mail-body"' in element.proto.body for element in app.get("html"))
    assert find(app.expander, "Add a task manually")
    assert not any(button.label == "Analyze selected messages" for button in app.button)

    app.button(key="sf_mail_open_message002").click()
    checked(app)
    assert [call for call in state["calls"] if call[0] == "fetch"] == [("fetch", "message002")]
    navigate(app, "Tasks")
    navigate(app, "Inbox (Gmail)")
    assert app.session_state["batch_selected"] == ["message001", "message003"]
    assert app.checkbox(key="sf_mail_check_message001").value
    assert app.checkbox(key="sf_mail_check_message003").value
    assert find(app.selectbox, "Opened messages").value == opened_id


def test_search_preserves_hidden_selection_and_restores_checkbox_state(tmp_path, monkeypatch):
    app, state = connected_inbox(tmp_path, monkeypatch, {None: page(message_headers(1, 3))})
    find(app.button, "Refresh inbox").click()
    checked(app)
    select_messages(app, ["message001", "message003"])
    app.text_input(key="sf_mail_search").set_value("Message 2")
    checked(app)
    assert message_ids(app) == ["message002"]
    assert app.session_state["batch_selected"] == ["message001", "message003"]
    assert "2 selected outside this view" in interface_text(app)
    app.text_input(key="sf_mail_search").set_value("")
    checked(app)
    assert app.checkbox(key="sf_mail_check_message001").value
    assert app.checkbox(key="sf_mail_check_message003").value
    assert not app.checkbox(key="sf_mail_check_message002").value
    assert not any(call[0] == "fetch" for call in state["calls"])


@pytest.mark.parametrize("checked_message", ["message002", "message001"])
def test_reader_reply_handoff_uses_displayed_email_and_navigates_to_assistant(tmp_path, monkeypatch, checked_message):
    from studentflow.assistant_context import source_key
    from studentflow.reply_preferences import DEFAULT_REPLY_REQUEST

    app, state = connected_inbox(tmp_path, monkeypatch, {None: page(message_headers(1, 3))})
    find(app.button, "Refresh inbox").click()
    checked(app)
    select_messages(app, [checked_message])
    if checked_message != "message002":
        # Reading B must bind its reply action to B even while A remains checked.
        app.button(key="sf_mail_open_message002").click()
        checked(app)
        displayed_id = find(app.selectbox, "Opened messages").value
        assert app.session_state["mails"][displayed_id]["gmail_id"] == "message002"
        assert app.session_state["batch_selected"] == [checked_message]
    app.button(key="sf_mail_reply").click()
    checked(app)
    assert app.radio(key="sf_app_tab").value == "Assistant"
    assert app.session_state["sf_assistant_workspace"] == "Email replies"
    mail = next(iter(app.session_state["mails"].values()))
    assert mail["gmail_id"] == "message002"
    assert app.multiselect(key="sf_assistant_sources").value == [
        source_key("gmail", "student@example.com", mail["id"]),
    ]
    assert app.text_area(key="sf_assistant_request").value == DEFAULT_REPLY_REQUEST
    assert not find(app.checkbox, "Send this request and only the selected information to OpenAI.").value
    assert [call for call in state["calls"] if call[0] == "fetch"] == [("fetch", "message002")]
    navigate(app, "Inbox (Gmail)")
    assert app.session_state["batch_selected"] == [checked_message]
    assert app.checkbox(key="sf_mail_check_" + checked_message).value


def test_assistant_request_and_edited_reply_survive_sidebar_navigation(tmp_path, monkeypatch):
    app, state = connected_inbox(tmp_path, monkeypatch, {None: page(message_headers(1, 2))})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-local-testing-only")
    requests = []

    def fake_run(request, toolbox, **_kwargs):
        requests.append(request)
        source = next(iter(toolbox._sources.values()))
        recorded = toolbox.invoke("record_reply_draft", {
            "source_id": source["source_id"], "subject": "Re: Assignment",
            "body": "Hello,\nThank you for the report instructions.\nBest regards,\nStudent",
            "recipient": "teacher@example.com",
            "citations": [{"source_id": source["source_id"],
                           "quote": "Please submit the report by October 15 at 17:00."}],
            "style_source_ids": [],
        }, role="email")
        assert recorded["ok"]
        return SimpleNamespace(answer="Review the draft below.", status="completed",
                               events=toolbox.events, usage={"model_calls": 1}, artifacts=toolbox.artifacts)

    monkeypatch.setattr(assistant_ui, "run_assistant", fake_run)
    find(app.button, "Refresh inbox").click()
    checked(app)
    select_messages(app, ["message002"])
    app.button(key="sf_mail_reply").click()
    checked(app)
    request = "Draft a brief acknowledgment of the instructions."
    app.text_area(key="sf_assistant_request").set_value(request)
    checked(app)
    find(app.checkbox, "Send this request and only the selected information to OpenAI.").check()
    checked(app)
    app.button(key="sf_assistant_run").click()
    checked(app)
    result_id = app.session_state["sf_assistant_result"]["id"]
    subject = "Reviewed report acknowledgment"
    body = "Hello,\nI have received the instructions.\nBest regards,\nReviewed Student"
    find(app.text_input, "Draft subject").set_value(subject)
    find(app.text_area, "Draft message").set_value(body)
    checked(app)

    navigate(app, "Inbox (Gmail)")
    assert not any(area.label == "Draft message" for area in app.text_area)
    checked(app)
    navigate(app, "Assistant")
    assert app.text_area(key="sf_assistant_request").value == request
    assert app.session_state["sf_assistant_result"]["id"] == result_id
    assert find(app.text_input, "Draft subject").value == subject
    assert find(app.text_area, "Draft message").value == body
    assert requests == [request]
    assert [call for call in state["calls"] if call[0] == "fetch"] == [("fetch", "message002")]


def test_assistant_workspace_and_general_request_survive_sidebar_navigation(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch, configured=False)
    app = checked(AppTest.from_file(str(APP)))
    navigate(app, "Assistant")
    app.radio(key="sf_assistant_workspace").set_value("Other assistance")
    checked(app)
    request = "Help me organize the questions for my study group."
    app.text_area(key="sf_general_request").set_value(request)
    checked(app)
    navigate(app, "Inbox (Gmail)")
    checked(app)
    navigate(app, "Assistant")
    assert app.radio(key="sf_assistant_workspace").value == "Other assistance"
    assert app.text_area(key="sf_general_request").value == request


def test_load_more_exceeds_twenty_deduplicates_and_preserves_selection(tmp_path, monkeypatch):
    app, state = connected_inbox(tmp_path, monkeypatch, {
        None: page(message_headers(1, 20), "second-page"),
        "second-page": page(message_headers(20, 39), "final-page"),
        "final-page": page(message_headers(40, 41)),
    })
    find(app.button, "Refresh inbox").click()
    checked(app)
    assert len(message_ids(app)) == 20
    assert ("list", 20, None) in state["calls"]
    select_messages(app, ["message007"])
    checked(app)
    calls_before = list(state["calls"])
    checked(app)
    assert state["calls"] == calls_before

    find(app.button, "Load more").click()
    checked(app)
    assert len(message_ids(app)) == 39
    assert app.session_state["batch_selected"] == ["message007"]
    assert ("list", 20, "second-page") in state["calls"]
    find(app.button, "Load more").click()
    checked(app)
    assert len(message_ids(app)) == 41
    assert app.session_state["batch_selected"] == ["message007"]
    assert ("list", 20, "final-page") in state["calls"]
    assert not any(button.label == "Load more" for button in app.button)
    assert not any(call[0] == "fetch" for call in state["calls"])

    # A message outside the first batch can be opened using its stable Gmail ID.
    select_messages(app, ["message041"])
    checked(app)
    assert not any(call[0] == "fetch" for call in state["calls"])
    find(app.button, "View selected messages").click()
    checked(app)
    assert ("fetch", "message041") in state["calls"]


def test_failed_load_more_can_retry_without_losing_messages_or_cursor(tmp_path, monkeypatch):
    app, state = connected_inbox(tmp_path, monkeypatch, {
        None: page(message_headers(1, 20), "retry-page"),
        "retry-page": gmail.GmailError("The inbox could not be loaded. Try again."),
    })
    find(app.button, "Refresh inbox").click()
    checked(app)
    select_messages(app, ["message009"])
    checked(app)
    options_before = message_ids(app)
    find(app.button, "Load more").click()
    checked(app)
    assert any("Try again" in error.value for error in app.error)
    assert message_ids(app) == options_before
    assert app.session_state["batch_selected"] == ["message009"]
    assert not find(app.button, "Load more").disabled

    state["pages"]["retry-page"] = page(message_headers(21, 25))
    calls_before = list(state["calls"])
    checked(app)
    assert state["calls"] == calls_before
    find(app.button, "Load more").click()
    checked(app)
    assert state["calls"][-1] == ("list", 20, "retry-page")
    assert len(message_ids(app)) == 25
    assert app.session_state["batch_selected"] == ["message009"]
    assert not any(button.label == "Load more" for button in app.button)
    assert not any(call[0] == "fetch" for call in state["calls"])


@pytest.mark.parametrize("selected", [["message001"], ["message001", "message003"]])
def test_unified_selection_extracts_one_or_many_and_saves_only_after_review(tmp_path, monkeypatch, selected):
    app, state = connected_inbox(tmp_path, monkeypatch, {
        None: page(message_headers(1, 3)),
    })
    monkeypatch.setenv("OPENAI_API_KEY", "sk-local-testing-only")
    extraction_calls = []

    def fake_extract(body, subject, sent_at, api_key, model):
        extraction_calls.append(subject)
        return {"tasks": [{
            "title": f"Submit report from {subject}",
            "due_at": "2026-10-15T17:00:00+08:00", "estimated_minutes": 60,
            "source_quote": body.strip(), "confidence": 0.9, "needs_clarification": [],
        }], "warnings": []}

    monkeypatch.setattr(batch_ui, "extract_tasks", fake_extract)
    def coordinate(tasks, events, *_args, **_kwargs):
        return {"status": "completed", "task_result": tasks(), "event_result": events(),
                "events": [], "usage": {}, "warnings": []}

    monkeypatch.setattr(batch_ui, "run_inbox_agents", coordinate)
    monkeypatch.setattr(batch_ui, "analyze_fixed_event_messages", lambda *_args, **_kwargs: {
        "events": [], "results": [], "warnings": [], "complete": True,
    })
    find(app.button, "Refresh inbox").click()
    checked(app)
    select_messages(app, selected)
    checked(app)
    assert extraction_calls == []
    assert not any(call[0] == "fetch" for call in state["calls"])
    assert not any(button.label == "Analyze selected messages" for button in app.button)

    if len(selected) > 1:
        # The same selection can be viewed without model consent or a paid call.
        find(app.button, "View selected messages").click()
        checked(app)
        assert extraction_calls == []
        assert len(app.session_state["mails"]) == len(selected)
        assert len(find(app.selectbox, "Opened messages").options) == len(selected)
        assert not any(box.key == "batch_consent" for box in app.checkbox)

    choose_workflow(app, "Tasks & events")
    assert app.session_state["batch_selected"] == selected
    assert app.checkbox(key="batch_consent").value is False
    assert find(app.button, "Analyze selected messages").disabled
    app.checkbox(key="batch_consent").check()
    checked(app)
    find(app.button, "Analyze selected messages").click()
    checked(app)
    assert sorted(extraction_calls) == sorted(selected)
    assert [call[1] for call in state["calls"] if call[0] == "fetch"] == selected
    assert len(app.session_state["batch_results"]) == len(selected)
    assert len([button for button in app.button if button.label == "Analyze selected messages"]) == 1
    assert not any(item.label == "Extract tasks with AI" for item in app.expander)
    store = Store(tmp_path / "records" / "studentflow.sqlite3")
    assert store.tasks() == []

    # Extraction creates proposals; the saved task list requires explicit review.
    app.session_state["batch_editor_1"] = {
        "edited_rows": {index: {"Include": True} for index in range(len(selected))},
        "added_rows": [], "deleted_rows": [],
    }
    find(app.button, "Save selected tasks").click()
    checked(app)
    assert any("reviewed" in error.value for error in app.error)
    assert store.tasks() == []
    # AppTest has no editable dataframe widget; submit its browser-style edits.
    app.session_state["batch_editor_1"] = {
        "edited_rows": {index: {"Include": True} for index in range(len(selected))},
        "added_rows": [], "deleted_rows": [],
    }
    app.checkbox(key="batch_reviewed_1").check()
    find(app.button, "Save selected tasks").click()
    checked(app)
    tasks = store.tasks()
    assert len(tasks) == len(selected), [error.value for error in app.error]
    assert {task["mail_subject"] for task in tasks} == set(selected)
    assert all(task["status"] == "confirmed" for task in tasks)
    assert sorted(extraction_calls) == sorted(selected)


def test_refresh_and_reconnect_start_a_new_pagination_sequence(tmp_path, monkeypatch):
    app, state = connected_inbox(tmp_path, monkeypatch, {
        None: page(message_headers(1, 20), "old-second"),
        "old-second": page(message_headers(21, 40), "old-third"),
    })
    find(app.button, "Refresh inbox").click()
    checked(app)
    find(app.button, "Load more").click()
    checked(app)
    select_messages(app, ["message030"])
    checked(app)
    state["pages"][None] = page(message_headers(1, 20, prefix="fresh"), "fresh-second")
    state["pages"]["fresh-second"] = page(message_headers(21, 21, prefix="fresh"))
    find(app.button, "Refresh inbox").click()
    checked(app)
    assert state["calls"][-1] == ("list", 20, None)
    assert len(message_ids(app)) == 20
    assert app.session_state["batch_selected"] == []
    find(app.button, "Load more").click()
    checked(app)
    assert state["calls"][-1] == ("list", 20, "fresh-second")
    assert len(message_ids(app)) == 21

    # Switching accounts starts from a new list and pagination cursor.
    find(app.button, "Switch account").click()
    checked(app)
    assert not message_ids(app)
    assert not any(button.label == "Load more" for button in app.button)
    find(app.button, "Refresh inbox").click()
    checked(app)
    assert state["calls"][-1] == ("list", 20, None)
    assert find(app.button, "Load more")
    find(app.button, "Disconnect Gmail").click()
    checked(app)
    assert not message_ids(app)
    assert not any(button.label == "Load more" for button in app.button)
    assert app.session_state["mails"] == {}
    assert not (tmp_path / "records" / "auth" / "gmail_token.json").exists()
    assert find(app.button, "Connect Gmail").disabled
    find(app.checkbox, "Allow StudentFlow to read my Gmail messages.").check()
    checked(app)
    find(app.button, "Connect Gmail").click()
    checked(app)
    assert not message_ids(app)
    find(app.button, "Refresh inbox").click()
    checked(app)
    assert state["calls"][-1] == ("list", 20, None)


@pytest.mark.parametrize("action", ["Load more", "View selected messages"])
def test_account_change_clears_old_messages_before_listing_or_opening(tmp_path, monkeypatch, action):
    app, state = connected_inbox(tmp_path, monkeypatch, {
        None: page(message_headers(1, 20), "old-account-page"),
    })
    find(app.button, "Refresh inbox").click()
    checked(app)
    select_messages(app, ["message001"])
    checked(app)
    find(app.button, "View selected messages").click()
    checked(app)
    assert len(app.session_state["mails"]) == 1
    state["account"] = "different@example.com"
    state["pages"][None] = page(message_headers(1, 2, prefix="different"))
    calls_before = [call for call in state["calls"] if call[0] in ("list", "fetch")]
    find(app.button, action).click()
    checked(app)
    assert "account" in interface_text(app).lower()
    assert [call for call in state["calls"] if call[0] in ("list", "fetch")] == calls_before
    assert not message_ids(app)
    assert not any(button.label == "Load more" for button in app.button)
    assert app.session_state["mails"] == {}
    assert not any('class="sf-mail-body"' in element.proto.body for element in app.get("html"))
    if action == "View selected messages":
        assert find(app.button, "Refresh inbox").disabled
        find(app.button, "Reconnect Gmail").click()
        checked(app)
    find(app.button, "Refresh inbox").click()
    checked(app)
    assert state["calls"][-1] == ("list", 20, None)
    assert len(message_ids(app)) == 2
    assert "batch_selected" not in app.session_state or app.session_state["batch_selected"] == []
    assert all(not app.checkbox(key="sf_mail_check_" + message_id).value for message_id in message_ids(app))
    assert all(header["id"].startswith("different") for header in app.session_state["gmail_headers"])


def saved_connection(tmp_path, monkeypatch, *, saved=True, error=None):
    """A durable fake sign-in shared by otherwise independent browser sessions."""
    isolate(tmp_path, monkeypatch)
    token_path = tmp_path / "records" / "auth" / "gmail_token.json"
    if saved:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text("{}", encoding="utf-8")
    state = {"calls": [], "error": error, "list_error": None, "fetch_error": None}

    class FakeMailbox:
        def profile(self):
            state["calls"].append(("profile",))
            return {"email_address": "student@example.com"}

        def list_message_page(self, limit=20, page_token=None):
            state["calls"].append(("list", limit, page_token))
            if state["list_error"]:
                raise state["list_error"]
            return page(message_headers(1, 2), "second-page")

        def fetch_eml(self, message_id):
            state["calls"].append(("fetch", message_id))
            if state["fetch_error"]:
                raise state["fetch_error"]
            message = EmailMessage()
            message["From"] = "teacher@example.com"
            message["Subject"] = "Assignment"
            message.set_content("Please submit the report by October 15 at 17:00.")
            return message.as_bytes()

        def close(self):
            state["calls"].append(("close",))

    def fake_connect(credentials_path, saved_token_path, interactive=False):
        state["calls"].append(("connect", interactive))
        if state["error"]:
            raise state["error"]
        if interactive:
            saved_token_path.parent.mkdir(parents=True, exist_ok=True)
            saved_token_path.write_text("{}", encoding="utf-8")
        else:
            assert saved_token_path.is_file()
        return FakeMailbox()

    monkeypatch.setattr(gmail, "connect", fake_connect)
    monkeypatch.setattr(batch_ui, "connect", fake_connect)
    return token_path, state


def test_saved_login_restores_once_per_new_browser_without_reading_messages(tmp_path, monkeypatch):
    token_path, state = saved_connection(tmp_path, monkeypatch, saved=False)
    original = checked(AppTest.from_file(str(APP)))
    assert state["calls"] == []
    find(original.checkbox, "Allow StudentFlow to read my Gmail messages.").check()
    checked(original)
    find(original.button, "Connect Gmail").click()
    checked(original)
    assert state["calls"] == [("connect", True), ("profile",), ("close",)]
    assert token_path.is_file()

    # Closing a browser loses its session state but keeps the on-device sign-in.
    state["calls"].clear()
    reopened = checked(AppTest.from_file(str(APP)))
    assert state["calls"] == [("connect", False), ("profile",), ("close",)]
    assert reopened.session_state["gmail_profile"]["email_address"] == "student@example.com"
    assert not find(reopened.button, "Refresh inbox").disabled
    assert find(reopened.button, "Switch account")
    assert "Gmail connected: student@example.com" in interface_text(reopened)

    # Rerunning the page must not repeat connection/profile requests.
    calls_before = list(state["calls"])
    checked(reopened)
    assert not any(box.label == "Messages per batch" for box in reopened.selectbox)
    assert state["calls"] == calls_before
    assert not find(reopened.button, "Refresh inbox").disabled
    find(reopened.button, "Refresh inbox").click()
    checked(reopened)
    assert ("list", 20, None) in state["calls"]
    assert not any(call[0] == "fetch" for call in state["calls"])


def test_explicit_disconnect_survives_new_browser_and_clears_messages(tmp_path, monkeypatch):
    token_path, state = saved_connection(tmp_path, monkeypatch)
    app = checked(AppTest.from_file(str(APP)))
    find(app.button, "Refresh inbox").click()
    checked(app)
    select_messages(app, ["message001"])
    checked(app)
    find(app.button, "View selected messages").click()
    checked(app)
    assert len(app.session_state["mails"]) == 1
    find(app.button, "Disconnect Gmail").click()
    checked(app)
    assert not token_path.exists()
    assert app.session_state["mails"] == {}
    assert "gmail_profile" not in app.session_state or not app.session_state["gmail_profile"]
    assert find(app.checkbox, "Allow StudentFlow to read my Gmail messages.").value is False
    assert find(app.button, "Refresh inbox").disabled
    assert not any(button.label == "Load more" for button in app.button)

    state["calls"].clear()
    reopened = checked(AppTest.from_file(str(APP)))
    assert state["calls"] == []
    assert find(reopened.button, "Connect Gmail").disabled
    assert find(reopened.button, "Refresh inbox").disabled


def test_temporary_restore_error_preserves_login_and_retries_without_oauth(tmp_path, monkeypatch):
    token_path, state = saved_connection(
        tmp_path, monkeypatch, error=gmail.GmailError("Gmail is temporarily unavailable. Try again."),
    )
    app = checked(AppTest.from_file(str(APP)))
    assert state["calls"] == [("connect", False)]
    assert token_path.exists()
    assert find(app.button, "Refresh inbox").disabled
    assert find(app.button, "Retry connection")
    assert "temporarily unavailable" in interface_text(app)
    checked(app)
    assert state["calls"] == [("connect", False)]

    state["error"] = None
    find(app.button, "Retry connection").click()
    checked(app)
    assert state["calls"] == [("connect", False), ("connect", False), ("profile",), ("close",)]
    assert not find(app.button, "Refresh inbox").disabled
    assert token_path.exists()


def test_revoked_saved_login_requests_explicit_reconnect(tmp_path, monkeypatch):
    token_path, state = saved_connection(
        tmp_path, monkeypatch, error=gmail.GmailAuthError("Your Gmail sign-in has expired. Reconnect Gmail to continue."),
    )
    app = checked(AppTest.from_file(str(APP)))
    assert state["calls"] == [("connect", False)]
    assert token_path.exists()
    assert find(app.button, "Reconnect Gmail")
    assert find(app.button, "Refresh inbox").disabled
    assert "Reconnect Gmail" in interface_text(app)
    checked(app)
    assert state["calls"] == [("connect", False)]

    state["error"] = None
    find(app.button, "Reconnect Gmail").click()
    checked(app)
    assert state["calls"] == [("connect", False), ("connect", True), ("profile",), ("close",)]
    assert not find(app.button, "Refresh inbox").disabled
    assert not any(call[0] in ("list", "fetch") for call in state["calls"])


@pytest.mark.parametrize("action", ["Refresh inbox", "Load more", "View selected messages"])
def test_revoked_login_during_message_action_prompts_reconnect(tmp_path, monkeypatch, action):
    token_path, state = saved_connection(tmp_path, monkeypatch)
    app = checked(AppTest.from_file(str(APP)))
    find(app.button, "Refresh inbox").click()
    checked(app)
    select_messages(app, ["message001"])
    checked(app)
    error = gmail.GmailAuthError("Your Gmail sign-in has expired. Reconnect Gmail to continue.")
    state["fetch_error" if action == "View selected messages" else "list_error"] = error
    find(app.button, action).click()
    checked(app)
    assert token_path.exists()
    assert find(app.button, "Reconnect Gmail")
    assert find(app.button, "Refresh inbox").disabled
    assert "Reconnect Gmail" in interface_text(app)
    assert not any(call == ("connect", True) for call in state["calls"])
