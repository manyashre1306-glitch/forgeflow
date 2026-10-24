"""Home page: what ForgeFlow is, and what this website does."""

from __future__ import annotations

from web import ui

INTRO = (
    "ForgeFlow finds, ranks, analyses, and prepares open-source contributions, "
    "then stops and waits for a human before anything is published."
)

PIPELINE = [
    ("Discover", "Search an allowlist of repositories for open issues."),
    ("Filter and dedupe", "Drop pull requests, closed, assigned, and duplicate items."),
    ("Rank", "Score issues with six weighted factors and show every reason."),
    ("Context", "Collect the issue, repository guidance, and relevant files."),
    ("Analyse", "Explain the problem, propose a minimal fix, list risks and tests."),
    ("Generate a patch", "Draft a unified diff and prove it applies."),
    ("Validate", "Run the repository's tests and lint in an isolated container."),
    ("Approve", "A human approves, requests changes, or rejects, bound to the patch hash."),
    ("Publish", "Open a draft pull request, only after explicit approval."),
    ("Track", "Follow reviews, requested changes, merge status, and cost."),
]

HONESTY = (
    "The ranking weights are initial heuristics, not proven predictors of whether "
    "a maintainer will accept a change. The evaluation numbers are fixture "
    "arithmetic, not a benchmark. Nothing here claims a merged upstream "
    "contribution."
)


def render(mode) -> None:
    ui.page_setup("Home")
    ui.mode_banner(mode)
    ui.header("ForgeFlow", INTRO)

    ui.render_text(
        "What this is",
        "ForgeFlow is a portfolio project: a full pipeline for preparing "
        "open-source contributions, built to keep a human in control of every "
        "external write. This website is the pipeline's front end.",
    )

    ui._st().markdown("**The workflow**")
    for index, (name, detail) in enumerate(PIPELINE, start=1):
        ui._st().text(f"{index}. {name} - {detail}")

    ui.disclaimer(HONESTY)

    ui._st().markdown("**How to use it**")
    if mode.is_hosted:
        ui._st().text(
            "You are in hosted mode. Everything read-only is available: find and "
            "rank issues, check a repository's contribution rules, run an AI "
            "analysis with your own key, and generate a draft patch. Validation "
            "and publishing are locked here, because they need a container "
            "runtime and the publishing token, which a shared server must never "
            "hold."
        )
    else:
        ui._st().text(
            "You are in local mode. The read-only pages work as in hosted mode, "
            "and Validation and Approval/Publish are unlocked because they run on "
            "your own machine through ForgeFlow's existing guards."
        )
