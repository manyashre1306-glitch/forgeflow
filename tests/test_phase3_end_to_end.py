"""End-to-end tests for the Phase 3 validation and approval chain.

This file exercises the whole path SPEC sections 7, 8, 9, and 10 describe:
implement leaves the task at validating, validate runs the gates and records
evidence, the approval gate refuses to advance without proof, an exception can
authorize an override, and every step is audited.

Where the individual modules are tested in isolation elsewhere, these tests
drive the real functions in sequence against a real SQLite database. The only
substitution is the container runtime, which is unavailable here: the sandbox's
run method is replaced so the Validator's own command-building, exit-code
interpretation, and classification logic all still execute.
"""

from __future__ import annotations

import pytest

from forgeflow import approval, audit, storage
from forgeflow.approval import ApprovalError, require_validation_or_exception
from forgeflow.models import (
    ApprovalAction,
    CheckKind,
    CheckStatus,
    Issue,
    PatchRecord,
    Repository,
    TaskState,
    ValidationCheck,
    ValidationReport,
)
from forgeflow.patch import compute_patch_sha256
from forgeflow.pipeline import run_validation
from forgeflow.sandbox import (
    IsolationKind,
    IsolationStatus,
    Sandbox,
    SandboxResult,
)
from forgeflow.validation import ValidationCommands, Validator

REPO = "acme/widget"
ISSUE = 7
DIFF = "diff --git a/p.py b/p.py\n--- a/p.py\n+++ b/p.py\n@@ -1 +1,2 @@\n def f():\n+    pass\n"
HASH = compute_patch_sha256(DIFF)

ISOLATED = IsolationStatus(IsolationKind.DOCKER, True, "docker reachable")
NO_ISOLATION = IsolationStatus(IsolationKind.NONE, False, "no runtime")


@pytest.fixture
def conn(tmp_path):
    connection = storage.connect(tmp_path / "ff.db")
    storage.init_db(connection)
    yield connection
    connection.close()


def seed_to_validating(conn, diff: str = DIFF) -> str:
    storage.upsert_repository(conn, Repository(full_name=REPO, stars=10))
    storage.upsert_issue(conn, Issue(repo_full_name=REPO, number=ISSUE, title="t", body="b"))
    for target in (
        TaskState.SHORTLISTED,
        TaskState.ANALYZED,
        TaskState.AWAITING_PLAN_APPROVAL,
        TaskState.IMPLEMENTING,
        TaskState.VALIDATING,
    ):
        storage.transition_task_state(conn, REPO, ISSUE, target)
    digest = compute_patch_sha256(diff)
    storage.record_patch(
        conn,
        PatchRecord(
            repo_full_name=REPO,
            issue_number=ISSUE,
            provider="mock",
            patch_sha256=digest,
            diff=diff,
        ),
    )
    return digest


def sandbox_with(exit_code: int, *, isolated: bool = True) -> Validator:
    status = ISOLATED if isolated else NO_ISOLATION
    sandbox = Sandbox(status=status)
    if isolated:

        def fake_run(command, *, workspace, limits=None):
            return SandboxResult(
                command=list(command),
                exit_code=exit_code,
                stdout="output",
                stderr="",
                duration_seconds=0.2,
                isolated=True,
            )

        sandbox.run = fake_run  # type: ignore[method-assign]
    return Validator(sandbox)


def commands() -> ValidationCommands:
    return ValidationCommands(tests=["python", "-m", "pytest"])


# ==========================================================================
# The happy path
# ==========================================================================


def test_full_happy_path_from_validating_to_publish_ready(conn) -> None:
    seed_to_validating(conn)

    outcome = run_validation(conn, REPO, ISSUE, validator=sandbox_with(0), commands=commands())
    assert outcome.passed is True
    assert outcome.issue.task_state is TaskState.VALIDATING

    _, state = approval.apply_action(conn, REPO, ISSUE, ApprovalAction.APPROVE)
    assert state is TaskState.AWAITING_PUBLISH_APPROVAL


