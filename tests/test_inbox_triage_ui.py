"""Classification and Gmail-label journeys with isolated data and offline services."""

import sqlite3

import pytest
from streamlit.testing.v1 import AppTest
from test_inbox_triage import ACCOUNT, mail, runner

from studentflow import inbox_triage
from studentflow import inbox_triage_ui as ui
from studentflow.email_label_catalog import LabelCatalog, gmail_label_names
from studentflow.gmail_labels import TOKEN_FILENAME

SCRIPT = '''
import os
from pathlib import Path
import streamlit as st
from studentflow.inbox_triage_ui import render_inbox_triage
from studentflow.storage import Store
root = Path(os.environ['TRIAGE_UI_ROOT'])
st.session_state.setdefault('gmail_profile', {'email_address': 'student@example.invalid'})
st.session_state.setdefault('mails', {})
st.session_state.setdefault('candidates', {})
workflow = st.radio('Inbox workflow', ['Tasks & events', 'Email categories'], index=1, key='sf_inbox_workflow')
if workflow == 'Email categories':
    render_inbox_triage([
        {'id': 'abc1', 'subject': 'Course message 1', 'sender': 'Teacher'},
        {'id': 'abc2', 'subject': 'Course message 2', 'sender': 'Teacher'},
    ], root / 'client.json', root / 'read-token.json', Store(root / 'db.sqlite3'), root,
        'TEST-KEY', 'test-model', True)
'''


def run(app):
    app = app.run(timeout=30)
    assert not app.exception
    return app


def checkbox(app, text):
    return next(item for item in app.checkbox if item.label.startswith(text))


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setenv('TRIAGE_UI_ROOT', str(tmp_path))
    (tmp_path / 'client.json').write_text('{}')
    (tmp_path / 'auth').mkdir()
    (tmp_path / 'auth' / TOKEN_FILENAME).write_text('{}')
    calls = []

    def fetch(ids, _client, _token, profile, mails, **kwargs):
        calls.append(('fetch', tuple(ids)))
        result = []
        for identifier in ids:
            value = mail(int(identifier[-1]), gmail_account=profile['email_address'])
            mails[value['id']] = value
            result.append({'gmail_id': identifier, 'mail_id': value['id']})
        return result

    def classify(*args, **kwargs):
        calls.append(('classify',))
        return inbox_triage.classify_messages(*args, **kwargs, runner=runner)

    class Client:
        account_name = ACCOUNT

        def __init__(self):
            self.labels = {}
            self.named_labels = {}
            self.catalog_names = {}
            self.allowed_names = set()
            self.messages = {'abc1': {'INBOX', 'UNREAD', 'personal'}, 'abc2': {'INBOX', 'personal'}}

        def account(self):
            return self.account_name

        def label_ids(self):
            return self.labels.copy()

        def message_labels(self, identifier):
            return self.messages[identifier].copy()

        def create_label(self, category):
            calls.append(('create', category))
            self.labels[category] = 'Label_' + category
            return self.labels[category]

        def set_category(self, identifier, category, labels):
            calls.append(('write', identifier, category))
            self.messages[identifier] -= set(labels.values())
            self.messages[identifier].add(labels[category])

        def configure_catalog(self, catalog):
            self.catalog_names, self.allowed_names = gmail_label_names(catalog)

        def managed_label_ids(self):
            return {name: identifier for name, identifier in self.named_labels.items() if name in self.allowed_names}

        def create_named_label(self, name):
            key = next(key for key, label in self.catalog_names.items() if label == name)
            calls.append(('create', key))
            self.named_labels[name] = 'Label_' + key + '_' + str(len(self.named_labels))
            if not key.startswith('tag_') and name.endswith({'needs_reply': 'Needs reply', 'needs_action': 'Needs action', 'ignore': 'No action needed'}[key]):
                self.named_labels[name] = 'Label_' + key
            return self.named_labels[name]

        def set_named_labels(self, identifier, names, labels):
            category = next(key for key in ('needs_reply', 'needs_action', 'ignore') if self.catalog_names[key] in names)
            calls.append(('write', identifier, category))
            calls.append(('named_write', identifier, tuple(sorted(names))))
            self.messages[identifier] -= set(labels.values())
            self.messages[identifier].update(labels[name] for name in names)

        def close(self):
            pass

    client = Client()

    def connect(*args, **kwargs):
        calls.append(('connect_labels', kwargs.get('interactive', False)))
        return client

    monkeypatch.setattr(ui, 'fetch_selected_messages', fetch)
    monkeypatch.setattr(ui, 'classify_messages', classify)
    monkeypatch.setattr(ui, 'connect_labels', connect)
    return tmp_path, calls, client


