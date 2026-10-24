"""Shared Streamlit widgets and renderers.

This is the only module in ``web/`` that imports Streamlit, and it imports it at
call time rather than at module import, so the service layer and the tests never
need Streamlit installed. Pages import this module and call these helpers.

Rendering rules kept in one place:

* Every repository-sourced string is shown inside ``st.text`` or a fenced code
  block, never ``st.markdown``/``st.write`` with ``unsafe_allow_html``. A
  repository that puts ``<script>`` in its README therefore renders as text.
* Secrets are entered through ``st.text_input(type="password")`` and are never
  rendered back.
"""

from __future__ import annotations

from collections.abc import Iterable


def _st():
    import streamlit as st

    return st


def page_setup(title: str, *, layout: str = "wide") -> None:
    st = _st()
    st.set_page_config(page_title=f"ForgeFlow - {title}", page_icon="ForgeFlow", layout=layout)


def header(title: str, subtitle: str) -> None:
    st = _st()
    st.title(title)
    st.caption(subtitle)


def mode_banner(mode) -> None:
    """Show the active mode on every page, so it is never ambiguous."""
    st = _st()
    (st.info if mode.is_hosted else st.warning)(mode.banner)


def render_text(label: str, value: str, *, caption: str | None = None) -> None:
    """Render an untrusted string as plain text, never as HTML."""
    st = _st()
    st.markdown(f"**{label}**")
    if caption:
        st.caption(caption)
    st.text(value)


def render_list(label: str, items: Iterable[str], *, empty: str = "(none)") -> None:
    st = _st()
    st.markdown(f"**{label}**")
    materialised = [str(item) for item in items]
    if not materialised:
        st.caption(empty)
        return
    for item in materialised:
        st.text(f"- {item}")


def render_fenced(label: str, value: str, *, language: str = "text") -> None:
    """Render a diff or code blob inside a fenced code block (safe by default)."""
    st = _st()
    st.markdown(f"**{label}**")
    st.code(value, language=language)


def secret_input(label: str, *, key: str, help_text: str = "") -> str:
    """A password field that returns the value but never renders it back."""
    st = _st()
    return st.text_input(label, type="password", key=key, help=help_text or None)


def show_error(message: str, *, detail: str | None = None) -> None:
    st = _st()
    st.error(message)
    if detail:
        st.caption(detail)


def show_success(message: str) -> None:
    _st().success(message)


def metrics_row(pairs: Iterable[tuple[str, str]]) -> None:
    st = _st()
    materialised = list(pairs)
    if not materialised:
        return
    columns = st.columns(len(materialised))
    for column, (label, value) in zip(columns, materialised, strict=False):
        column.metric(label, value)


def data_table(rows: list[dict[str, object]], *, empty: str = "No rows.") -> None:
    st = _st()
    if not rows:
        st.caption(empty)
        return
    st.dataframe(rows, use_container_width=True, hide_index=True)


def disclaimer(text: str) -> None:
    _st().caption(text)
