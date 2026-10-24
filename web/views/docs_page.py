"""Docs: the project's own documentation, in the browser.

Content is read from the repository's Markdown files. Each file is shown inside a
fenced code block so its Markdown is not interpreted, keeping the rendering rule
the same as everywhere else: repository text is displayed, never executed.
"""

from __future__ import annotations

from pathlib import Path

from web import ui

#: Documents the page offers, as (label, path) relative to the repository root.
DOCUMENTS: tuple[tuple[str, str], ...] = (
    ("README", "README.md"),
    ("Specification", "docs/SPEC.md"),
    ("Setup", "docs/setup.md"),
    ("Architecture", "docs/architecture.md"),
    ("Threat model", "docs/threat-model.md"),
    ("Cost controls", "docs/cost-controls.md"),
    ("Evaluation", "docs/evaluation.md"),
    ("Demonstration", "docs/demonstration.md"),
    ("Contributing", "CONTRIBUTING.md"),
    ("Contribution ideas", "docs/contributing-ideas.md"),
    ("Deploy the website", "docs/deploy-website.md"),
)

MAX_DOC_BYTES = 200 * 1024


def render(mode) -> None:
    st = ui._st()
    ui.page_setup("Docs")
    ui.mode_banner(mode)
    ui.header("Docs", "The project's documentation, read from the repository.")

    labels = [label for label, _ in DOCUMENTS]
    choice = st.selectbox("Document", labels, key="docs_choice")
    relative = dict(DOCUMENTS)[choice]

    text = _read(relative)
    if text is None:
        st.warning(f"{relative} is not available in this deployment.")
        return

    if len(text.encode("utf-8")) > MAX_DOC_BYTES:
        st.warning("This document is larger than the display cap; showing the first part.")
        text = text.encode("utf-8")[:MAX_DOC_BYTES].decode("utf-8", errors="replace")

    st.caption(f"Source: {relative}")
    st.code(text, language="markdown")


def _read(relative: str) -> str | None:
    """Read a documentation file from the repository root, or None."""
    path = Path(__file__).resolve().parents[2] / relative
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