def test_happy_path_is_fully_audited(conn) -> None:
    seed_to_validating(conn)
    run_validation(conn, REPO, ISSUE, validator=sandbox_with(0), commands=commands())
    approval.apply_action(conn, REPO, ISSUE, ApprovalAction.APPROVE)

    events = {event.event for event in audit.list_events(conn)}
    assert "validation.passed" in events
    assert "approval.approve" in events


def test_happy_path_records_evidence(conn) -> None:
    digest = seed_to_validating(conn)
    run_validation(conn, REPO, ISSUE, validator=sandbox_with(0), commands=commands())

    rows = storage.list_validation_results(conn, REPO, ISSUE, patch_sha256=digest)
    assert rows
    assert any(row["status"] == "passed" for row in rows)
    assert all(row["exit_code"] == 0 for row in rows if row["status"] == "passed")


# ==========================================================================
# Every way a task can be blocked from publishing
# ==========================================================================


def test_blocked_when_validation_never_ran(conn) -> None:
    seed_to_validating(conn)
    with pytest.raises(ApprovalError, match="no validation has been run"):
        approval.apply_action(conn, REPO, ISSUE, ApprovalAction.APPROVE)
    assert storage.get_issue(conn, REPO, ISSUE).task_state is TaskState.VALIDATING


def test_blocked_when_all_checks_skipped(conn) -> None:
    """No isolation means nothing executed, which is not a pass."""
    seed_to_validating(conn)
    run_validation(conn, REPO, ISSUE, validator=sandbox_with(0, isolated=False))

    issue = storage.get_issue(conn, REPO, ISSUE)
    assert issue.task_state is TaskState.FAILED_VALIDATION


def test_blocked_when_a_check_failed(conn) -> None:
    seed_to_validating(conn)
    run_validation(conn, REPO, ISSUE, validator=sandbox_with(1), commands=commands())
    assert storage.get_issue(conn, REPO, ISSUE).task_state is TaskState.FAILED_VALIDATION


def test_blocked_approve_from_failed_validation(conn) -> None:
    seed_to_validating(conn)
    run_validation(conn, REPO, ISSUE, validator=sandbox_with(1), commands=commands())
    with pytest.raises(ApprovalError, match="validation failed"):
        approval.apply_action(conn, REPO, ISSUE, ApprovalAction.APPROVE)


def test_blocked_when_evidence_is_for_another_patch(conn) -> None:
    seed_to_validating(conn)
    # Validation ran, but against a different patch hash.
    other = DIFF + "+# later\n"
    storage.record_validation_report(
        conn,
        ValidationReport(
            repo_full_name=REPO,
            issue_number=ISSUE,
            patch_sha256=compute_patch_sha256(other),
            isolated=True,
            checks=[
                ValidationCheck(
                    kind=CheckKind.TESTS,
                    name="pytest",
                    command=["python", "-m", "pytest"],
                    status=CheckStatus.PASSED,
                    exit_code=0,
                )
            ],
        ),
    )
    with pytest.raises(ApprovalError, match="no validation has been run"):
        approval.apply_action(conn, REPO, ISSUE, ApprovalAction.APPROVE)


def test_a_patch_modified_after_validation_is_blocked(conn) -> None:
    """The core SPEC 8 guarantee, tested through the gate rather than the model."""
    digest = seed_to_validating(conn)
    run_validation(conn, REPO, ISSUE, validator=sandbox_with(0), commands=commands())
    assert storage.validation_passed_for_patch(conn, REPO, ISSUE, digest) is True

    # The agent produces a new patch under the same issue.
    tweaked = DIFF + "+    return None\n"
    new_digest = compute_patch_sha256(tweaked)
    storage.record_patch(
        conn,
        PatchRecord(
            repo_full_name=REPO,
            issue_number=ISSUE,
            provider="mock",
            patch_sha256=new_digest,
            diff=tweaked,
        ),
    )

    # The old evidence no longer applies to the latest patch.
    with pytest.raises(ApprovalError):
        approval.apply_action(conn, REPO, ISSUE, ApprovalAction.APPROVE)


