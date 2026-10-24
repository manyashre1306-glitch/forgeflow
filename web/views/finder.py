"""Issue Finder: discover, filter, dedupe, and rank one repository's issues."""

from __future__ import annotations

from forgeflow.config import AppConfig
from web import clients, service, ui
from web.limits import InputError, SessionLimits


def render(mode) -> None:
    st = ui._st()
    ui.page_setup("Issue Finder")
    ui.mode_banner(mode)
    ui.header(
        "Issue Finder",
        "Discover open issues in one repository, then rank them with the six weighted factors.",
    )

    token = ui.secret_input(
        "GitHub token (optional)",
        key="github_token",
        help_text="A read-only token raises the rate limit. Leave blank to search anonymously.",
    )
    repo = st.text_input("Repository", value="pydantic/pydantic", key="finder_repo")
    per_repo = st.slider("Issues to fetch", min_value=10, max_value=100, value=50, step=10)

    limits = SessionLimits()
    if not st.button("Find and rank", type="primary"):
        ui.disclaimer(service.HEURISTIC_CAVEAT)
        return

    try:
        config = AppConfig(repositories=[repo])
        client = clients.build_read_client(mode, visitor_token=token)
    except InputError as exc:
        ui.show_error("That repository reference is not valid.", str(exc))
        return

    try:
        result = service.find_and_rank(client, config, repo, limits=limits, per_repo_limit=per_repo)
    except Exception as exc:
        ui.show_error("The search failed.", f"{type(exc).__name__}: {exc}")
        return
    finally:
        client.close()

    if result.errors:
        for error in result.errors:
            ui.show_error(error)

    ui.metrics_row(
        [
            ("Repository", result.repo_full_name),
            ("Stars", str(result.repository_stars)),
            ("Contributing guide", "yes" if result.has_contributing else "no"),
            ("Tests detected", "yes" if result.has_tests else "no"),
            ("Issues kept", str(result.issues_found)),
        ]
    )

    if not result.ranked:
        st.warning("No eligible issues were found. They may all have been filtered out.")
    else:
        st.markdown("**Ranked issues**")
        for row in result.ranked:
            _render_row(st, row)

    if result.dropped:
        with st.expander(f"Filtered out ({len(result.dropped)})"):
            for reference, reason in result.dropped:
                st.text(f"- {reference}: {reason}")

    ui.disclaimer(result.heuristic_caveat)
    ui.disclaimer(
        f"GitHub requests used this session: {limits.github_requests} of "
        f"{limits.max_github_requests}."
    )


def _render_row(st, row) -> None:
    label = f"#{row.rank}  {row.repo_full_name}#{row.number} - {row.title}"
    with st.expander(f"{label}   (score {row.total})"):
        if row.url:
            st.text(f"URL: {row.url}")
        if row.labels:
            st.text("Labels: " + ", ".join(row.labels))
        if row.outcome_adjustment:
            st.text(f"Outcome adjustment: {row.outcome_adjustment:+.2f}")
        st.text("Factor breakdown:")
        for factor in row.factors:
            st.text(
                f"  {factor.label}: weight {factor.weight:.2f} x value "
                f"{factor.value:.2f} = {factor.points:.2f} points"
            )
            st.caption(f"      {factor.reason}")
