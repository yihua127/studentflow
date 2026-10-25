"""Persistent reply preferences never contain run consent or mix Gmail accounts."""

import json

import pytest

from studentflow.reply_preferences import (
    clear_reply_preferences,
    read_reply_preferences,
    reply_preferences_path,
    save_reply_preferences,
)


def test_roundtrip_retains_exact_name_and_permissions_without_run_consent(tmp_path):
    sample = "Thank you.\n\nBest regards,\nALEX TAN"
    save_reply_preferences(tmp_path, " Student@Example.invalid ", style=sample,
                           request="Write an English reply.", style_approved=True)
    result = read_reply_preferences(tmp_path, "student@example.invalid")
    assert result == {
        "version": 1, "account": "student@example.invalid", "style": sample,
        "request": "Write an English reply.", "style_approved": True,
    }
    assert not list((tmp_path / "reply_preferences").glob("*.tmp"))


def test_account_and_disconnected_preferences_are_isolated_and_clear_is_scoped(tmp_path):
    for account in ("first@example.invalid", "second@example.invalid", ""):
        save_reply_preferences(tmp_path, account, style="", request=account, style_approved=False)
    assert len(list((tmp_path / "reply_preferences").glob("*.json"))) == 3
    clear_reply_preferences(tmp_path, "first@example.invalid")
    assert read_reply_preferences(tmp_path, "first@example.invalid") is None
    assert read_reply_preferences(tmp_path, "second@example.invalid")["request"] == "second@example.invalid"
    assert read_reply_preferences(tmp_path, "")["request"] == ""
    clear_reply_preferences(tmp_path, "first@example.invalid")


@pytest.mark.parametrize("raw", ["not json", "[]", '{"version": 99}', "\ufffd", "x" * 100_001],
                         ids=["not-json", "list", "version", "bad-content", "oversized"])
def test_corrupt_preferences_survive_read_and_save_until_explicitly_cleared(tmp_path, raw):
    path = reply_preferences_path(tmp_path, "student@example.invalid")
    path.parent.mkdir()
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError):
        read_reply_preferences(tmp_path, "student@example.invalid")
    with pytest.raises(ValueError):
        save_reply_preferences(tmp_path, "student@example.invalid", style="", request="new", style_approved=False)
    assert path.read_text(encoding="utf-8") == raw
    clear_reply_preferences(tmp_path, "student@example.invalid")
    assert not path.exists()


@pytest.mark.parametrize("change", [
    {"style": "x" * 6001}, {"request": "x" * 4001}, {"style_approved": "yes"},
    {"style": "bad\x00text"}, {"request": 123},
])
def test_invalid_preference_update_does_not_replace_valid_file(tmp_path, change):
    args = {"style": "Best, Yihua", "request": "Draft a reply.", "style_approved": True}
    save_reply_preferences(tmp_path, "student@example.invalid", **args)
    path = reply_preferences_path(tmp_path, "student@example.invalid")
    original = path.read_bytes()
    with pytest.raises(ValueError):
        save_reply_preferences(tmp_path, "student@example.invalid", **(args | change))
    assert path.read_bytes() == original


def test_copied_profile_from_other_account_is_rejected(tmp_path):
    save_reply_preferences(tmp_path, "first@example.invalid", style="", request="first", style_approved=False)
    second = reply_preferences_path(tmp_path, "second@example.invalid")
    second.write_bytes(reply_preferences_path(tmp_path, "first@example.invalid").read_bytes())
    with pytest.raises(ValueError, match="different account"):
        read_reply_preferences(tmp_path, "second@example.invalid")


def test_atomic_write_failure_preserves_saved_preferences_and_cleans_temporary(tmp_path, monkeypatch):
    from studentflow import reply_preferences

    save_reply_preferences(tmp_path, "", style="", request="previous", style_approved=False)

    def fail(*args):
        raise OSError("Synthetic storage failure")

    monkeypatch.setattr(reply_preferences.os, "replace", fail)
    with pytest.raises(OSError):
        save_reply_preferences(tmp_path, "", style="", request="replacement", style_approved=False)
    assert read_reply_preferences(tmp_path, "")["request"] == "previous"
    assert not list((tmp_path / "reply_preferences").glob("*.tmp"))


def test_approval_is_not_saved_for_empty_style_and_unknown_fields_are_rejected(tmp_path):
    save_reply_preferences(tmp_path, "", style=" ", request="", style_approved=True)
    assert read_reply_preferences(tmp_path, "")["style_approved"] is False
    path = reply_preferences_path(tmp_path, "")
    value = json.loads(path.read_text(encoding="utf-8"))
    value["send_consent"] = True
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        read_reply_preferences(tmp_path, "")