def test_blocked_approve_writes_nothing(conn) -> None:
    seed_to_validating(conn)
    with pytest.raises(ApprovalError):
        approval.apply_action(conn, REPO, ISSUE, ApprovalAction.APPROVE)
    assert approval.live_approval(conn, REPO, ISSUE) is None
    assert audit.list_events(conn, event="approval.approve") == []


def test_gate_fails_closed_on_a_missing_patch(conn) -> None:
    storage.upsert_repository(conn, Repository(full_name=REPO, stars=1))
    storage.upsert_issue(conn, Issue(repo_full_name=REPO, number=ISSUE, title="t", body="b"))
    with pytest.raises(ApprovalError, match="no patch recorded"):
        approval.apply_action(conn, REPO, ISSUE, ApprovalAction.APPROVE)


# ==========================================================================
# Exceptions
# ==========================================================================


def test_exception_unblocks_a_failed_validation(conn) -> None:
    digest = seed_to_validating(conn)
    run_validation(conn, REPO, ISSUE, validator=sandbox_with(1), commands=commands())

    storage.record_validation_exception(
        conn,
        REPO,
        ISSUE,
        patch_sha256=digest,
        authorized_by="manyashre",
        reason="flaky upstream test",
    )
    ok, reason = require_validation_or_exception(conn, REPO, ISSUE, patch_sha256=digest)
    assert ok is True
    assert "manyashre" in reason


def test_exception_bound_to_patch_does_not_cover_a_new_patch(conn) -> None:
    digest = seed_to_validating(conn)
    storage.record_validation_exception(
        conn, REPO, ISSUE, patch_sha256=digest, authorized_by="x", reason="y"
    )
    assert storage.validation_exception_for_patch(conn, REPO, ISSUE, "deadbeef" * 8) is None


# ==========================================================================
# Non-approve actions keep working without evidence
# ==========================================================================


def test_request_changes_loops_back_without_evidence(conn) -> None:
    seed_to_validating(conn)
    _, state = approval.apply_action(conn, REPO, ISSUE, ApprovalAction.REQUEST_CHANGES)
    assert state is TaskState.IMPLEMENTING


def test_reject_and_discard_need_no_evidence(conn) -> None:
    seed_to_validating(conn)
    _, state = approval.apply_action(conn, REPO, ISSUE, ApprovalAction.DISCARD)
    assert state is TaskState.DISCARDED


def test_repair_loop_is_capped_by_spec(conn) -> None:
    """SPEC 7 limits automated repair to two attempts after the initial one."""
    from forgeflow.validation import MAX_REPAIR_ATTEMPTS, can_repair

    seed_to_validating(conn)
    outcome = run_validation(
        conn,
        REPO,
        ISSUE,
        validator=sandbox_with(1),
        commands=commands(),
        repair_attempts=MAX_REPAIR_ATTEMPTS,
    )
    assert outcome.report.repair_attempts == MAX_REPAIR_ATTEMPTS
    assert can_repair(outcome.report) is False


def test_skipped_evidence_is_stored_but_never_counts_as_a_pass(conn) -> None:
    """A no-isolation run records its skipped checks and still fails."""
    digest = seed_to_validating(conn)
    outcome = run_validation(conn, REPO, ISSUE, validator=sandbox_with(0, isolated=False))

    assert outcome.passed is False
    assert storage.validation_passed_for_patch(conn, REPO, ISSUE, digest) is False

    rows = storage.list_validation_results(conn, REPO, ISSUE, patch_sha256=digest)
    assert rows
    assert all(row["status"] == "skipped" for row in rows)
    assert all(row["skip_reason"] for row in rows)
