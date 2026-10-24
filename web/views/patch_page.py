"""Patch Generator: draft a unified diff and prove it applies.

The patch is generated with the visitor's key and verified with
:func:`web.patch_agent.check_diff_applies`, which runs ForgeFlow's real
``apply_diff``. A patch that does not apply is never shown as usable.
"""

from __future__ import annotations

from web import clients, patch_agent, service, ui
from web.limits import InputError, SessionLimits, cap_text, validate_issue_number


def render(mode) -> None:
    st = ui._st()
    ui.page_setup("Patch Generator")
    ui.mode_banner(mode)
    ui.header(
        "Patch Generator",
        "Ask a model for a unified diff, then check it applies cleanly before you "
        "trust it. This drafts a patch; it does not run it or publish it.",
    )

    ui.disclaimer(
        "The generated diff is a draft. It is verified to apply, but it has not "
        "been run and its tests have not been executed. Treat it as a starting "
        "point, not a finished change."
    )

    token = ui.secret_input("GitHub token (optional)", key="github_token")
    api_key = ui.secret_input("Gemini API key", key="gemini_key")
    col1, col2 = st.columns(2)
    repo = col1.text_input("Repository", value="pydantic/pydantic", key="patch_repo")
    number = col2.text_input("Issue number", value="12345", key="patch_number")
    plan = st.text_area(
        "Approved plan",
        key="patch_plan",
        help="The fix you want implemented. This is treated as trusted instruction.",
        height=160,
    )

    if not st.button("Generate patch", type="primary"):
        return

    if not api_key.strip():
        ui.show_error("A Gemini API key is required to generate a patch.")
        return
    if not plan.strip():
        ui.show_error("Enter the plan you want implemented.")
        return

    try:
        number_int = validate_issue_number(number)
        issue = service.issue_from_inputs(repo, number_int)
    except InputError as exc:
        ui.show_error("Those inputs are not valid.", str(exc))
        return

    limits = SessionLimits()
    client = clients.build_read_client(mode, visitor_token=token)
    try:
        context = _build_context(client, issue, limits)
        if context is None:
            ui.show_error("Could not build a repository context to patch against.")
            return

        config = service.default_llm_config()
        agent = patch_agent.build_patch_agent(
            api_key=api_key.strip(),
            model=config.analysis_model,
            limits=limits,
            config=config,
        )
        try:
            result = agent.run_task(context, cap_text(plan)[0], timeout=120.0)
        except Exception as exc:
            ui.show_error(
                "No usable patch was produced.",
                f"{type(exc).__name__}: {exc}",
            )
            return
    finally:
        client.close()

    st.success("A patch was generated and verified to apply.")
    ui.render_fenced("Proposed diff", result.diff, language="diff")

    findings = patch_agent.patch_findings(result.diff)
    if findings:
        st.markdown("**Diff findings (from ForgeFlow's diff analysis)**")
        for finding in findings:
            line = f"[{finding['severity']}] {finding['detail']}"
            (st.error if finding["severity"] == "blocking" else st.warning)(line)
    else:
        st.caption("Diff analysis reported no hazards.")

    ui.metrics_row(
        [
            ("Model calls", str(limits.model_calls)),
            ("Prompt tokens", f"{limits.prompt_tokens:,}"),
            ("Completion tokens", f"{limits.completion_tokens:,}"),
            ("Cost (USD)", f"${limits.cost_usd:.6f}"),
        ]
    )
    ui.disclaimer(
        "This patch has not been applied to a working tree, has not been run, and "
        "its tests have not been executed. Run it locally before trusting it."
    )


def _build_context(client, issue, limits: SessionLimits):
    from web.context import ContextBudget, build_hosted_context

    budget = ContextBudget()
    try:
        context = build_hosted_context(client, issue, budget=budget)
    except Exception:
        return None
    limits.record_github_request(budget.total)
    return context
