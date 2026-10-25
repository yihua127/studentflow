"""Offline AppTest journeys for selecting Chat messages separately from attachments.

The room/read controls and Streamlit label serialization are real. Google reads
and assistant results are synthetic; these tests make no provider requests.
"""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from studentflow import assistant_ui, workspace_ui
from studentflow.assistant_context import source_key

ACCOUNT = "student@example.invalid"
ROOM = "spaces/project"
OTHER_ROOM = "spaces/other-project"
ATTACHMENT_ID = source_key("attachment", ACCOUNT, "discussion-brief")
MAIL_ID = source_key("gmail", ACCOUNT, "selected-email")
SEND_CONSENT = "Send this request and only the selected information to OpenAI."
RESULT_TEXT = "SYNTHETIC DISCUSSION RESULT: the selected messages were reviewed."
MODES = [
    ("Meetings & discussions", "sf_meeting_", "sf_meeting_sources", "sf_meeting_attachments"),
    ("Other assistance", "sf_general_", "sf_general_messages", "sf_general_sources"),
]

SCRIPT = """
import os
from pathlib import Path
import streamlit as st
from studentflow.storage import Store
from studentflow.assistant_ui import render_assistant
root = Path(os.environ['SF_DISCUSSION_SELECTION_ROOT'])
store = Store(root / 'data' / 'studentflow.sqlite3')
st.session_state['gmail_profile'] = {'email_address': 'student@example.invalid'}
mails = {'selected-email': {
    'gmail_account': 'student@example.invalid', 'subject': 'Report instructions',
    'body': 'Please confirm receipt of the report instructions.',
    'sender': 'teacher@example.invalid', 'provider': 'gmail',
}}
render_assistant(store, root / 'data', root, mails, 'offline-api-key', 'offline-model')
"""


def messages(room=ROOM, count=4):
    return [{
        "name": f"{room}/messages/{index + 1}",
        "account": ACCOUNT,
        "title": "Project discussion",
        "text": f"Message {index + 1}: prepare the project outline and review the evidence.",
        "sent_at": f"2026-10-25T01:{index:02d}:00+00:00",
        "sender_id": f"users/{index % 2 + 1}",
        "sender_name": "Alex" if index % 2 == 0 else "Beth",
        "sender_type": "HUMAN",
    } for index in range(count)]


def ids(records):
    return [source_key("google_chat", item["account"], item["name"]) for item in records]


def checked(app):
    app.run(timeout=30)
    assert not app.exception
    return app


def send_consent(app):
    return next(element for element in app.checkbox if element.label == SEND_CONSENT)


def load_discussion(app, mode):
    app.radio(key="sf_assistant_workspace").set_value(mode)
    checked(app)
    app.checkbox(key="sf_workspace_consent").check()
    checked(app)
    app.button(key="sf_workspace_load_spaces").click()
    checked(app)
    app.button(key="sf_workspace_read_chat").click()
    return checked(app)


@pytest.fixture
def discussion_ui(tmp_path, monkeypatch):
    monkeypatch.setenv("SF_DISCUSSION_SELECTION_ROOT", str(tmp_path))
    client_path = tmp_path / "credentials" / "gmail_client.json"
    client_path.parent.mkdir()
    client_path.write_text("{}", encoding="utf-8")
    token_path = tmp_path / "data" / "auth" / "google_workspace_token.json"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("STUDENTFLOW_GMAIL_CLIENT", str(client_path))
    state = {"rooms": {ROOM: messages(), OTHER_ROOM: messages(OTHER_ROOM)},
             "reads": [], "runs": []}

    class SyntheticWorkspace:
        def profile(self):
            return {"email_address": ACCOUNT}

        def list_spaces(self):
            return [{"name": ROOM, "display_name": "Project discussion"},
                    {"name": OTHER_ROOM, "display_name": "Another project discussion"}]

        def list_messages(self, room):
            state["reads"].append(room)
            # Deliberately return the provider batch in reverse order.
            return deepcopy(list(reversed(state["rooms"][room])))

        def close(self):
            pass

    monkeypatch.setattr(workspace_ui, "connect_workspace",
                        lambda *_args, **_kwargs: SyntheticWorkspace())
    monkeypatch.setattr(assistant_ui, "available_documents", lambda *_args: [{
        "library_id": "discussion-brief", "filename": "discussion-brief.pdf",
        "gmail_account": ACCOUNT, "text": "The project brief supplies background context.",
    }])

    def fake_run(request, toolbox, **_kwargs):
        state["runs"].append({"request": request, "sources": deepcopy(toolbox._sources),
                              "styles": deepcopy(toolbox._styles)})
        return SimpleNamespace(answer=RESULT_TEXT, status="completed", events=[],
                               usage={"model_calls": 1}, artifacts=toolbox.artifacts)

    monkeypatch.setattr(assistant_ui, "run_assistant", fake_run)
    return state


