"""Local OpenAI settings and an explicitly requested connection check."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import dotenv
import streamlit as st

from studentflow.extraction import extract_tasks

DEFAULT_MODEL = "gpt-5.4-mini"
_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{10,}")
_MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")


def read_ai_settings(root: Path) -> tuple[str, str]:
    """Prefer saved local settings, with process variables as a fallback."""
    try:
        values = dotenv.dotenv_values(Path(root) / ".env", interpolate=False)
    except (OSError, UnicodeError):
        raise ValueError("AI settings could not be read. Check access to the app folder.") from None
    api_key = (values.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
    model = (
        values.get("OPENAI_MODEL") or os.getenv("OPENAI_MODEL") or DEFAULT_MODEL
    ).strip() or DEFAULT_MODEL
    return api_key, model


def save_ai_settings(root: Path, api_key: str, model: str) -> None:
    """Replace only AI settings atomically; a blank key preserves the saved key."""
    api_key = api_key.strip()
    model = model.strip() or DEFAULT_MODEL
    if api_key and not _KEY_PATTERN.fullmatch(api_key):
        raise ValueError("Enter a valid OpenAI API key beginning with sk-.")
    if not _MODEL_PATTERN.fullmatch(model):
        raise ValueError("Enter a valid model ID, such as gpt-5.4-mini.")
    root = Path(root)
    saved_key, _ = read_ai_settings(root)
    if not api_key and not saved_key:
        raise ValueError("Enter an OpenAI API key to enable AI extraction.")
    path = root / ".env"
    temporary = None
    try:
        # Keep comments and unrelated settings intact. The temporary file shares
        # the .env.* ignore rule and is replaced only after all updates succeed.
        original = path.read_text(encoding="utf-8") if path.exists() else ""
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=root, prefix=".env.", suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(original)
        if api_key:
            dotenv.set_key(temporary, "OPENAI_API_KEY", api_key, quote_mode="always")
        dotenv.set_key(temporary, "OPENAI_MODEL", model, quote_mode="always")
        os.replace(temporary, path)
    except (OSError, UnicodeError):
        raise ValueError("AI settings could not be saved. Check access to the app folder.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def check_ai_connection(api_key: str, model: str) -> bool:
    """Exercise the same extraction path with a short, built-in sample."""
    body = "Please review the draft for 15 minutes by October 15, 2030 at 17:00 Singapore time."
    result = extract_tasks(
        body=body,
        subject="Connection check",
        sent_at="2030-10-10T09:00:00+08:00",
        api_key=api_key,
        model=model,
    )
    return any(task.get("source_quote") for task in result.get("tasks", []))


def render_ai_settings(root: Path) -> None:
    """Render settings without ever sending the saved key to a form field."""
    st.subheader("AI settings")
    st.caption("Connect OpenAI to extract tasks from your email.")
    try:
        api_key, model = read_ai_settings(root)
    except ValueError as error:
        st.error(str(error))
        return
    if st.session_state.pop("sf_ai_settings_saved", False):
        st.session_state["sf_ai_api_key_input"] = ""
        st.session_state["sf_ai_model_input"] = model
        st.success("AI settings saved on this device.")
    if api_key:
        st.info(f"API key saved. Current model: {model}")
    else:
        st.info("Add your OpenAI API key to enable AI extraction.")
    st.markdown(
        "[OpenAI billing](https://platform.openai.com/settings/organization/billing/overview)"
        " · [Create an API key](https://platform.openai.com/api-keys)"
        " · [Setup guide](https://github.com/yihua127/studentflow/blob/main/docs/OPENAI_SETUP.md)"
    )
    st.session_state.setdefault("sf_ai_model_input", model)
    with st.form("sf_ai_settings_form", clear_on_submit=True):
        entered_key = st.text_input(
            "OpenAI API key", type="password", key="sf_ai_api_key_input",
            help="Saved on this device. Leave blank to keep your current key.",
        )
        entered_model = st.text_input(
            "Model ID", key="sf_ai_model_input",
            help="Choose a model with structured output support, such as gpt-5.4-mini.",
        )
        submitted = st.form_submit_button("Save AI settings", type="primary")
    if submitted:
        try:
            save_ai_settings(root, entered_key, entered_model)
        except ValueError as error:
            st.error(str(error))
        else:
            st.session_state.sf_ai_settings_saved = True
            st.rerun()
    st.caption(
        "Test AI connection sends a short built-in example to OpenAI. "
        "Standard API usage charges apply."
    )
    if st.button("Test AI connection", disabled=not api_key, key="sf_ai_test_connection"):
        with st.spinner("Checking AI connection..."):
            try:
                valid = check_ai_connection(api_key, model)
            except Exception:  # noqa: BLE001 - Never expose provider request data or credentials.
                # Provider exceptions can contain request data or credentials.
                st.error(
                    "The AI connection could not be verified. Check your API key, "
                    "API balance, internet connection, and model access."
                )
            else:
                if valid:
                    st.success("AI connection successful. Task extraction is ready.")
                else:
                    st.warning("The model responded. Try the check again to verify task extraction.")
