"""Local label editing is explicit, account-scoped and invalidates Gmail review."""

import sqlite3

import pytest
from streamlit.testing.v1 import AppTest

from studentflow.email_label_catalog import LabelCatalog

ACCOUNT = "student@example.invalid"
OTHER = "other@example.invalid"
SCRIPT = '''
import os
from pathlib import Path
import streamlit as st
from studentflow.email_label_catalog import LabelCatalog
from studentflow.email_label_manager_ui import render_label_manager
st.session_state.setdefault('account', 'student@example.invalid')
catalog = render_label_manager(LabelCatalog(Path(os.environ['LABEL_MANAGER_DB'])), st.session_state['account'])
st.json(catalog)
'''


def run(app):
    app = app.run(timeout=30)
    assert not app.exception
    return app


def element(elements, label):
    return next(item for item in elements if item.label == label)


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    path = tmp_path / "catalog.sqlite3"
    monkeypatch.setenv("LABEL_MANAGER_DB", str(path))
    return LabelCatalog(path)


def test_render_does_not_add_or_rename_and_returns_catalog(catalog, monkeypatch):
    original = catalog.get(ACCOUNT)

    def forbidden(*args, **kwargs):
        raise AssertionError("Opening label management must not alter the catalog.")

    monkeypatch.setattr(LabelCatalog, "add", forbidden)
    monkeypatch.setattr(LabelCatalog, "rename", forbidden)
    app = run(AppTest.from_string(SCRIPT))
    assert element(app.expander, "Manage labels")
    assert catalog.get(ACCOUNT) == original


def test_add_label_then_reopen_and_other_account_isolated(catalog):
    app = run(AppTest.from_string(SCRIPT))
    element(app.text_input, "New label name").set_value("Coursework")
    app.session_state["sf_triage_preview"] = {"old": True}
    run(element(app.button, "Add label").click())
    updated = catalog.get(ACCOUNT)
    assert len(updated["custom_ids"]) == 1
    key = updated["custom_ids"][0]
    assert updated["names"][key] == "Coursework"
    assert "sf_triage_preview" not in app.session_state
    reopened = run(AppTest.from_string(SCRIPT))
    assert "Coursework" in element(reopened.selectbox, "Label to rename").options
    reopened.session_state["account"] = OTHER
    run(reopened)
    assert "Coursework" not in element(reopened.selectbox, "Label to rename").options
    assert not catalog.get(OTHER)["custom_ids"]


def test_rename_category_keeps_key_and_clears_old_preview(catalog):
    app = run(AppTest.from_string(SCRIPT))
    original = catalog.get(ACCOUNT)
    element(app.text_input, "Label name").set_value("Please reply")
    app.session_state["sf_triage_preview"] = {"confirmed": True}
    app.session_state["sf_triage_sync_result"] = {"status": "synced"}
    run(element(app.button, "Save label name").click())
    updated = catalog.get(ACCOUNT)
    assert updated["names"]["needs_reply"] == "Please reply"
    assert set(updated["names"]) == set(original["names"])
    assert updated["version"] != original["version"]
    assert "sf_triage_preview" not in app.session_state
    assert "sf_triage_sync_result" not in app.session_state


@pytest.mark.parametrize("name", ["", "Needs reply", "needs REPLY", "Wrong/name"])
def test_invalid_or_duplicate_custom_name_does_not_change_catalog(catalog, name):
    app = run(AppTest.from_string(SCRIPT))
    original = catalog.get(ACCOUNT)
    element(app.text_input, "New label name").set_value(name)
    run(element(app.button, "Add label").click())
    assert app.error
    assert catalog.get(ACCOUNT) == original


def test_renaming_selected_extra_label_changes_only_that_label(catalog):
    initial = catalog.get(ACCOUNT)
    catalog.add(ACCOUNT, "Coursework", expected_version=initial["version"])
    value = catalog.get(ACCOUNT)
    key = value["custom_ids"][0]
    app = run(AppTest.from_string(SCRIPT))
    run(element(app.selectbox, "Label to rename").set_value(key))
    assert element(app.text_input, "Label name").value == "Coursework"
    element(app.text_input, "Label name").set_value("DSS5105")
    run(element(app.button, "Save label name").click())
    updated = catalog.get(ACCOUNT)
    assert updated["names"][key] == "DSS5105"
    assert updated["names"]["needs_reply"] == "Needs reply"


def test_storage_failure_is_visible_and_does_not_clear_preview(catalog, monkeypatch):
    app = run(AppTest.from_string(SCRIPT))
    original = catalog.get(ACCOUNT)

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("details must not appear")

    monkeypatch.setattr(LabelCatalog, "add", fail)
    element(app.text_input, "New label name").set_value("Urgent")
    app.session_state["sf_triage_preview"] = {"old": True}
    run(element(app.button, "Add label").click())
    assert any("could not be saved" in error.value for error in app.error)
    assert all("details must not appear" not in error.value for error in app.error)
    assert catalog.get(ACCOUNT) == original
    assert "sf_triage_preview" in app.session_state
