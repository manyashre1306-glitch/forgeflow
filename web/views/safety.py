"""Safety: what ForgeFlow refuses to do, and why."""

from __future__ import annotations

from web import ui

RULES = [
    (
        "No publishing without a human",
        "Every external write needs explicit human approval. The approval binds "
        "to the SHA-256 of the exact diff bytes, so a patch that changes after "
        "approval needs a fresh one.",
    ),
    (
        "Unknown is not permission",
        "An absent AI-use statement is treated as no statement. Publishing is "
        "blocked unless a human records a documented override.",
    ),
    (
        "Separate publishing token",
        "Publishing uses FORGEFLOW_PUBLISH_TOKEN, a different credential from the "
        "read-only GITHUB_TOKEN. The two never cross: reads use the read client, "
        "writes use the write client.",
    ),
    (
        "No untrusted code on the host",
        "Repository code only runs inside a container. When no container runtime "
        "is available the sandbox refuses; there is no host fallback.",
    ),
    (
        "Prompt injection is fenced",
        "Every free-text field from a repository is an UntrustedText and is "
        "rendered only inside its data fence. Instructions and data travel in "
        "separate arguments.",
    ),
    (
        "Budget before every paid call",
        "The monthly cap is checked before every model call. No budget query "
        "means no call. The website adds a per-session cap on top.",
    ),
    (
        "Kill switch",
        "kill_switch: true in the config, or FORGEFLOW_KILL_SWITCH=1 in the "
        "environment, halts model calls and sandbox execution.",
    ),
    (
        "Draft pull requests only",
        "Publishing opens a draft PR. It never merges, never force-pushes, and "
        "never writes outside the repository allowlist.",
    ),
]

HOSTED_RULES = [
    "Hosted mode never reads the local database (data/forgeflow.db).",
    "Hosted mode never reads the .env file.",
    "Hosted mode never imports the publisher or the publishing token.",
    "Hosted mode never executes repository code.",
    "Hosted mode refuses validation and publishing even if a function is called directly.",
]


def render(mode) -> None:
    ui.page_setup("Safety")
    ui.mode_banner(mode)
    ui.header(
        "Safety",
        "The rules ForgeFlow enforces in code, not just in documentation.",
    )

    st = ui._st()
    st.markdown("**Core guarantees**")
    for title, detail in RULES:
        st.text(f"- {title}: {detail}")

    st.markdown("**What hosted mode refuses**")
    for rule in HOSTED_RULES:
        st.text(f"- {rule}")

    ui.disclaimer(
        "These are enforced by functions the tests call directly, so a page that "
        "forgot to check still cannot reach a hosted-forbidden action. See "
        "docs/threat-model.md for the full analysis."
    )
