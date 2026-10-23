"""Tests for the enforcement gate before awaiting_publish_approval (SPEC 7, 8).

The guarantee under test: a task cannot reach awaiting_publish_approval unless a
check actually executed and passed for that exact patch, or a documented
exception exists. The cases that matter most are the blocked ones, above all
"all checks skipped", which is what an environment without container isolation
produces and must never be mistaken for a pass.
"""

from __future__ import annotations

import pytest

from forgeflow import approval, audit, storage
from forgeflow.approval import ApprovalError, require_validation_or_exception
from forgeflow.models import (
    ApprovalAction,
    CheckKind,
    CheckStatus,
    IllegalTransitionError,
    Issue,
    PatchRecord,
    Repository,
    TaskState,
    ValidationCheck,
    ValidationReport,
)
from forgeflow.patch import compute_patch_sha256

REPO = "acme/widget"
DIFF = "diff --git a/p.py b/p.py\n--- a/p.py\n+++ b/p.py\n@@ -1 +1,2 @@\n def f():\n+    pass\n"
HASH = compute_patch_sha256(DIFF)
OTHER_HASH = compute_patch_sha256("something else")


@pytest.fixture
def conn(tmp_path):
    connection = storage.connect(tmp_path / "t.db")
    storage.init_db(connection)
    storage.upsert_repository(connection, Repository(full_name=REPO, stars=10))
    storage.upsert_issue(connection, Issue(repo_full_name=REPO, number=7, title="t", body="b"))
    for target in (
        TaskState.SHORTLISTED,
        TaskState.ANALYZED,
        TaskState.AWAITING_PLAN_APPROVAL,
        TaskState.IMPLEMENTING,
        TaskState.VALIDATING,
    ):
        storage.transition_task_state(connection, REPO, 7, target)
    storage.record_patch(
        connection,
        PatchRecord(
            repo_full_name=REPO,
            issue_number=7,
            provider="mock",
            patch_sha256=HASH,
            diff=DIFF,
        ),
    )
    yield connection
    connection.close()


def store(conn, statuses: list[CheckStatus], *, patch_sha256: str = HASH) -> None:
    checks = [
        ValidationCheck(
            kind=CheckKind.TESTS,
            name=f"check{index}",
            command=["python", "-m", "pytest"],
            status=status,
            exit_code=0 if status is CheckStatus.PASSED else 1,
            skip_reason="no isolation" if status is CheckStatus.SKIPPED else None,
        )
        for index, status in enumerate(statuses)
    ]
    storage.record_validation_report(
        conn,
        ValidationReport(
            repo_full_name=REPO,
            issue_number=7,
            patch_sha256=patch_sha256,
            isolated=True,
            checks=checks,
        ),
    )


# --------------------------------------------------------------------------
# The gate function directly
# --------------------------------------------------------------------------


def test_gate_allows_a_passed_check(conn) -> None:
    store(conn, [CheckStatus.PASSED])
    ok, reason = require_validation_or_exception(conn, REPO, 7, patch_sha256=HASH)
    assert ok is True
    assert reason == ""


def test_gate_allows_a_recorded_exception(conn) -> None:
    storage.record_validation_exception(
        conn, REPO, 7, patch_sha256=HASH, authorized_by="manyashre", reason="flaky upstream"
    )
    ok, reason = require_validation_or_exception(conn, REPO, 7, patch_sha256=HASH)
    assert ok is True
    assert "manyashre" in reason


def test_gate_blocks_when_nothing_ran(conn) -> None:
    ok, reason = require_validation_or_exception(conn, REPO, 7, patch_sha256=HASH)
    assert ok is False
    assert "no validation has been run" in reason


def test_gate_blocks_all_skipped(conn) -> None:
    """The headline case: skipped is not passed."""
    store(conn, [CheckStatus.SKIPPED, CheckStatus.SKIPPED])
    ok, reason = require_validation_or_exception(conn, REPO, 7, patch_sha256=HASH)
    assert ok is False
    assert "no executed checks" in reason


