"""Validation: run a repository's tests and lint in an isolated container.

Locked in hosted mode. The lock is enforced in two places: this page refuses to
draw the controls, and :func:`web.local.run_validation` raises
:class:`web.modes.HostedModeRefused` if it is called directly anyway.
"""

from __future__ import annotations

from web import local, ui
from web.modes import HostedModeRefused


def render(mode) -> None:
    st = ui._st()
    ui.page_setup("Validation")
    ui.mode_banner(mode)
    ui.header(
        "Validation",
        "Run the repository's own tests and lint against a patch inside an "
        "isolated container, then report the real results.",
    )

    if mode.is_hosted:
        st.error("Validation is locked in hosted mode.")
        st.text(
            "Validation runs repository code inside a container. A shared public "
            "server must not execute untrusted code, so this page is disabled "
            "here. Run the website locally (FORGEFLOW_WEB_MODE=local) to use it."
        )
        return

    repo = st.text_input("Repository", value="pydantic/pydantic", key="validate_repo")
    number = st.text_input("Issue number", value="12345", key="validate_number")

    isolated, reason = local.sandbox_status(mode)
    if isolated:
        st.success(f"Container runtime available: {reason}")
    else:
        st.warning(
            "No container runtime is available, so validation will refuse to run "
            "rather than execute code on the host."
        )
        st.text(reason)

    if not st.button("Run validation", type="primary"):
        ui.disclaimer(
            "The sandbox refuses when no container runtime is available. A refusal "
            "is reported as skipped checks, never as a pass."
        )
        return

    try:
        report = local.run_validation(mode, repo, int(number))
    except HostedModeRefused as exc:
        ui.show_error("Refused by mode policy.", str(exc))
        return
    except Exception as exc:
        ui.show_error("Validation could not run.", f"{type(exc).__name__}: {exc}")
        return

    st.markdown("**Result**")
    st.text(f"Isolated: {report.isolated}")
    st.text(f"Repair attempts: {report.repair_attempts}")
    if report.environment_limitations:
        ui.render_list("Environment limitations", report.environment_limitations)

    rows = []
    for check in report.checks:
        rows.append(
            {
                "name": check.name,
                "status": check.status.value,
                "detail": check.detail,
            }
        )
    ui.data_table(rows, empty="No checks were recorded.")

    if report.findings:
        st.markdown("**Diff findings**")
        for finding in report.findings:
            line = f"[{finding.severity}] {finding.detail}"
            (st.error if finding.severity == "blocking" else st.warning)(line)
