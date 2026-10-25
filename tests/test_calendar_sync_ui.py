"""UI regression: rendering/preview cannot sync; exact reviewed action can."""

from streamlit.testing.v1 import AppTest

from studentflow import calendar_sync_ui
from studentflow.google_workspace import CALENDAR_SYNC_SCOPES

APP = '''
import os
from pathlib import Path
import streamlit as st
from studentflow.calendar_sync_ui import render_calendar_sync
class Store:
    def latest_plan(self):
        return {"id": "saved"}
root = Path(os.environ["CALENDAR_UI_ROOT"])
render_calendar_sync(Store(), root, root / "data", fixed_events=[],
    current_fingerprint=lambda: st.session_state.get("revision", "revision1"),
    saved_plan_is_fresh=lambda _: st.session_state.get("fresh", True))
'''


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("CALENDAR_UI_ROOT", str(tmp_path))
    client = tmp_path / "credentials" / "gmail_client.json"
    client.parent.mkdir()
    client.write_text("{}")
    monkeypatch.setenv("STUDENTFLOW_GMAIL_CLIENT", str(client))
    token = tmp_path / "data" / "auth" / "google_calendar_sync_token.json"
    token.parent.mkdir(parents=True)
    token.write_text("{}")
    calls = []

    class Client:
        def close(self):
            pass

        def profile(self):
            return {"email_address": "student@example.test"}

    def connect(*args, **kwargs):
        calls.append(("connect", kwargs))
        return Client()

    def preview(*args, **kwargs):
        calls.append(("preview", kwargs))
        return {"signature": "reviewed", "fingerprint": "revision1",
                "include_study_plan": kwargs.get("include_study_plan", True),
                "account": "student@example.test", "create_calendar": True,
                "operations": [{"action": "create", "event": {
                    "summary": "Exam", "start": {"dateTime": "2036-10-24T09:00:00+08:00"},
                    "end": {"dateTime": "2036-10-24T10:00:00+08:00"},
                }}]}

    def apply(*args, **kwargs):
        calls.append(("apply", kwargs))
        return {"counts": {"create": 1, "remove": 0, "update": 0}}

    monkeypatch.setattr(calendar_sync_ui, "connect_workspace", connect)
    monkeypatch.setattr(calendar_sync_ui, "prepare_sync", preview)
    monkeypatch.setattr(calendar_sync_ui, "apply_sync", apply)
    return calls


def test_render_and_preview_do_not_write_until_explicit_reviewed_sync(tmp_path, monkeypatch):
    calls = setup(tmp_path, monkeypatch)
    app = AppTest.from_string(APP).run(timeout=30)
    assert not app.exception and calls == []
    app.button(key="sf_calendar_sync_preview_button").click().run()
    assert not app.exception
    assert [call[0] for call in calls] == ["connect", "preview"]
    assert app.button(key="sf_calendar_sync_apply_reviewed").disabled
    app.checkbox(key="sf_calendar_sync_review_reviewed").check().run()
    assert [call[0] for call in calls] == ["connect", "preview"]
    app.button(key="sf_calendar_sync_apply_reviewed").click().run()
    assert [call[0] for call in calls] == ["connect", "preview", "connect", "apply"]
    assert all(call[1]["required_scopes"] == CALENDAR_SYNC_SCOPES
               for call in calls if call[0] == "connect")


def test_changed_source_invalidates_reviewed_preview_without_writes(tmp_path, monkeypatch):
    calls = setup(tmp_path, monkeypatch)
    app = AppTest.from_string(APP).run(timeout=30)
    app.button(key="sf_calendar_sync_preview_button").click().run()
    app.session_state["revision"] = "changed"
    app.run()
    assert not app.exception
    assert not any(item.label == "Sync reviewed changes" for item in app.button)
    assert "apply" not in [call[0] for call in calls]


def test_stale_saved_plan_allows_fixed_events_without_study_sessions(tmp_path, monkeypatch):
    calls = setup(tmp_path, monkeypatch)
    app = AppTest.from_string(APP)
    app.session_state["fresh"] = False
    app.run(timeout=30)
    assert not app.exception
    assert not app.button(key="sf_calendar_sync_preview_button").disabled
    assert app.checkbox(key="sf_calendar_sync_include_plan").disabled
    assert not app.checkbox(key="sf_calendar_sync_include_plan").value
    assert calls == []
    app.button(key="sf_calendar_sync_preview_button").click().run()
    assert not app.exception
    assert calls[-1][1]["include_study_plan"] is False


def test_explicit_connection_is_separate_and_requests_minimal_scopes(tmp_path, monkeypatch):
    calls = setup(tmp_path, monkeypatch)
    app = AppTest.from_string(APP).run(timeout=30)
    app.button(key="sf_calendar_sync_connect").click().run()
    assert not app.exception
    assert calls == [("connect", {"interactive": True, "required_scopes": CALENDAR_SYNC_SCOPES})]