def test_gate_blocks_a_failure(conn) -> None:
    store(conn, [CheckStatus.FAILED])
    ok, reason = require_validation_or_exception(conn, REPO, 7, patch_sha256=HASH)
    assert ok is False
    assert "validation failed" in reason


def test_gate_blocks_an_error(conn) -> None:
    store(conn, [CheckStatus.ERRORED])
    ok, _ = require_validation_or_exception(conn, REPO, 7, patch_sha256=HASH)
    assert ok is False


def test_gate_blocks_evidence_for_another_patch(conn) -> None:
    store(conn, [CheckStatus.PASSED], patch_sha256=OTHER_HASH)
    ok, _ = require_validation_or_exception(conn, REPO, 7, patch_sha256=HASH)
    assert ok is False


def test_gate_blocks_an_exception_for_another_patch(conn) -> None:
    storage.record_validation_exception(
        conn, REPO, 7, patch_sha256=OTHER_HASH, authorized_by="x", reason="y"
    )
    ok, _ = require_validation_or_exception(conn, REPO, 7, patch_sha256=HASH)
    assert ok is False


# --------------------------------------------------------------------------
# Through apply_action
# --------------------------------------------------------------------------


def test_approve_blocked_without_validation(conn) -> None:
    with pytest.raises(ApprovalError, match="no validation has been run"):
        approval.apply_action(conn, REPO, 7, ApprovalAction.APPROVE)


def test_approve_blocked_when_all_skipped(conn) -> None:
    store(conn, [CheckStatus.SKIPPED])
    with pytest.raises(ApprovalError, match="no executed checks"):
        approval.apply_action(conn, REPO, 7, ApprovalAction.APPROVE)


def test_approve_blocked_on_failure(conn) -> None:
    store(conn, [CheckStatus.FAILED])
    with pytest.raises(ApprovalError, match="validation failed"):
        approval.apply_action(conn, REPO, 7, ApprovalAction.APPROVE)


def test_approve_allowed_after_a_pass(conn) -> None:
    store(conn, [CheckStatus.PASSED])
    _, state = approval.apply_action(conn, REPO, 7, ApprovalAction.APPROVE)
    assert state is TaskState.AWAITING_PUBLISH_APPROVAL


def test_approve_allowed_with_an_exception(conn) -> None:
    storage.record_validation_exception(
        conn, REPO, 7, patch_sha256=HASH, authorized_by="manyashre", reason="verified by hand"
    )
    _, state = approval.apply_action(conn, REPO, 7, ApprovalAction.APPROVE)
    assert state is TaskState.AWAITING_PUBLISH_APPROVAL


def test_task_cannot_reach_awaiting_publish_approval_without_validation(conn) -> None:
    """The end state must be unchanged after a blocked approve."""
    with pytest.raises(ApprovalError):
        approval.apply_action(conn, REPO, 7, ApprovalAction.APPROVE)
    assert storage.get_issue(conn, REPO, 7).task_state is TaskState.VALIDATING


def test_blocked_approve_leaves_no_approval_row(conn) -> None:
    """A refused action must not leave a decision on record."""
    with pytest.raises(ApprovalError):
        approval.apply_action(conn, REPO, 7, ApprovalAction.APPROVE)
    assert approval.live_approval(conn, REPO, 7) is None


def test_blocked_approve_writes_no_approval_audit_event(conn) -> None:
    with pytest.raises(ApprovalError):
        approval.apply_action(conn, REPO, 7, ApprovalAction.APPROVE)
    assert audit.list_events(conn, event="approval.approve") == []


def test_blocked_approve_does_not_invalidate_a_prior_pass(conn) -> None:
    """A later blocked attempt must not disturb valid evidence."""
    store(conn, [CheckStatus.PASSED])
    _, state = approval.apply_action(conn, REPO, 7, ApprovalAction.APPROVE)
    assert state is TaskState.AWAITING_PUBLISH_APPROVAL
    # The approval is live and still bound to the patch.
    ok, _ = approval.check_approval(conn, REPO, 7, diff=DIFF, target_branch="")
    assert ok is True


