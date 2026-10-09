"""Product interface journeys with isolated records and a fake Gmail service."""

from email.message import EmailMessage
from pathlib import Path
import re

import dotenv
import pytest
from streamlit.testing.v1 import AppTest

from studentflow import gmail
from studentflow.storage import Store


APP = Path(__file__).resolve().parents[1] / "app.py"


def find(elements, label):
    return next(element for element in elements if element.label == label)


def checked(app):
    app.run(timeout=30)
    assert not app.exception
    return app


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
        "button", "checkbox", "selectbox", "text_input", "text_area", "number_input",
        "date_input", "time_input", "expander", "tab", "download_button", "link_button",
    ):
        values.extend(str(item.label) for item in app.get(element_type))
    return "\n".join(values)


def test_empty_interface_is_english_and_gmail_first(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch, configured=False)

    def unexpected_connection(*_args, **_kwargs):
        raise AssertionError("Opening StudentFlow must not connect to an external service.")

    monkeypatch.setattr(gmail, "connect", unexpected_connection)
    app = checked(AppTest.from_file(str(APP)))
    assert [tab.label for tab in app.tabs] == ["Inbox (Gmail)", "Tasks", "Study Plan", "Attachments"]
    assert not app.get("file_uploader")
    assert find(app.button, "Connect Gmail").disabled
    assert find(app.button, "Refresh inbox").disabled
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
    find(app.button, "Open message").click()
    checked(app)
    assert ("fetch", "demo123") in calls
    assert find(app.text_area, "Message").value.strip() == original_body.strip()
    model_consent = next(item for item in app.checkbox if item.label.startswith("Allow this message"))
    assert model_consent.value is False
    assert find(app.button, "Extract tasks").disabled
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
    task_table = next(table.value for table in app.dataframe if "Task" in table.value.columns)
    assert list(task_table.columns) == ["Task", "Deadline", "Study time (min)", "Status", "Source message"]
    assert task_table.iloc[0]["Status"] == "Confirmed"
    assert task_table.iloc[0]["Source message"] == "Assignment"

    find(app.button, "Open message").click()
    checked(app)
    assert len(app.session_state["mails"]) == 1
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
    app = checked(AppTest.from_file(str(APP)))
    assert state["calls"] == []
    assert find(app.selectbox, "Messages per batch").value == 20
    find(app.checkbox, "Allow StudentFlow to read my Gmail messages.").check()
    checked(app)
    assert state["calls"] == []
    find(app.button, "Connect Gmail").click()
    checked(app)
    assert state["calls"] == [("connect", True)]
    return app, state


def test_load_more_exceeds_twenty_deduplicates_and_preserves_selection(tmp_path, monkeypatch):
    app, state = connected_inbox(tmp_path, monkeypatch, {
        None: page(message_headers(1, 20), "second-page"),
        "second-page": page(message_headers(20, 39), "final-page"),
        "final-page": page(message_headers(40, 41)),
    })
    find(app.button, "Refresh inbox").click()
    checked(app)
    assert len(find(app.selectbox, "Choose a message").options) == 20
    assert ("list", 20, None) in state["calls"]
    find(app.selectbox, "Choose a message").select("message007")
    checked(app)
    calls_before = list(state["calls"])
    checked(app)
    assert state["calls"] == calls_before

    find(app.button, "Load more").click()
    checked(app)
    selector = find(app.selectbox, "Choose a message")
    assert len(selector.options) == 39
    assert selector.value == "message007"
    assert ("list", 20, "second-page") in state["calls"]
    find(app.button, "Load more").click()
    checked(app)
    assert len(find(app.selectbox, "Choose a message").options) == 41
    assert find(app.selectbox, "Choose a message").value == "message007"
    assert ("list", 20, "final-page") in state["calls"]
    assert not any(button.label == "Load more" for button in app.button)
    assert not any(call[0] == "fetch" for call in state["calls"])

    # A message outside the first batch can be opened using its stable Gmail ID.
    find(app.selectbox, "Choose a message").select("message041")
    checked(app)
    assert not any(call[0] == "fetch" for call in state["calls"])
    find(app.button, "Open message").click()
    checked(app)
    assert ("fetch", "message041") in state["calls"]


def test_failed_load_more_can_retry_without_losing_messages_or_cursor(tmp_path, monkeypatch):
    app, state = connected_inbox(tmp_path, monkeypatch, {
        None: page(message_headers(1, 20), "retry-page"),
        "retry-page": gmail.GmailError("The inbox could not be loaded. Try again."),
    })
    find(app.button, "Refresh inbox").click()
    checked(app)
    find(app.selectbox, "Choose a message").select("message009")
    checked(app)
    options_before = find(app.selectbox, "Choose a message").options
    find(app.button, "Load more").click()
    checked(app)
    assert any("Try again" in error.value for error in app.error)
    assert find(app.selectbox, "Choose a message").options == options_before
    assert find(app.selectbox, "Choose a message").value == "message009"
    assert not find(app.button, "Load more").disabled

    state["pages"]["retry-page"] = page(message_headers(21, 25))
    calls_before = list(state["calls"])
    find(app.selectbox, "Messages per batch").select(5)
    checked(app)
    assert state["calls"] == calls_before
    find(app.button, "Load more").click()
    checked(app)
    assert state["calls"][-1] == ("list", 5, "retry-page")
    assert len(find(app.selectbox, "Choose a message").options) == 25
    assert find(app.selectbox, "Choose a message").value == "message009"
    assert not any(button.label == "Load more" for button in app.button)
    assert not any(call[0] == "fetch" for call in state["calls"])


