"""Approval and Publish: the human boundary before any external write.

Locked in hosted mode. The lock is enforced here *and* in
:mod:`web.local`, so a direct call still refuses. A real publish needs the exact
typed confirmation and a patch hash that still matches what was confirmed.
"""

from __future__ import annotations

from web import local, ui
from web.modes import HostedModeRefused


def render(mode) -> None:
    st = ui._st()
    ui.page_setup("Approval and Publish")
    ui.mode_banner(mode)
    ui.header(
        "Approval and Publish",
        "Review a prepared patch, approve or reject it, and open a draft pull "
        "request only after an explicit, patch-bound confirmation.",
    )

    if mode.is_hosted:
        st.error("Approval and publishing are locked in hosted mode.")
        st.text(
            "Publishing needs the separate publishing token, which a shared "
            "public server must never hold, and it must only run on your own "
            "machine. Run the website locally (FORGEFLOW_WEB_MODE=local) to use "
            "this page."
        )
        return

    st.warning(
        "Publishing opens a draft pull request on GitHub. It never merges and "
        "never force-pushes. Review the plan first."
    )

    repo = st.text_input("Repository", value="pydantic/pydantic", key="publish_repo")
    number = st.text_input("Issue number", value="12345", key="publish_number")
    branch = st.text_input("Target branch", value="main", key="publish_branch")

    if st.button("Load approval state"):
        try:
            context = local.publish_context(mode, repo, int(number), target_branch=branch)
        except HostedModeRefused as exc:
            ui.show_error("Refused by mode policy.", str(exc))
            return
        except Exception as exc:
            ui.show_error("Could not load the task.", f"{type(exc).__name__}: {exc}")
            return
        st.session_state["publish_context"] = context
        st.success("Loaded.")

    context = st.session_state.get("publish_context")
    if context is None:
        ui.disclaimer("Load a task to see its state and the available actions.")
        return

    ui.metrics_row(
        [
            ("Repository", context.repo_full_name),
            ("Issue", str(context.issue_number)),
            ("State", context.task_state),
            ("Target branch", context.target_branch),
            ("Patch hash", context.patch_sha256[:12]),
        ]
    )

    st.markdown("**Decision**")
    action = st.radio(
        "Action",
        ["approve", "request_changes", "reject", "discard"],
        horizontal=True,
        key="publish_action",
    )
    reason = st.text_input("Reason (recorded in the audit log)", key="publish_reason")
    if st.button("Record decision"):
        try:
            new_action, new_state = local.apply_approval(
                mode,
                context.repo_full_name,
                context.issue_number,
                action,
                reason=reason,
                target_branch=context.target_branch,
            )
        except HostedModeRefused as exc:
            ui.show_error("Refused by mode policy.", str(exc))
            return
        except Exception as exc:
            ui.show_error("The decision could not be recorded.", f"{type(exc).__name__}: {exc}")
            return
        st.success(f"Recorded {new_action}; task state is now {new_state}.")

    st.markdown("**Publish**")
    if st.button("Build dry-run plan"):
        try:
            plan = local.dry_run_publish(
                mode,
                context.repo_full_name,
                context.issue_number,
                target_branch=context.target_branch,
            )
        except HostedModeRefused as exc:
            ui.show_error("Refused by mode policy.", str(exc))
            return
        except Exception as exc:
            ui.show_error("The dry run failed.", f"{type(exc).__name__}: {exc}")
            return
        ui.render_fenced("Dry-run plan (nothing was sent)", plan)

    expected = local.confirmation_string(
        context.repo_full_name, context.target_branch, context.patch_sha256
    )
    st.text("To publish for real, type this exactly:")
    st.code(expected, language="text")
    confirmation = st.text_input("Confirmation", key="publish_confirmation")

    if st.button("Publish draft pull request", type="primary"):
        if confirmation.strip() != expected:
            ui.show_error("The typed confirmation does not match.")
            return
        try:
            record = local.real_publish(
                mode,
                context.repo_full_name,
                context.issue_number,
                target_branch=context.target_branch,
                expected_patch_sha256=context.patch_sha256,
                confirmation=confirmation,
            )
        except HostedModeRefused as exc:
            ui.show_error("Refused by mode policy.", str(exc))
            return
        except Exception as exc:
            ui.show_error("Publishing was refused or failed.", f"{type(exc).__name__}: {exc}")
            return
        st.success("A draft pull request was opened.")
        ui.render_text("Publication record", str(record))
