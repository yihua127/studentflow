"""Local, account-scoped label names and extra tags; never changes Gmail."""

from __future__ import annotations

import hashlib
import sqlite3

import streamlit as st


def _scope(account: str) -> str:
    return hashlib.sha256(account.strip().casefold().encode()).hexdigest()[:20]


def _saved(prefix: str, message: str) -> None:
    # A renamed label changes the meaning of a previous Gmail preview.
    st.session_state.pop("sf_triage_preview", None)
    st.session_state.pop("sf_triage_sync_result", None)
    st.session_state[prefix + "notice"] = message
    st.rerun()


def render_label_manager(catalog_store, account: str) -> dict:
    """Return the current catalog and offer explicit local add/rename actions.

    Catalog writes bind to the version shown by the current form. Their changed
    version also gives later selection/review widgets a fresh identity.
    """
    account = account.strip().casefold()
    catalog = catalog_store.get(account)
    prefix = "sf_label_manager_" + _scope(account) + "_"
    version_prefix = prefix + str(catalog["version"]) + "_"
    names = catalog["names"]
    notice = st.session_state.pop(prefix + "notice", None)
    if notice:
        st.success(notice)
    with st.expander("Manage labels"):
        st.caption(
            "Rename the three category labels or add extra labels to choose manually. "
            "Renaming a category changes its name, not how AI classifies emails."
        )
        st.caption(
            "Names are saved on this device. Gmail changes only after you preview and confirm "
            "selected emails; other messages keep their existing Gmail labels until you sync them."
        )
        left, right = st.columns(2)
        with left:
            st.write("**Rename a label**")
            selected = st.selectbox(
                "Label to rename", list(names), format_func=names.get,
                key=version_prefix + "label",
            )
            with st.form(version_prefix + "rename_form_" + selected):
                name = st.text_input(
                    "Label name", value=names[selected], max_chars=60,
                    key=version_prefix + "rename_name_" + selected,
                )
                if st.form_submit_button("Save label name"):
                    try:
                        catalog_store.rename(
                            account, selected, name, expected_version=catalog["version"],
                        )
                    except ValueError as error:
                        st.error(str(error))
                    except (OSError, sqlite3.Error):
                        st.error("The label name could not be saved. Check local storage and try again.")
                    else:
                        _saved(prefix, "Label name saved. Review the updated Gmail labels before syncing.")
        with right:
            st.write("**Add an extra label**")
            with st.form(version_prefix + "add_form"):
                name = st.text_input(
                    "New label name", max_chars=60, placeholder="For example: Coursework or Urgent",
                    key=version_prefix + "new_name",
                )
                st.caption("Extra labels can be combined with a category and with one another.")
                if st.form_submit_button("Add label"):
                    try:
                        catalog_store.add(account, name, expected_version=catalog["version"])
                    except ValueError as error:
                        st.error(str(error))
                    except (OSError, sqlite3.Error):
                        st.error("The label could not be saved. Check local storage and try again.")
                    else:
                        _saved(prefix, "Extra label added. Choose it for the emails you want to tag.")
    return catalog
