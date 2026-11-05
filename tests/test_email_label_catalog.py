"""Local label naming is isolated by account and retains migration history."""

import sqlite3
from copy import deepcopy

import pytest

from studentflow.email_label_catalog import (
    DEFAULT_NAMES,
    LabelCatalog,
    default_catalog,
    gmail_label_names,
    validate_catalog,
)

ACCOUNT = "student@example.test"


def test_defaults_do_not_create_account_row_and_get_returns_independent_data(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = LabelCatalog(path)
    first = store.get(" Student@Example.Test ")
    assert first == default_catalog(ACCOUNT)
    assert first["names"] == DEFAULT_NAMES and first["custom_ids"] == []
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM email_label_catalog").fetchone()[0] == 0
    first["names"]["ignore"] = "changed"
    assert store.get(ACCOUNT)["names"] == DEFAULT_NAMES


def test_add_rename_and_reopen_keep_custom_id_and_account_isolation(tmp_path):
    store = LabelCatalog(tmp_path / "state.sqlite3")
    first = store.get(ACCOUNT)
    added = store.add(ACCOUNT, "  课程资料  ", expected_version=first["version"])
    tag = added["custom_ids"][0]
    assert tag.startswith("tag_") and len(tag) == 36
    assert added["names"][tag] == "课程资料"
    renamed = store.rename(ACCOUNT, tag, "项目资料", expected_version=added["version"])
    assert renamed["custom_ids"] == [tag]
    assert renamed["aliases"][tag] == ["课程资料"]
    assert LabelCatalog(store.path).get(ACCOUNT) == renamed
    assert store.get("other@example.test") == default_catalog("other@example.test")


def test_primary_rename_and_revert_preserve_aliases_without_changing_category(tmp_path):
    store = LabelCatalog(tmp_path / "state.sqlite3")
    first = store.get(ACCOUNT)
    renamed = store.rename(ACCOUNT, "needs_reply", "Reply later", expected_version=first["version"])
    current, allowed = gmail_label_names(renamed)
    assert current["needs_reply"] == "StudentFlow/Reply later"
    assert {"StudentFlow/Needs reply", "StudentFlow/Reply later"} <= allowed
    reverted = store.rename(ACCOUNT, "needs_reply", "Needs reply", expected_version=renamed["version"])
    assert reverted["aliases"]["needs_reply"] == ["Reply later"]
    assert reverted["version"] != first["version"]


@pytest.mark.parametrize("name", ["", "   ", "a" * 61, "a/b", "a\\b", "\nvalid",
                                 "valid\t", "a\x00b", "a\u200bb", "ALL MESSAGES", "Unclassified"])
def test_invalid_label_names_leave_catalog_unchanged(tmp_path, name):
    store = LabelCatalog(tmp_path / "state.sqlite3")
    first = store.get(ACCOUNT)
    with pytest.raises(ValueError):
        store.add(ACCOUNT, name, expected_version=first["version"])
    assert store.get(ACCOUNT) == first


def test_duplicate_current_and_historical_names_are_reserved_for_logical_label(tmp_path):
    store = LabelCatalog(tmp_path / "state.sqlite3")
    first = store.get(ACCOUNT)
    renamed = store.rename(ACCOUNT, "needs_reply", "Reply later", expected_version=first["version"])
    for name in ["reply LATER", "needs REPLY", "needs action"]:
        with pytest.raises(ValueError, match="already used"):
            store.add(ACCOUNT, name, expected_version=renamed["version"])
    with pytest.raises(ValueError, match="capitalization"):
        store.rename(ACCOUNT, "needs_reply", "REPLY LATER", expected_version=renamed["version"])
    assert store.get(ACCOUNT) == renamed


def test_concurrent_edit_rejected_and_noop_keeps_version(tmp_path):
    store = LabelCatalog(tmp_path / "state.sqlite3")
    before = store.get(ACCOUNT)
    after = store.add(ACCOUNT, "Project", expected_version=before["version"])
    with pytest.raises(ValueError, match="another window"):
        store.rename(ACCOUNT, "needs_reply", "Respond", expected_version=before["version"])
    assert store.rename(ACCOUNT, "needs_reply", "Needs reply", expected_version=after["version"]) == after


@pytest.mark.parametrize("custom", [False, True])
def test_historical_name_case_variant_rejected_but_exact_revert_allowed(tmp_path, custom):
    store = LabelCatalog(tmp_path / "state.sqlite3")
    original = store.get(ACCOUNT)
    key = "needs_reply"
    if custom:
        original = store.add(ACCOUNT, "Project", expected_version=original["version"])
        key = original["custom_ids"][0]
    old_name = original["names"][key]
    renamed = store.rename(ACCOUNT, key, "Review later", expected_version=original["version"])
    with pytest.raises(ValueError, match="capitalization"):
        store.rename(ACCOUNT, key, old_name.upper(), expected_version=renamed["version"])
    assert store.get(ACCOUNT) == renamed
    reverted = store.rename(ACCOUNT, key, old_name, expected_version=renamed["version"])
    assert reverted["names"][key] == old_name
    assert reverted["aliases"][key] == ["Review later"]


def test_forged_account_or_name_catalog_rejected():
    first = default_catalog(ACCOUNT)
    changed = deepcopy(first)
    changed["aliases"]["ignore"] = ["Unrelated"]
    with pytest.raises(ValueError, match="changed"):
        validate_catalog(changed)
    with pytest.raises(ValueError, match="different Gmail account"):
        validate_catalog(first, account="other@example.test")


def test_corrupt_catalog_is_not_silently_replaced(tmp_path):
    store = LabelCatalog(tmp_path / "state.sqlite3")
    with sqlite3.connect(store.path) as db:
        db.execute("INSERT INTO email_label_catalog VALUES (?, ?)", (ACCOUNT, "bad JSON"))
    with pytest.raises(ValueError, match="Existing settings have been kept"):
        store.get(ACCOUNT)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT payload FROM email_label_catalog").fetchone()[0] == "bad JSON"