def classified(app, ids=None):
    run(app.multiselect(key='batch_selected').set_value(ids or ['abc1', 'abc2']))
    run(checkbox(app, 'Send the selected email text').check())
    run(app.button(key='sf_triage_run').click())
    return app


def test_load_and_selection_do_not_call_model_or_google(fixture):
    _, calls, _ = fixture
    app = run(AppTest.from_string(SCRIPT))
    assert calls == [] and app.button(key='sf_triage_run').disabled
    run(app.multiselect(key='batch_selected').set_value(['abc1']))
    assert calls == [] and app.button(key='sf_triage_run').disabled
    run(checkbox(app, 'Send the selected email text').check())
    assert calls == [] and not app.button(key='sf_triage_run').disabled
    run(app.multiselect(key='batch_selected').set_value(['abc2']))
    assert app.button(key='sf_triage_run').disabled


def test_classification_persists_and_review_changes_do_not_touch_gmail(fixture):
    root, calls, _ = fixture
    app = classified(run(AppTest.from_string(SCRIPT)))
    store = inbox_triage.InboxTriageStore(root / 'db.sqlite3')
    assert len(store.list(ACCOUNT)) == 2
    assert not [call for call in calls if call[0] == 'connect_labels']
    editor = next(key for key in app.session_state if key.startswith('sf_triage_editor_'))
    app.session_state[editor] = {'edited_rows': {0: {'Category': 'No action needed'}}, 'added_rows': [], 'deleted_rows': []}
    run(app.button(key='sf_triage_save').click())
    assert store.get(ACCOUNT, 'abc1')['category'] == 'ignore'
    assert store.get(ACCOUNT, 'abc1')['reviewed']
    assert not [call for call in calls if call[0] in {'write', 'create'}]
    reopened = run(AppTest.from_string(SCRIPT))
    run(reopened.selectbox(key='sf_triage_filter').set_value('ignore'))
    assert len(reopened.multiselect(key='batch_selected').options) == 1


def test_preview_is_read_only_and_confirm_applies_only_checked_emails(fixture):
    root, calls, client = fixture
    app = classified(run(AppTest.from_string(SCRIPT)), ['abc1'])
    run(app.button(key='sf_triage_preview_button').click())
    assert app.button(key='sf_triage_apply').disabled
    assert not [call for call in calls if call[0] in {'create', 'write'}]
    run(checkbox(app, 'I have reviewed these label changes').check())
    assert not [call for call in calls if call[0] in {'create', 'write'}]
    run(app.button(key='sf_triage_apply').click())
    assert ('write', 'abc1', 'needs_reply') in calls
    assert client.messages['abc1'] == {'INBOX', 'UNREAD', 'personal', 'Label_needs_reply'}
    assert client.messages['abc2'] == {'INBOX', 'personal'}
    assert inbox_triage.InboxTriageStore(root / 'db.sqlite3').get(ACCOUNT, 'abc1')['reviewed']
    writes = [call for call in calls if call[0] == 'write']
    run(app)
    assert [call for call in calls if call[0] == 'write'] == writes


def test_category_edit_invalidates_remote_preview(fixture):
    _, calls, _ = fixture
    app = classified(run(AppTest.from_string(SCRIPT)))
    run(app.button(key='sf_triage_preview_button').click())
    run(checkbox(app, 'I have reviewed these label changes').check())
    editor = next(key for key in app.session_state if key.startswith('sf_triage_editor_'))
    app.session_state[editor] = {'edited_rows': {0: {'Category': 'Needs action'}}, 'added_rows': [], 'deleted_rows': []}
    run(app)
    assert not any(button.key == 'sf_triage_apply' for button in app.button)
    assert not [call for call in calls if call[0] == 'write']


def test_account_switch_clears_results_and_preview(fixture):
    _, calls, _ = fixture
    app = classified(run(AppTest.from_string(SCRIPT)))
    run(app.button(key='sf_triage_preview_button').click())
    app.session_state['gmail_profile'] = {'email_address': 'different@example.invalid'}
    run(app)
    assert not any(button.key == 'sf_triage_apply' for button in app.button)
    assert not any(button.key == 'sf_triage_save' for button in app.button)
    assert not [call for call in calls if call[0] == 'write']