@pytest.mark.parametrize("mode,prefix,chat_key,other_key", MODES)
def test_same_room_messages_have_distinct_labels_and_keep_three_selections(
    discussion_ui, mode, prefix, chat_key, other_key,
):
    app = load_discussion(checked(AppTest.from_string(SCRIPT)), mode)
    choices = app.multiselect(key=chat_key)
    assert choices.label == "Messages to analyze"
    assert len(choices.options) == len(set(choices.options)) == 4
    for index, label in enumerate(choices.options):
        assert label.startswith(f"{index + 1}.")
        assert f"09:0{index}:00" in label
        assert ("Alex" if index % 2 == 0 else "Beth") in label
        assert f"Message {index + 1}:" in label
    other_choices = app.multiselect(key=other_key)
    assert any("discussion-brief.pdf" in item for item in other_choices.options)
    assert not any("Message 1:" in item for item in other_choices.options)
    if prefix == "sf_meeting_":
        assert other_choices.label == "Supporting attachments (optional)"
        assert not any("Report instructions" in item for item in other_choices.options)

    selected = ids(discussion_ui["rooms"][ROOM])[:3]
    for identifier in selected:
        app.multiselect(key=chat_key).select(identifier)
        checked(app)
    app.text_area(key=prefix + "request").set_value("Summarize the selected discussion in English.")
    checked(app)
    assert app.multiselect(key=chat_key).value == selected
    assert app.session_state[chat_key] == selected
    assert discussion_ui["reads"] == [ROOM]


@pytest.mark.parametrize("mode,prefix,chat_key,other_key", MODES)
def test_all_and_clear_messages_leave_supporting_information_selected(
    discussion_ui, mode, prefix, chat_key, other_key,
):
    app = load_discussion(checked(AppTest.from_string(SCRIPT)), mode)
    supporting = [ATTACHMENT_ID] if prefix == "sf_meeting_" else [ATTACHMENT_ID, MAIL_ID]
    app.multiselect(key=other_key).set_value(supporting)
    checked(app)
    assert app.button(key=prefix + "select_all_messages").label == "Select all messages"
    app.button(key=prefix + "select_all_messages").click()
    checked(app)
    assert app.multiselect(key=chat_key).value == ids(discussion_ui["rooms"][ROOM])
    assert app.multiselect(key=other_key).value == supporting
    assert app.button(key=prefix + "clear_messages").label == "Clear message selection"
    app.button(key=prefix + "clear_messages").click()
    checked(app)
    assert app.multiselect(key=chat_key).value == []
    assert app.multiselect(key=other_key).value == supporting
    assert app.button(key=prefix + "clear_messages").disabled


@pytest.mark.parametrize("mode,prefix,chat_key,other_key", MODES)
def test_select_all_accepts_more_than_twenty_discussion_messages(
    discussion_ui, mode, prefix, chat_key, other_key,
):
    discussion_ui["rooms"][ROOM] = messages(count=25)
    app = load_discussion(checked(AppTest.from_string(SCRIPT)), mode)
    app.button(key=prefix + "select_all_messages").click()
    checked(app)
    assert app.multiselect(key=chat_key).value == ids(discussion_ui["rooms"][ROOM])
    assert len(app.multiselect(key=chat_key).value) == 25
    checked(app)
    assert len(app.multiselect(key=chat_key).value) == 25


