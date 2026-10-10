"""AI settings persist locally and contact OpenAI only after an explicit check."""

import os

import dotenv
import pytest
from streamlit.testing.v1 import AppTest

from studentflow import ai_settings


FAKE_KEY = "sk-test-not-a-real-key-12345"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)


def test_read_defaults_and_environment_fallback(tmp_path, monkeypatch):
    assert ai_settings.read_ai_settings(tmp_path) == ("", ai_settings.DEFAULT_MODEL)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    monkeypatch.setenv("OPENAI_MODEL", "environment-model")
    assert ai_settings.read_ai_settings(tmp_path) == (FAKE_KEY, "environment-model")
    assert not (tmp_path / ".env").exists()


def test_save_preserves_other_settings_and_blank_key(tmp_path, monkeypatch):
    original = "# Keep this comment\nSTUDENTFLOW_DATA_DIR='custom records'\nOTHER='${EXAMPLE}'\n"
    (tmp_path / ".env").write_text(original, encoding="utf-8")
    ai_settings.save_ai_settings(tmp_path, FAKE_KEY, "gpt-5.4-mini")
    assert (tmp_path / ".env").read_text(encoding="utf-8").startswith(original)
    ai_settings.save_ai_settings(tmp_path, "", "gpt-5.4-mini-2026-03-17")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-an-older-environment-key")
    assert ai_settings.read_ai_settings(tmp_path) == (FAKE_KEY, "gpt-5.4-mini-2026-03-17")
    assert dotenv.dotenv_values(tmp_path / ".env", interpolate=False)["OTHER"] == "${EXAMPLE}"
    assert list(tmp_path.glob(".env.*.tmp")) == []


@pytest.mark.parametrize("key,model", [
    ("", "gpt-5.4-mini"),
    ("not-a-key", "gpt-5.4-mini"),
    (FAKE_KEY + "\nINJECT=value", "gpt-5.4-mini"),
    (FAKE_KEY, "gpt-5.4-mini\nINJECT=value"),
    (FAKE_KEY, "invalid model"),
])
def test_invalid_inputs_leave_saved_file_unchanged(tmp_path, key, model):
    original = "OTHER=keep\n"
    path = tmp_path / ".env"
    path.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError) as error:
        ai_settings.save_ai_settings(tmp_path, key, model)
    assert FAKE_KEY not in str(error.value)
    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".env.*.tmp")) == []


def test_failed_save_is_atomic_and_sanitized(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("OTHER=keep\n", encoding="utf-8")

    def fail(*_args, **_kwargs):
        raise PermissionError(FAKE_KEY)

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(ValueError, match="could not be saved") as error:
        ai_settings.save_ai_settings(tmp_path, FAKE_KEY, "gpt-5.4-mini")
    assert FAKE_KEY not in str(error.value)
    assert path.read_text(encoding="utf-8") == "OTHER=keep\n"
    assert list(tmp_path.glob(".env.*.tmp")) == []


def settings_app(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDENTFLOW_TEST_SETTINGS_ROOT", str(tmp_path))
    app = AppTest.from_string("""
import os
from pathlib import Path
from studentflow.ai_settings import render_ai_settings
render_ai_settings(Path(os.environ['STUDENTFLOW_TEST_SETTINGS_ROOT']))
""")
    return app


def run(app):
    app.run(timeout=30)
    assert not app.exception


def button(app, label):
    return next(item for item in app.button if item.label == label)


def test_settings_ui_save_reopen_and_explicit_connection_test(tmp_path, monkeypatch):
    calls = []

    def fake_extract(**kwargs):
        calls.append(kwargs)
        return {"tasks": [{"source_quote": kwargs["body"]}], "warnings": []}

    monkeypatch.setattr(ai_settings, "extract_tasks", fake_extract)
    app = settings_app(tmp_path, monkeypatch)
    run(app)
    assert calls == []
    assert app.text_input(key="sf_ai_api_key_input").value == ""
    assert button(app, "Test AI connection").disabled
    app.text_input(key="sf_ai_api_key_input").input(FAKE_KEY)
    button(app, "Save AI settings").click()
    run(app)
    assert calls == []
    assert ai_settings.read_ai_settings(tmp_path) == (FAKE_KEY, "gpt-5.4-mini")
    assert app.text_input(key="sf_ai_api_key_input").value == ""
    assert not button(app, "Test AI connection").disabled
    assert not app.warning
    app = settings_app(tmp_path, monkeypatch)
    run(app)
    assert calls == []
    assert app.text_input(key="sf_ai_api_key_input").value == ""
    button(app, "Test AI connection").click()
    run(app)
    assert len(calls) == 1
    assert calls[0]["api_key"] == FAKE_KEY
    assert calls[0]["subject"] == "Connection check"
    assert "AI connection successful" in app.success[0].value
    run(app)
    assert len(calls) == 1


def test_connection_error_is_sanitized(tmp_path, monkeypatch):
    ai_settings.save_ai_settings(tmp_path, FAKE_KEY, "gpt-5.4-mini")

    def fail(**_kwargs):
        raise RuntimeError("Sensitive provider data: " + FAKE_KEY)

    monkeypatch.setattr(ai_settings, "extract_tasks", fail)
    app = settings_app(tmp_path, monkeypatch)
    run(app)
    button(app, "Test AI connection").click()
    run(app)
    assert "could not be verified" in app.error[0].value
    assert FAKE_KEY not in app.error[0].value