def test_hidden_selected_message_is_not_synced(fixture):
    root, calls, _ = fixture
    app = classified(run(AppTest.from_string(SCRIPT)))
    store = inbox_triage.InboxTriageStore(root / 'db.sqlite3')
    record = store.get(ACCOUNT, 'abc2')
    store.review(ACCOUNT, 'abc2', 'ignore', expected_fingerprint=record['fingerprint'])
    run(app.selectbox(key='sf_triage_filter').set_value('needs_reply'))
    assert app.multiselect(key='batch_selected').value == ['abc1']
    run(app.button(key='sf_triage_preview_button').click())
    run(checkbox(app, 'I have reviewed these label changes').check())
    run(app.button(key='sf_triage_apply').click())
    assert [call for call in calls if call[0] == 'write'] == [('write', 'abc1', 'needs_reply')]


def test_reply_and_task_handoff_reuse_current_message(fixture):
    _, calls, _ = fixture
    app = classified(run(AppTest.from_string(SCRIPT)), ['abc1'])
    run(app.button(key='sf_triage_reply').click())
    assert app.session_state['sf_reply_handoff']['mail_id'] == 'local-1'
    assert app.session_state['sf_app_pending_tab'] == 'Assistant'
    run(app.button(key='sf_triage_tasks').click())
    assert app.session_state['sf_inbox_workflow'] == 'Tasks & events'
    assert app.session_state['batch_selected'] == ['abc1']
    assert not app.session_state['batch_consent']
    assert not [call for call in calls if call[0] == 'write']


def test_stale_saved_category_blocks_remote_write(fixture):
    root, calls, _ = fixture
    app = classified(run(AppTest.from_string(SCRIPT)), ['abc1'])
    run(app.button(key='sf_triage_preview_button').click())
    run(checkbox(app, 'I have reviewed these label changes').check())
    store = inbox_triage.InboxTriageStore(root / 'db.sqlite3')
    record = store.get(ACCOUNT, 'abc1')
    store.review(ACCOUNT, 'abc1', 'needs_action', expected_fingerprint=record['fingerprint'])
    run(app.button(key='sf_triage_apply').click())
    assert not [call for call in calls if call[0] == 'write']


def test_mismatched_google_account_cannot_prepare_preview(fixture):
    _, calls, client = fixture
    app = classified(run(AppTest.from_string(SCRIPT)), ['abc1'])
    client.account_name = 'different@example.invalid'
    run(app.button(key='sf_triage_preview_button').click())
    assert any('same account' in error.value for error in app.error)
    assert not any(button.key == 'sf_triage_apply' for button in app.button)
    assert not [call for call in calls if call[0] in {'create', 'write'}]


def test_classifying_unclassified_view_keeps_new_results_visible(fixture):
    app = run(AppTest.from_string(SCRIPT))
    run(app.selectbox(key='sf_triage_filter').set_value('Unclassified'))
    classified(app, ['abc1'])
    assert app.selectbox(key='sf_triage_filter').value == 'All messages'
    assert not app.button(key='sf_triage_save').disabled
    assert app.selectbox(key='sf_triage_detail').value == 'abc1'


def test_failed_local_persistence_keeps_read_only_result(fixture, monkeypatch):
    _, calls, _ = fixture

    def fail_save(*args, **kwargs):
        raise sqlite3.OperationalError('Local storage unavailable')

    monkeypatch.setattr(inbox_triage.InboxTriageStore, 'save_suggestion', fail_save)
    app = classified(run(AppTest.from_string(SCRIPT)), ['abc1'])
    assert any('not saved' in warning.value for warning in app.warning)
    assert len(app.dataframe) == 1
    assert app.dataframe[0].value.iloc[0]['Category'] == 'Needs reply'
    assert not any(button.key in {'sf_triage_save', 'sf_triage_apply'} for button in app.button)
    assert not [call for call in calls if call[0] in {'create', 'write'}]


def _manual_category(app):
    return next(item for item in app.selectbox if item.label == 'Primary category')


def _manual_tags(app):
    return next(item for item in app.multiselect if item.label == 'Custom labels')


