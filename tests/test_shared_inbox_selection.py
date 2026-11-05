"""Shared source selection keeps category review and extraction explicitly scoped."""

import test_inbox_triage_ui as triage_tests
from streamlit.testing.v1 import AppTest
from test_batch_ui import APP_SOURCE, setup_fakes
from test_fixed_events_ui import event_result
from test_inbox_triage import ACCOUNT
from test_inbox_triage_ui import SCRIPT, checkbox, run

from studentflow import batch_ui
from studentflow.inbox_triage import InboxTriageStore
from studentflow.storage import Store

fixture = triage_tests.fixture

BATCH_SHARED = APP_SOURCE.replace(
    "'test-key', 'model-test', True)",
    "'test-key', 'model-test', True, selected_ids=st.session_state.get('shared_ids', []))",
)
TRIAGE_SHARED = SCRIPT.replace(
    "'TEST-KEY', 'test-model', True)",
    "'TEST-KEY', 'test-model', True, selected_ids=st.session_state.get('shared_ids', []))",
)
BATCH_NAVIGATION = BATCH_SHARED.replace(
    "render_batch_extraction(headers",
    "from studentflow.batch_ui import preserve_batch_review_inputs\n"
    "preserve_batch_review_inputs()\n"
    "page = st.radio('Page', ['Inbox', 'Tasks'], key='test_page')\n"
    "if page == 'Inbox':\n"
    "    render_batch_extraction(headers",
)


def test_external_batch_selection_opens_and_extracts_only_current_sources(tmp_path, monkeypatch):
    monkeypatch.setenv('BATCH_TEST_RECORDS', str(tmp_path))
    calls = setup_fakes(monkeypatch)
    app = run(AppTest.from_string(BATCH_SHARED))
    assert not app.multiselect
    assert not any(button.key in {'batch_all', 'batch_clear'} for button in app.button)
    assert app.button(key='batch_extract').disabled and app.button(key='batch_view').disabled
    assert calls == []

    app.session_state['shared_ids'] = ['b', 'missing', 'b']
    run(app)
    assert calls == [] and app.button(key='batch_extract').disabled
    run(app.button(key='batch_view').click())
    assert [call for call in calls if call[0] == 'fetch'] == [('fetch', 'b')]
    run(app.checkbox(key='batch_consent').check())
    run(app.button(key='batch_extract').click())
    assert [call[1] for call in calls if call[0] == 'extract'] == ['Assignment b']
    assert app.dataframe[0].value['Source message'].tolist() == ['Assignment b']
    assert Store(tmp_path / 'tasks.sqlite3').tasks() == []

    app.session_state['shared_ids'] = ['a']
    run(app)
    assert not app.checkbox(key='batch_consent').value
    assert app.button(key='batch_extract').disabled and not app.dataframe
    assert any('selected messages, model or analysis options changed' in item.value for item in app.info)


def test_external_triage_selection_limits_review_save_and_confirmed_sync(fixture):
    root, calls, client = fixture
    app = AppTest.from_string(TRIAGE_SHARED)
    app.session_state['shared_ids'] = ['abc1', 'abc2']
    run(app)
    assert not any(item.key == 'batch_selected' for item in app.multiselect)
    assert not any(button.key in {'sf_triage_all', 'sf_triage_clear'} for button in app.button)
    assert calls == []
    run(checkbox(app, 'Send the selected email text').check())
    run(app.button(key='sf_triage_run').click())
    assert ('fetch', ('abc1', 'abc2')) in calls

    run(app.button(key='sf_triage_preview_button').click())
    run(checkbox(app, 'I have reviewed these label changes').check())
    app.session_state['shared_ids'] = ['abc2', 'missing', 'abc2']
    run(app)
    assert app.selectbox(key='sf_triage_detail').value == 'abc2'
    assert app.selectbox(key='sf_triage_detail').options == ['Course message 2']
    assert not any(button.key == 'sf_triage_apply' for button in app.button)
    assert app.button(key='sf_triage_run').disabled

    category = next(item for item in app.selectbox if item.label == 'Primary category')
    run(category.set_value('ignore'))
    assert app.button(key='sf_triage_preview_button').disabled
    run(app.button(key='sf_triage_manual_save').click())
    store = InboxTriageStore(root / 'db.sqlite3')
    assert store.get(ACCOUNT, 'abc2')['category'] == 'ignore'
    assert store.get(ACCOUNT, 'abc1')['category'] == 'needs_reply'
    assert app.session_state['sf_triage_pending_selection'] == ['abc2']
    assert not [call for call in calls if call[0] == 'write']

    run(app.button(key='sf_triage_preview_button').click())
    assert app.button(key='sf_triage_apply').disabled
    run(checkbox(app, 'I have reviewed these label changes').check())
    run(app.button(key='sf_triage_apply').click())
    assert [call for call in calls if call[0] == 'write'] == [('write', 'abc2', 'ignore')]
    assert client.messages['abc1'] == {'INBOX', 'UNREAD', 'personal'}
    run(app.button(key='sf_triage_reply').click())
    assert app.session_state['sf_reply_handoff']['mail_id'] == 'local-2'
    run(app.button(key='sf_triage_tasks').click())
    assert app.session_state['batch_selected'] == ['abc2']
    assert app.session_state['sf_inbox_workflow'] == 'Tasks & events'
    assert not app.session_state['batch_consent']


