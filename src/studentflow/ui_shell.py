"""Shared visual theme and native, keyboard-accessible workspace navigation."""

from __future__ import annotations

from base64 import b64encode
from html import escape
from pathlib import Path

import streamlit as st

_BRAND_IMAGE = Path(__file__).resolve().parents[2] / "assets" / "sidebar-usagi.jpg"

PAGES = (
    "Overview", "Inbox (Gmail)", "Tasks", "Study Plan", "Discussions",
    "Assistant", "Attachments", "Settings",
)
DEFAULT_PAGE = "Inbox (Gmail)"
NAVIGATION_KEY = "sf_app_tab"
PENDING_NAVIGATION_KEY = "sf_app_pending_tab"

# These values also remain the public routing contract for reply/task handoffs.
_LABELS = {
    "Overview": ":material/grid_view: Overview",
    "Inbox (Gmail)": ":material/inbox: Inbox",
    "Tasks": ":material/check_circle: Tasks",
    "Study Plan": ":material/calendar_month: Study plan",
    "Discussions": ":material/forum: Discussions",
    "Assistant": ":material/auto_awesome: Assistant",
    "Attachments": ":material/folder_open: Attachments",
    "Settings": ":material/settings: Settings",
}
_LEGACY_TABS = ("Inbox (Gmail)", "Tasks", "Study Plan", "Attachments", "Settings", "Assistant")
_ALIASES = {"Inbox": "Inbox (Gmail)", "Study plan": "Study Plan"}


def _canonical_page(value: object) -> str | None:
    """Accept saved labels and old positional tabs without trusting stale state."""
    if isinstance(value, str):
        page = _ALIASES.get(value, value)
        return page if page in PAGES else None
    if type(value) is int and 0 <= value < len(_LEGACY_TABS):
        return _LEGACY_TABS[value]
    return None


def apply_theme(root: Path | str) -> None:
    """Load bundled CSS after ``set_page_config`` without executing JavaScript.

    Streamlit's native color tokens live in ``.streamlit/config.toml``. This
    stylesheet adds the layout and component treatment shared by every page.
    """
    stylesheet = Path(root) / "assets" / "workspace.css"
    if stylesheet.is_file():
        # st.html recognizes a CSS file and inserts it without a visible spacer.
        st.html(stylesheet)


def render_gmail_loader(status: str, elapsed: float = 0) -> None:
    """Render the React Bits 3×3 orbit in Streamlit's HTML layer."""
    orbit = (0, 1, 2, 7, None, 3, 6, 5, 4)
    marks = {"done": {2, 3, 5, 7}, "error": {0, 2, 4, 6, 8}}
    labels = {"working": "Loading", "done": "Successful!", "error": "Connection failed"}
    cells = []
    for index, delay in enumerate(orbit):
        if status == "working" and delay is not None:
            cells.append(f'<i class="sf-lattice-lit" style="animation-delay:{delay * 108}ms"></i>')
        else:
            active = ' class="sf-lattice-mark"' if index in marks.get(status, ()) else ""
            cells.append(f"<i{active}></i>")
    st.html(
        f'<div class="sf-gmail-loader" data-status="{status}" role="status">'
        f'<span class="sf-lattice-grid" aria-hidden="true">{"".join(cells)}</span>'
        f'<strong>{labels[status]}</strong>'
        f'<span class="sf-lattice-time" aria-hidden="true">{elapsed:.1f}s</span></div>'
    )
    if status == "working":
        st.html(_BRAND_IMAGE.parent / "welcome" / "loader-clock.html", unsafe_allow_javascript=True)


def render_navigation() -> str:
    """Render one sidebar selector and return its canonical page name.

    Process handoffs before constructing the keyed widget: changing its value
    after rendering would raise Streamlit's widget-state mutation exception.
    Invalid pending routes are discarded while the current valid page survives.
    """
    pending = _canonical_page(st.session_state.pop(PENDING_NAVIGATION_KEY, None))
    current = _canonical_page(st.session_state.get(NAVIGATION_KEY))
    selected = pending or current or DEFAULT_PAGE
    if st.session_state.get(NAVIGATION_KEY) != selected:
        st.session_state[NAVIGATION_KEY] = selected

    brand_image = b64encode(_BRAND_IMAGE.read_bytes()).decode("ascii")
    with st.sidebar:
        st.html(
            '<div class="sf-brand">'
            '<div class="sf-brand-icon" aria-hidden="true">'
            f'<img src="data:image/jpeg;base64,{brand_image}" alt="">'
            '<div class="sf-brand-mark">s<span>f</span></div></div>'
            '<div><div class="sf-brand-name">StudentFlow</div>'
            '<div class="sf-brand-caption">A little more clarity.</div></div>'
            '</div>'
        )
        page = st.radio(
            "Workspace navigation", PAGES,
            index=None,
            format_func=_LABELS.__getitem__,
            key=NAVIGATION_KEY,
            label_visibility="collapsed",
            width="stretch",
        )
        profile = st.session_state.get("gmail_profile") or {}
        account = profile.get("email_address", "") if isinstance(profile, dict) else ""
        footer_label = escape(str(account)) if account else "Your personal workspace"
        footer_detail = "Gmail connected" if account else "Make room for what matters."
        st.html(
            '<div class="sf-sidebar-footer">'
            '<span class="sf-workspace-avatar" aria-hidden="true">'
            + (escape(str(account)[0].upper()) if account else "S")
            + '</span><div class="sf-sidebar-account"><strong>' + footer_label
            + '</strong><span>' + footer_detail + '</span></div></div>'
        )
    return page or DEFAULT_PAGE