def test_explicit_manual_edit_saves_and_syncs_custom_labels(fixture):
    root, calls, client = fixture
    catalog_store = LabelCatalog(root / 'db.sqlite3')
    catalog = catalog_store.get(ACCOUNT)
    catalog = catalog_store.add(ACCOUNT, 'Urgent', expected_version=catalog['version'])
    urgent = catalog['custom_ids'][0]
    app = classified(run(AppTest.from_string(SCRIPT)), ['abc1'])
    run(_manual_category(app).set_value('needs_action'))
    run(_manual_tags(app).set_value([urgent]))
    assert app.button(key='sf_triage_preview_button').disabled
    run(app.button(key='sf_triage_manual_save').click())
    record = inbox_triage.InboxTriageStore(root / 'db.sqlite3').get(ACCOUNT, 'abc1')
    assert record['category'] == 'needs_action' and record['tag_ids'] == [urgent]
    assert not [call for call in calls if call[0] == 'write']
    run(app.button(key='sf_triage_preview_button').click())
    run(checkbox(app, 'I have reviewed these label changes').check())
    run(app.button(key='sf_triage_apply').click())
    assert ('named_write', 'abc1', ('StudentFlow/Needs action', 'StudentFlow/Urgent')) in calls
    assert client.messages['abc1'] >= {'INBOX', 'UNREAD', 'personal'}
    reopened = run(AppTest.from_string(SCRIPT))
    assert _manual_tags(reopened).value == [urgent]
    run(reopened.selectbox(key='sf_triage_filter').set_value(urgent))
    assert len(reopened.multiselect(key='batch_selected').options) == 1


def test_unsaved_manual_edit_clears_previous_approval(fixture):
    _, calls, _ = fixture
    app = classified(run(AppTest.from_string(SCRIPT)), ['abc1'])
    run(app.button(key='sf_triage_preview_button').click())
    run(checkbox(app, 'I have reviewed these label changes').check())
    run(_manual_category(app).set_value('ignore'))
    assert not any(button.key == 'sf_triage_apply' for button in app.button)
    assert app.button(key='sf_triage_preview_button').disabled
    assert not [call for call in calls if call[0] == 'write']


def test_rename_updates_display_and_invalidates_old_preview(fixture):
    root, calls, _ = fixture
    app = classified(run(AppTest.from_string(SCRIPT)), ['abc1'])
    run(app.button(key='sf_triage_preview_button').click())
    catalog_store = LabelCatalog(root / 'db.sqlite3')
    catalog_store.rename(ACCOUNT, 'needs_reply', 'Reply required', expected_version=catalog_store.get(ACCOUNT)['version'])
    run(app)
    assert not any(button.key == 'sf_triage_apply' for button in app.button)
    assert 'Reply required' in _manual_category(app).options
    run(app.button(key='sf_triage_preview_button').click())
    preview = app.session_state['sf_triage_preview']['preview']
    assert preview.items[0].add_labels == ('StudentFlow/Reply required',)
    assert not [call for call in calls if call[0] == 'write']


def test_remove_custom_tag_preserves_other_labels(fixture):
    root, _calls, client = fixture
    catalog_store = LabelCatalog(root / 'db.sqlite3')
    catalog = catalog_store.add(ACCOUNT, 'Coursework', expected_version=catalog_store.get(ACCOUNT)['version'])
    tag = catalog['custom_ids'][0]
    app = classified(run(AppTest.from_string(SCRIPT)), ['abc1'])
    run(_manual_tags(app).set_value([tag]))
    run(app.button(key='sf_triage_manual_save').click())
    run(app.button(key='sf_triage_preview_button').click())
    run(checkbox(app, 'I have reviewed these label changes').check())
    run(app.button(key='sf_triage_apply').click())
    extra = client.named_labels['StudentFlow/Coursework']
    assert extra in client.messages['abc1']
    run(_manual_tags(app).set_value([]))
    run(app.button(key='sf_triage_manual_save').click())
    run(app.button(key='sf_triage_preview_button').click())
    preview = app.session_state['sf_triage_preview']['preview']
    assert preview.items[0].remove_labels == ('StudentFlow/Coursework',)
    assert extra in client.messages['abc1']
    run(checkbox(app, 'I have reviewed these label changes').check())
    run(app.button(key='sf_triage_apply').click())
    assert client.messages['abc1'] == {'INBOX', 'UNREAD', 'personal', 'Label_needs_reply'}
