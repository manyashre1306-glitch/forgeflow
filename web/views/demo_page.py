"""Live Demo: run ForgeFlow's offline, dry-run pipeline in this process.

This calls :func:`demo.run_demo`, which uses ``MockAgent`` and in-process fake
GitHub clients that count write requests. It makes no network call, no model
call, and no external write. The zero-write count is asserted, not assumed.
"""

from __future__ import annotations

from web import ui


def render(mode) -> None:
    st = ui._st()
    ui.page_setup("Live Demo")
    ui.mode_banner(mode)
    ui.header(
        "Live Demo",
        "Run the whole pipeline offline, in this process, against a fixture "
        "repository. No network, no model, no write.",
    )
    ui.disclaimer(
        "This is a demonstration of the pipeline's shape, not a benchmark and not "
        "evidence about a real repository. It uses a mock coding agent and fake "
        "GitHub clients."
    )

    if not st.button("Run the offline demo", type="primary"):
        return

    try:
        from demo import run_demo
    except Exception as exc:
        ui.show_error("The demo module could not be imported.", f"{type(exc).__name__}: {exc}")
        return

    try:
        report = run_demo(db_path=":memory:")
    except Exception as exc:
        ui.show_error("The demo failed.", f"{type(exc).__name__}: {exc}")
        return

    st.success("Demo completed.")
    ui.metrics_row(
        [
            ("Stages", str(len(report.stages))),
            ("Writes attempted", str(report.writes_attempted)),
            ("Dry run recorded", "yes" if report.dry_run_recorded else "no"),
            ("Final state", report.task_state or "(unknown)"),
        ]
    )

    if report.writes_attempted != 0:
        st.error(
            f"The demo attempted {report.writes_attempted} write(s). That should "
            "never happen; the demo is dry-run only."
        )

    st.markdown("**Stages**")
    for stage, detail in report.stages:
        st.text(f"- {stage}: {detail}")

    if report.third_party_refused_reason:
        st.markdown("**Third-party refusal**")
        st.text(report.third_party_refused_reason)

    if report.plan_render:
        ui.render_fenced("Publish plan (dry run)", report.plan_render)
    if report.body:
        ui.render_fenced("Proposed PR body (not sent)", report.body, language="markdown")