# --------------------------------------------------------------------------
# Non-approve actions are not gated by validation
# --------------------------------------------------------------------------


def test_request_changes_is_not_gated(conn) -> None:
    """Sending work back needs no validation evidence."""
    _, state = approval.apply_action(conn, REPO, 7, ApprovalAction.REQUEST_CHANGES)
    assert state is TaskState.IMPLEMENTING


def test_reject_is_not_gated(conn) -> None:
    _, state = approval.apply_action(conn, REPO, 7, ApprovalAction.REJECT)
    assert state is TaskState.DISCARDED


def test_discard_is_not_gated(conn) -> None:
    _, state = approval.apply_action(conn, REPO, 7, ApprovalAction.DISCARD)
    assert state is TaskState.DISCARDED


def test_illegal_transition_still_refused(conn) -> None:
    """The ordering fix must not weaken the state machine.

    From failed_validation the legal moves are implementing and discarded, so
    approve is refused by the state machine before the validation gate is even
    consulted.
    """
    storage.transition_task_state(conn, REPO, 7, TaskState.FAILED_VALIDATION)
    with pytest.raises(IllegalTransitionError, match="illegal transition"):
        approval.apply_action(conn, REPO, 7, ApprovalAction.APPROVE)


def test_illegal_transition_leaves_no_approval_row(conn) -> None:
    storage.transition_task_state(conn, REPO, 7, TaskState.FAILED_VALIDATION)
    with pytest.raises(IllegalTransitionError):
        approval.apply_action(conn, REPO, 7, ApprovalAction.APPROVE)
    assert approval.live_approval(conn, REPO, 7) is None


# --------------------------------------------------------------------------
# A pass must survive the round trip through the database
# --------------------------------------------------------------------------


def test_pass_recorded_by_validation_enables_approval(conn) -> None:
    """Wire the real producer to the real gate."""
    from forgeflow.pipeline import run_validation
    from forgeflow.sandbox import (
        IsolationKind,
        IsolationStatus,
        Sandbox,
        SandboxResult,
    )
    from forgeflow.validation import ValidationCommands, Validator

    isolated = IsolationStatus(IsolationKind.DOCKER, True, "ok")
    sandbox = Sandbox(status=isolated)
    sandbox.run = lambda command, *, workspace, limits=None: SandboxResult(  # type: ignore[method-assign]
        command=list(command),
        exit_code=0,
        stdout="ok",
        stderr="",
        duration_seconds=0.1,
        isolated=True,
    )
    run_validation(
        conn,
        REPO,
        7,
        validator=Validator(sandbox),
        commands=ValidationCommands(tests=["python", "-m", "pytest"]),
    )
    _, state = approval.apply_action(conn, REPO, 7, ApprovalAction.APPROVE)
    assert state is TaskState.AWAITING_PUBLISH_APPROVAL


def test_failed_validation_recorded_by_pipeline_blocks_approval(conn) -> None:
    from forgeflow.pipeline import run_validation
    from forgeflow.sandbox import (
        IsolationKind,
        IsolationStatus,
        Sandbox,
        SandboxResult,
    )
    from forgeflow.validation import ValidationCommands, Validator

    isolated = IsolationStatus(IsolationKind.DOCKER, True, "ok")
    sandbox = Sandbox(status=isolated)
    sandbox.run = lambda command, *, workspace, limits=None: SandboxResult(  # type: ignore[method-assign]
        command=list(command),
        exit_code=1,
        stdout="failed",
        stderr="",
        duration_seconds=0.1,
        isolated=True,
    )
    run_validation(
        conn,
        REPO,
        7,
        validator=Validator(sandbox),
        commands=ValidationCommands(tests=["python", "-m", "pytest"]),
    )
    # The pipeline moved the task to failed_validation, so approving is now an
    # illegal transition as well as unproven.
    with pytest.raises(IllegalTransitionError):
        approval.apply_action(conn, REPO, 7, ApprovalAction.APPROVE)
