"""Repo Rules Checker: what a repository says about AI-assisted contributions."""

from __future__ import annotations

from web import clients, service, ui
from web.limits import InputError, SessionLimits

_VERDICT_STYLE = {
    "allowed": "success",
    "disclosure required": "warning",
    "restricted": "warning",
    "restricted (AI contributions prohibited)": "error",
    "unknown": "info",
}


def render(mode) -> None:
    st = ui._st()
    ui.page_setup("Repo Rules Checker")
    ui.mode_banner(mode)
    ui.header(
        "Repo Rules Checker",
        "Read a repository's CONTRIBUTING and README through the read-only API "
        "and report what they say about AI-assisted contributions.",
    )

    token = ui.secret_input("GitHub token (optional)", key="github_token")
    repo = st.text_input("Repository", value="scikit-learn/scikit-learn", key="rules_repo")

    if not st.button("Check rules", type="primary"):
        ui.disclaimer(
            "Unknown is not permission. An absent statement is treated as no "
            "statement, and publishing is blocked until a human records a "
            "documented override."
        )
        return

    client = None
    try:
        client = clients.build_read_client(mode, visitor_token=token)
        result = service.check_repo_rules(client, repo, limits=SessionLimits())
    except InputError as exc:
        ui.show_error("That repository reference is not valid.", str(exc))
        return
    except Exception as exc:
        ui.show_error("The check failed.", f"{type(exc).__name__}: {exc}")
        return
    finally:
        if client is not None:
            client.close()

    show = {
        "success": st.success,
        "warning": st.warning,
        "error": st.error,
        "info": st.info,
    }.get(_VERDICT_STYLE.get(result.verdict, "info"), st.info)
    show(f"Verdict: {result.verdict}")
    st.text(result.verdict_explanation)

    ui.metrics_row(
        [
            ("Requires tests", "yes" if result.requires_tests else "no"),
            ("Requires sign-off", "yes" if result.requires_signoff else "no"),
            ("Requires CLA", "yes" if result.requires_cla else "no"),
            ("AI disclosure", "yes" if result.requires_ai_disclosure else "no"),
        ]
    )

    ui.render_list("Evidence found", result.evidence, empty="(no AI-use statement found)")
    ui.render_list("Documents read", result.documents_read, empty="(none found)")
    ui.render_list("Sources", result.sources, empty="(none)")
    ui.disclaimer(result.unknown_is_not_permission)