def test_refresh_and_reconnect_start_a_new_pagination_sequence(tmp_path, monkeypatch):
    app, state = connected_inbox(tmp_path, monkeypatch, {
        None: page(message_headers(1, 20), "old-second"),
        "old-second": page(message_headers(21, 40), "old-third"),
    })
    find(app.button, "Refresh inbox").click()
    checked(app)
    find(app.button, "Load more").click()
    checked(app)
    find(app.selectbox, "Choose a message").select("message030")
    checked(app)
    state["pages"][None] = page(message_headers(1, 20, prefix="fresh"), "fresh-second")
    state["pages"]["fresh-second"] = page(message_headers(21, 21, prefix="fresh"))
    find(app.button, "Refresh inbox").click()
    checked(app)
    assert state["calls"][-1] == ("list", 20, None)
    assert len(find(app.selectbox, "Choose a message").options) == 20
    assert find(app.selectbox, "Choose a message").value.startswith("fresh")
    find(app.button, "Load more").click()
    checked(app)
    assert state["calls"][-1] == ("list", 20, "fresh-second")
    assert len(find(app.selectbox, "Choose a message").options) == 21

    # Switching accounts starts from a new list and pagination cursor.
    find(app.button, "Switch account").click()
    checked(app)
    assert not any(box.label == "Choose a message" for box in app.selectbox)
    assert not any(button.label == "Load more" for button in app.button)
    find(app.button, "Refresh inbox").click()
    checked(app)
    assert state["calls"][-1] == ("list", 20, None)
    assert find(app.button, "Load more")
    find(app.button, "Disconnect Gmail").click()
    checked(app)
    assert not any(box.label == "Choose a message" for box in app.selectbox)
    assert not any(button.label == "Load more" for button in app.button)
    assert app.session_state["mails"] == {}
    assert not (tmp_path / "records" / "auth" / "gmail_token.json").exists()
    assert find(app.button, "Connect Gmail").disabled
    find(app.checkbox, "Allow StudentFlow to read my Gmail messages.").check()
    checked(app)
    find(app.button, "Connect Gmail").click()
    checked(app)
    assert not any(box.label == "Choose a message" for box in app.selectbox)
    find(app.button, "Refresh inbox").click()
    checked(app)
    assert state["calls"][-1] == ("list", 20, None)


@pytest.mark.parametrize("action", ["Load more", "Open message"])
def test_account_change_clears_old_messages_before_listing_or_opening(tmp_path, monkeypatch, action):
    app, state = connected_inbox(tmp_path, monkeypatch, {
        None: page(message_headers(1, 20), "old-account-page"),
    })
    find(app.button, "Refresh inbox").click()
    checked(app)
    find(app.button, "Open message").click()
    checked(app)
    assert len(app.session_state["mails"]) == 1
    state["account"] = "different@example.com"
    state["pages"][None] = page(message_headers(1, 2, prefix="different"))
    calls_before = [call for call in state["calls"] if call[0] in ("list", "fetch")]
    find(app.button, action).click()
    checked(app)
    assert any("account" in error.value.lower() and "refresh" in error.value.lower() for error in app.error)
    assert [call for call in state["calls"] if call[0] in ("list", "fetch")] == calls_before
    assert not any(box.label == "Choose a message" for box in app.selectbox)
    assert not any(button.label == "Load more" for button in app.button)
    assert app.session_state["mails"] == {}
    assert not any(area.label == "Message" for area in app.text_area)
    find(app.button, "Refresh inbox").click()
    checked(app)
    assert state["calls"][-1] == ("list", 20, None)
    assert len(find(app.selectbox, "Choose a message").options) == 2
    assert find(app.selectbox, "Choose a message").value.startswith("different")


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

    # A rerun or a batch-size change must not repeat connection/profile requests.
    calls_before = list(state["calls"])
    checked(reopened)
    find(reopened.selectbox, "Messages per batch").select(5)
    checked(reopened)
    assert state["calls"] == calls_before
    assert not find(reopened.button, "Refresh inbox").disabled
    find(reopened.button, "Refresh inbox").click()
    checked(reopened)
    assert ("list", 5, None) in state["calls"]
    assert not any(call[0] == "fetch" for call in state["calls"])


def test_explicit_disconnect_survives_new_browser_and_clears_messages(tmp_path, monkeypatch):
    token_path, state = saved_connection(tmp_path, monkeypatch)
    app = checked(AppTest.from_file(str(APP)))
    find(app.button, "Refresh inbox").click()
    checked(app)
    find(app.button, "Open message").click()
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


@pytest.mark.parametrize("action", ["Refresh inbox", "Load more", "Open message"])
def test_revoked_login_during_message_action_prompts_reconnect(tmp_path, monkeypatch, action):
    token_path, state = saved_connection(tmp_path, monkeypatch)
    app = checked(AppTest.from_file(str(APP)))
    find(app.button, "Refresh inbox").click()
    checked(app)
    error = gmail.GmailAuthError("Your Gmail sign-in has expired. Reconnect Gmail to continue.")
    state["fetch_error" if action == "Open message" else "list_error"] = error
    find(app.button, action).click()
    checked(app)
    assert token_path.exists()
    assert find(app.button, "Reconnect Gmail")
    assert find(app.button, "Refresh inbox").disabled
    assert "Reconnect Gmail" in interface_text(app)
    assert not any(call == ("connect", True) for call in state["calls"])
