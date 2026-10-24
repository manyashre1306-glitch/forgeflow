"""Shared session state helpers.

Streamlit reruns the whole script on every interaction, so state lives in
``st.session_state``. This module keeps that state in one place and makes the
secret-handling rule explicit: keys and tokens are stored in the session only,
never written anywhere, and cleared on demand.

The functions here are thin wrappers so the pages read clearly and the tests can
import the state helpers without importing Streamlit.
"""

from __future__ import annotations

from web.limits import SessionLimits

_KEY = "_forgeflow_session"


def new_limits() -> SessionLimits:
    """A fresh set of per-session caps."""
    return SessionLimits()


def get_limits(state) -> SessionLimits:
    """Return the session's limits, creating them on first use."""
    if _KEY not in state:
        state[_KEY] = new_limits()
    return state[_KEY]


def reset_limits(state) -> None:
    """Replace the session's limits, e.g. after a reload."""
    state[_KEY] = new_limits()


def clear_secrets(state) -> None:
    """Remove every stored secret from the session.

    Keys and tokens live under a small, known set of names so this can clear
    them without touching anything else the page stored.
    """
    for name in (
        "github_token",
        "gemini_key",
        "publish_confirmation",
    ):
        state.pop(name, None)
