"""Product interface journeys with isolated records and a fake Gmail service."""

from email.message import EmailMessage
from pathlib import Path
import re

import dotenv
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

        def list_messages(self, limit):
            calls.append(("list", limit))
            return [{
                "id": "demo123", "subject": "Assignment", "sender": "teacher@example.com",
                "sent_at": "Fri, 9 Oct 2026 10:00:00 +0800",
            }]

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
    assert ("list", 5) in calls
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
