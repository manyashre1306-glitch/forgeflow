"""Contribute: how to help, and the rules that keep the project honest."""

from __future__ import annotations

from web import ui

STEPS = [
    "Read CONTRIBUTING.md and the specification.",
    "Pick something from docs/contributing-ideas.md, or open an issue first.",
    "Run the gates locally: ruff check, ruff format --check, pytest.",
    "Open a pull request with a plain-language description of what changed and why.",
]

RULES = [
    "Never fabricate an API, a test result, a benchmark, or a pull request.",
    "Never write a secret into a file.",
    "Keep the core install and CI working with a plain 'uv sync --frozen'.",
    "Keep every repository free-text field inside its untrusted-data fence.",
    "Keep publishing behind the approval boundary and the separate token.",
]


def render(mode) -> None:
    st = ui._st()
    ui.page_setup("Contribute")
    ui.mode_banner(mode)
    ui.header("Contribute", "How to contribute to ForgeFlow itself.")

    st.markdown("**Getting started**")
    for index, step in enumerate(STEPS, start=1):
        st.text(f"{index}. {step}")

    st.markdown("**Rules that keep the project honest**")
    for rule in RULES:
        st.text(f"- {rule}")

    st.markdown("**Local development**")
    st.code(
        "uv sync\n"
        "uv run python -m ruff check .\n"
        "uv run python -m ruff format --check .\n"
        "uv run python -m pytest -q\n"
        "uv run --extra web streamlit run web/app.py",
        language="bash",
    )
    ui.disclaimer("The website is an optional extra. The core install and CI do not need it.")
