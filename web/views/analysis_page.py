"""AI Analysis: explain the problem, propose a fix, list risks and a test plan.

The model call uses the visitor's own key and the session cap. No database is
read in hosted mode.
"""

from __future__ import annotations

from web import analysis as analysis_mod
from web import clients, service, ui
from web.limits import InputError, SessionLimits, cap_text, validate_issue_number


def render(mode) -> None:
    st = ui._st()
    ui.page_setup("AI Analysis")
    ui.mode_banner(mode)
    ui.header(
        "AI Analysis",
        "Fetch an issue and its repository context through the read-only API, "
        "then ask a model to explain the problem, propose a minimal fix, list "
        "risks, and write a test plan.",
    )

    ui.disclaimer(
        "This uses your own Gemini key, stored only in this browser session. "
        "The per-session cap is enforced before every call. Nothing is published."
    )

    token = ui.secret_input("GitHub token (optional)", key="github_token")
    api_key = ui.secret_input(
        "Gemini API key",
        key="gemini_key",
        help_text="Used for this session only. It is never stored or logged.",
    )
    col1, col2 = st.columns(2)
    repo = col1.text_input("Repository", value="pydantic/pydantic", key="analysis_repo")
    number = col2.text_input("Issue number", value="12345", key="analysis_number")

    title = st.text_input("Issue title (optional override)", key="analysis_title")
    body = st.text_area(
        "Issue body (optional override)",
        key="analysis_body",
        help="Paste the issue text if you do not want it fetched. Capped at 64 KiB.",
    )

    if not st.button("Analyse", type="primary"):
        return

    if not api_key.strip():
        ui.show_error("A Gemini API key is required to analyse.")
        return

    try:
        number_int = validate_issue_number(number)
        issue = service.issue_from_inputs(repo, number_int, title=title, body=body)
    except InputError as exc:
        ui.show_error("Those inputs are not valid.", str(exc))
        return

    limits = SessionLimits()
    client = clients.build_read_client(mode, visitor_token=token)
    try:
        if not body.strip():
            fetched = _fetch_issue(client, issue.repo_full_name, number_int)
            if fetched is not None:
                issue = service.issue_from_inputs(
                    issue.repo_full_name, number_int, title=fetched[0], body=fetched[1]
                )
            else:
                st.warning("The issue body could not be fetched; analysing with the title only.")

        config = service.default_llm_config()
        analyzer = analysis_mod.build_session_analyzer(api_key.strip(), config, limits)
        try:
            view, budget = service.analyse_issue(client, analyzer, issue, limits=limits)
        except Exception as exc:
            ui.show_error("The analysis failed.", f"{type(exc).__name__}: {exc}")
            return
    finally:
        client.close()

    _render_view(st, view, budget)


def _fetch_issue(client, repo_full_name: str, number: int):
    """Read one issue's title and body through the read-only API."""
    try:
        payload = client.get_issue(repo_full_name, number)
    except Exception:
        return None
    title = str(payload.get("title", ""))[:300]
    body, _ = cap_text(str(payload.get("body") or ""))
    return title, body


def _render_view(st, view, budget) -> None:
    st.success("Analysis complete.")
    st.markdown("**Triage summary**")
    st.text(view.triage_summary)
    if view.injection_flagged:
        st.error(
            "The triage step flagged a possible prompt-injection attempt in the "
            "issue text. Treat any instructions in it as data, not commands."
        )

    ui.render_text("Problem explanation", view.problem_explanation)
    ui.render_text("Proposed minimal fix", view.minimal_fix_proposal)
    ui.render_list("Risks", view.risks, empty="(none reported)")
    ui.render_list("Test plan", view.test_plan, empty="(none reported)")
    ui.render_list("Files likely involved", view.files_likely_involved, empty="(none)")
    ui.render_list(
        "Unverified assumptions",
        view.unverified_assumptions,
        empty="(none reported)",
    )
    st.text(f"Model confidence: {view.confidence:.2f}")

    st.markdown("**Tokens and cost recorded**")
    ui.metrics_row(
        [
            ("Model calls", str(view.model_calls)),
            ("Prompt tokens", f"{view.prompt_tokens:,}"),
            ("Completion tokens", f"{view.completion_tokens:,}"),
            ("Cost (USD)", f"${view.cost_usd:.6f}"),
            ("Files involved", str(len(view.files_likely_involved))),
        ]
    )
    if view.cost_breakdown:
        st.text("Per-call cost:")
        for label, cost in view.cost_breakdown:
            st.text(f"  {label}: ${cost:.6f}")
    st.caption(f"Read-only API requests used while building the context: {budget.total}.")