def test_external_triage_filter_narrows_review_without_changing_classification_scope(fixture):
    _root, calls, _client = fixture
    app = AppTest.from_string(TRIAGE_SHARED)
    app.session_state['shared_ids'] = ['abc1', 'abc2']
    run(app)
    run(app.selectbox(key='sf_triage_filter').set_value('ignore'))
    run(checkbox(app, 'Send the selected email text').check())
    run(app.button(key='sf_triage_run').click())
    assert ('fetch', ('abc1', 'abc2')) in calls
    assert app.selectbox(key='sf_triage_filter').value == 'All messages'
    run(app.selectbox(key='sf_triage_filter').set_value('ignore'))
    assert not any(button.key == 'sf_triage_save' for button in app.button)
    assert not app.button(key='sf_triage_run').disabled
    app.session_state['shared_ids'] = []
    run(app)
    assert app.button(key='sf_triage_run').disabled
    assert not any(button.key in {'sf_triage_save', 'sf_triage_apply', 'sf_triage_manual_save'} for button in app.button)
    assert not any(item.key == 'batch_selected' for item in app.multiselect)


def test_navigation_preserves_task_and_event_edits_without_review_approval(tmp_path, monkeypatch):
    monkeypatch.setenv('BATCH_TEST_RECORDS', str(tmp_path))
    calls = setup_fakes(monkeypatch)
    monkeypatch.setattr(batch_ui, 'analyze_fixed_event_messages', lambda *_args, **_kwargs: {
        **event_result(), 'results': [], 'complete': True,
    })
    app = AppTest.from_string(BATCH_NAVIGATION)
    app.session_state['shared_ids'] = ['a']
    run(app)
    run(app.checkbox(key='batch_suggest_effort').uncheck())
    run(app.checkbox(key='batch_consent').check())
    run(app.button(key='batch_extract').click())
    task_key = next(key for key in app.session_state if key.startswith('batch_editor_'))
    event_key = next(key for key in app.session_state if key.startswith('batch_unified_fixed_') and key.endswith('_rows'))
    app.session_state[task_key] = {
        'edited_rows': {0: {'Include': True, 'Task': 'Reviewed report', 'Deadline': '2026-10-17 09:00',
                            'Study time (min)': 90}}, 'added_rows': [], 'deleted_rows': [],
    }
    app.session_state[event_key] = {
        'edited_rows': {0: {'Include': True, 'Event': 'Reviewed exam', 'Start (Singapore)': '2026-10-28 15:00',
                            'End (Singapore)': '2026-10-28 17:00', 'Location': 'LT1'}},
        'added_rows': [], 'deleted_rows': [],
    }
    run(checkbox(app, 'I have reviewed the selected tasks').check())
    run(checkbox(app, 'I have checked the event dates').check())
    app.session_state[checkbox(app, 'I have reviewed the selected tasks').key] = True
    before = list(calls)
    run(app.radio(key='test_page').set_value('Tasks'))
    assert task_key not in app.session_state and event_key not in app.session_state
    run(app.radio(key='test_page').set_value('Inbox'))
    assert calls == before
    assert not app.checkbox(key='batch_suggest_effort').value
    assert not checkbox(app, 'I have reviewed the selected tasks').value
    assert not checkbox(app, 'I have checked the event dates').value
    task_rows = next(frame.value for frame in app.dataframe if 'Task' in frame.value)
    event_rows = next(frame.value for frame in app.dataframe if 'Event' in frame.value)
    assert task_rows.iloc[0]['Include'] and task_rows.iloc[0]['Task'] == 'Reviewed report'
    assert task_rows.iloc[0]['Deadline'] == '2026-10-17 09:00'
    assert task_rows.iloc[0]['Study time (min)'] == 90
    assert event_rows.iloc[0]['Include'] and event_rows.iloc[0]['Event'] == 'Reviewed exam'
    assert event_rows.iloc[0]['Start (Singapore)'] == '2026-10-28 15:00'
    assert event_rows.iloc[0]['Location'] == 'LT1'

    checkbox(app, 'I have reviewed the selected tasks').check()
    run(next(button for button in app.button if button.label == 'Save selected tasks').click())
    assert not app.error, [error.value for error in app.error]
    run(checkbox(app, 'I have checked the event dates').check())
    run(next(button for button in app.button if button.label == 'Save selected fixed events').click())
    store = Store(tmp_path / 'tasks.sqlite3')
    assert store.tasks(), [warning.value for warning in app.warning]
    assert store.tasks()[0]['title'] == 'Reviewed report'
    assert store.tasks()[0]['estimated_minutes'] == 90
    assert store.list_fixed_events()[0]['title'] == 'Reviewed exam'
    assert store.list_fixed_events()[0]['start_at'] == '2026-10-28T15:00:00+08:00'
    assert store.list_fixed_events()[0]['sources'] == event_result()['events'][0]['sources']

    # A new analysis run must start from new proposals, never from old edits.
    run(app.checkbox(key='batch_consent').check())
    run(app.button(key='batch_extract').click())
    task_rows = next(frame.value for frame in app.dataframe if 'Task' in frame.value)
    event_rows = next(frame.value for frame in app.dataframe if 'Event' in frame.value)
    assert task_rows.iloc[0]['Task'] == 'Complete Assignment a' and not task_rows.iloc[0]['Include']
    assert event_rows.iloc[0]['Event'] == 'DSS5105 exam' and not event_rows.iloc[0]['Include']