@pytest.mark.parametrize("mode,prefix,chat_key,other_key", MODES)
def test_only_selected_messages_and_supporting_attachment_reach_assistant(
    discussion_ui, mode, prefix, chat_key, other_key,
):
    app = load_discussion(checked(AppTest.from_string(SCRIPT)), mode)
    selected = ids(discussion_ui["rooms"][ROOM])[1:]
    app.multiselect(key=chat_key).set_value(list(reversed(selected)))
    app.multiselect(key=other_key).set_value([ATTACHMENT_ID])
    app.text_area(key=prefix + "request").set_value("Summarize these three selected messages.")
    checked(app)
    send_consent(app).check()
    checked(app)
    app.button(key=prefix + "run").click()
    checked(app)
    assert len(discussion_ui["runs"]) == 1
    received = discussion_ui["runs"][0]
    assert list(received["sources"]) == [*selected, ATTACHMENT_ID]
    assert len(received["sources"]) == 4
    assert received["styles"] == []
    assert received["request"] == "Summarize these three selected messages."
    assert RESULT_TEXT in [item.value for item in app.text]


@pytest.mark.parametrize("mode,prefix,chat_key,other_key", MODES)
def test_changing_room_clears_old_messages_approval_and_report(
    discussion_ui, mode, prefix, chat_key, other_key,
):
    app = load_discussion(checked(AppTest.from_string(SCRIPT)), mode)
    app.button(key=prefix + "select_all_messages").click()
    checked(app)
    app.multiselect(key=other_key).set_value([ATTACHMENT_ID])
    app.text_area(key=prefix + "request").set_value("Summarize this discussion in English.")
    checked(app)
    send_consent(app).check()
    checked(app)
    app.button(key=prefix + "run").click()
    checked(app)
    assert RESULT_TEXT in [item.value for item in app.text]

    app.selectbox(key="sf_workspace_space").set_value(OTHER_ROOM)
    checked(app)
    assert app.multiselect(key=chat_key).value == []
    assert app.multiselect(key=chat_key).options == []
    assert not send_consent(app).value
    assert app.button(key=prefix + "run").disabled
    assert RESULT_TEXT not in [item.value for item in app.text]
    assert app.multiselect(key=other_key).value == [ATTACHMENT_ID]
    assert discussion_ui["reads"] == [ROOM]

    app.button(key="sf_workspace_read_chat").click()
    checked(app)
    assert app.multiselect(key=chat_key).value == []
    assert not send_consent(app).value
    app.button(key=prefix + "select_all_messages").click()
    checked(app)
    assert app.multiselect(key=chat_key).value == ids(discussion_ui["rooms"][OTHER_ROOM])
    assert not set(app.multiselect(key=chat_key).value).intersection(
        ids(discussion_ui["rooms"][ROOM])
    )
    assert len(discussion_ui["runs"]) == 1


@pytest.mark.parametrize("mode,prefix,chat_key,other_key", MODES)
def test_identical_message_content_still_keeps_distinct_source_ids(
    discussion_ui, mode, prefix, chat_key, other_key,
):
    for message in discussion_ui["rooms"][ROOM]:
        message.update(text="Agreed.", sender_name="Alex", sent_at="2026-10-25T01:00:00+00:00")
    app = load_discussion(checked(AppTest.from_string(SCRIPT)), mode)
    assert len(set(app.multiselect(key=chat_key).options)) == 4
    selected = ids(discussion_ui["rooms"][ROOM])[:3]
    for identifier in selected:
        app.multiselect(key=chat_key).select(identifier)
        checked(app)
    assert app.multiselect(key=chat_key).value == selected
    checked(app)
    assert app.multiselect(key=chat_key).value == selected
