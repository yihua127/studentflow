"""Offline interface journey. OAuth and Gmail requests are replaced with local fakes."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from studentflow import gmail


APP = Path(__file__).resolve().parents[1] / "app.py"


def find(elements, label):
    return next(element for element in elements if element.label == label)


def checked(app):
    app.run(timeout=30)
    assert not app.exception
    return app


def test_explicit_mail_connection_import_and_per_message_model_consent(tmp_path, monkeypatch):
    client_path = tmp_path / "client.json"
    client_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("STUDENTFLOW_DATA_DIR", str(tmp_path / "records"))
    monkeypatch.setenv("STUDENTFLOW_GMAIL_CLIENT", str(client_path))
    calls = []

    class FakeMailbox:
        def profile(self):
            return {"email_address": "student@example.com"}

        def list_messages(self, limit):
            calls.append(("list", limit))
            return [{"id": "demo123", "subject": "Assignment", "sender": "teacher@example.com", "sent_at": "Fri, 9 Oct 2026 10:00:00 +0800"}]

        def fetch_eml(self, message_id):
            calls.append(("fetch", message_id))
            return b"From: teacher@example.com\r\nSubject: Assignment\r\nDate: Fri, 9 Oct 2026 10:00:00 +0800\r\n\r\nSubmit the report by October 15 at 17:00."

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
    assert find(app.button, "登录并授权 Gmail").disabled
    find(app.checkbox, "我同意 StudentFlow 通过 Google 授权读取我选择的邮箱").check()
    checked(app)
    find(app.button, "登录并授权 Gmail").click()
    checked(app)
    assert calls == [("connect", True)]
    find(app.button, "读取收件箱列表").click()
    checked(app)
    assert ("list", 5) in calls
    assert not any(call[0] == "fetch" for call in calls)
    find(app.button, "导入选中的真实邮件").click()
    checked(app)
    assert ("fetch", "demo123") in calls
    # Imported mail appears on the next rerun, with cloud-model consent unchecked.
    checked(app)
    model_consent = next(item for item in app.checkbox if item.label.startswith("同意将当前邮件"))
    assert model_consent.value is False
    assert len(app.session_state["mails"]) == 1
    find(app.button, "导入选中的真实邮件").click()
    checked(app)
    assert len(app.session_state["mails"]) == 1
    find(app.button, "退出本机 Gmail 授权").click()
    checked(app)
    assert app.session_state["mails"] == {}
    assert not (tmp_path / "records" / "auth" / "gmail_token.json").exists()
