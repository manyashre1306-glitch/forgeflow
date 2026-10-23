"""Tests for the validate step and its CLI wiring (SPEC sections 7 and 10).

The validator itself is covered in tests/test_validation.py. These tests cover
the pipeline integration: which state the task ends in, that a failure blocks
advancing, and that the guard refuses to run from the wrong state.
"""

from __future__ import annotations

import pytest

from forgeflow import storage
from forgeflow.models import (
    CheckStatus,
    Issue,
    PatchRecord,
    Repository,
    TaskState,
    ValidationCheck,
    ValidationReport,
)
from forgeflow.patch import compute_patch_sha256
from forgeflow.pipeline import PipelineError, run_validation
from forgeflow.sandbox import (
    IsolationKind,
    IsolationStatus,
    Sandbox,
    SandboxResult,
)
from forgeflow.validation import ValidationCommands, Validator

REPO = "acme/widget"
DIFF = "diff --git a/p.py b/p.py\n--- a/p.py\n+++ b/p.py\n@@ -1 +1,2 @@\n def f():\n+    pass\n"

ISOLATED = IsolationStatus(IsolationKind.DOCKER, True, "docker daemon reachable")
NO_ISOLATION = IsolationStatus(IsolationKind.NONE, False, "no runtime")


@pytest.fixture
def conn(tmp_path):
    connection = storage.connect(tmp_path / "t.db")
    storage.init_db(connection)
    yield connection
    connection.close()


def seed(conn, state: TaskState = TaskState.VALIDATING, diff: str = DIFF) -> None:
    storage.upsert_repository(conn, Repository(full_name=REPO, stars=10))
    storage.upsert_issue(conn, Issue(repo_full_name=REPO, number=7, title="Bug", body="x"))
    for target in (
        TaskState.SHORTLISTED,
        TaskState.ANALYZED,
        TaskState.AWAITING_PLAN_APPROVAL,
        TaskState.IMPLEMENTING,
        TaskState.VALIDATING,
    ):
        storage.transition_task_state(conn, REPO, 7, target)
        if target is state:
            break
    storage.record_patch(
        conn,
        PatchRecord(
            repo_full_name=REPO,
            issue_number=7,
            provider="mock",
            patch_sha256=compute_patch_sha256(diff),
            diff=diff,
            files_changed=1,
            additions=1,
            deletions=0,
        ),
    )


def isolated_validator(exit_code: int) -> Validator:
    """A validator whose sandbox reports isolation and replays one exit code.

    Replaces the container runtime, not the validation logic: the Validator
    still builds commands, interprets the exit code, and classifies the result.
    """
    sandbox = Sandbox(status=ISOLATED)

    def fake_run(command, *, workspace, limits=None):
        return SandboxResult(
            command=list(command),
            exit_code=exit_code,
            stdout="ok",
            stderr="",
            duration_seconds=0.1,
            isolated=True,
        )

    sandbox.run = fake_run  # type: ignore[method-assign]
    return Validator(sandbox)


# --------------------------------------------------------------------------
# State outcomes
# --------------------------------------------------------------------------


def test_passing_validation_keeps_task_at_validating(conn) -> None:
    seed(conn)
    outcome = run_validation(
        conn,
        REPO,
        7,
        validator=isolated_validator(0),
        commands=ValidationCommands(tests=["python", "-m", "pytest"]),
    )
    assert outcome.passed is True
    assert outcome.issue.task_state is TaskState.VALIDATING


def test_failing_check_moves_task_to_failed_validation(conn) -> None:
    seed(conn)
    outcome = run_validation(
        conn,
        REPO,
        7,
        validator=isolated_validator(1),
        commands=ValidationCommands(tests=["python", "-m", "pytest"]),
    )
    assert outcome.passed is False
    assert outcome.issue.task_state is TaskState.FAILED_VALIDATION


def test_failed_state_is_persisted(conn) -> None:
    seed(conn)
    run_validation(
        conn,
        REPO,
        7,
        validator=isolated_validator(1),
        commands=ValidationCommands(tests=["python", "-m", "pytest"]),
    )
    assert storage.get_issue(conn, REPO, 7).task_state is TaskState.FAILED_VALIDATION


def test_no_isolation_fails_validation(conn) -> None:
    """A report that ran nothing must not advance the task."""
    seed(conn)
    outcome = run_validation(
        conn,
        REPO,
        7,
        validator=Validator(Sandbox(status=NO_ISOLATION)),
    )
    assert outcome.passed is False
    assert outcome.issue.task_state is TaskState.FAILED_VALIDATION


def test_no_isolation_records_the_limitation(conn) -> None:
    seed(conn)
    outcome = run_validation(
        conn,
        REPO,
        7,
        validator=Validator(Sandbox(status=NO_ISOLATION)),
    )
    assert outcome.report.environment_limitations


def test_blocking_finding_fails_validation(conn) -> None:
    """A test-weakening diff must fail even when every command would pass."""
    seed(conn, diff=DIFF.replace("+    pass", "+pass"))
    weak = (
        "diff --git a/tests/test_a.py b/tests/test_a.py\n"
        "--- a/tests/test_a.py\n"
        "+++ b/tests/test_a.py\n"
        "@@ -1,2 +1 @@\n"
        "-def test_x():\n"
        "-    assert 1\n"
        "+pass\n"
    )
    storage.upsert_issue(conn, Issue(repo_full_name=REPO, number=8, title="t", body="b"))
    for target in (
        TaskState.SHORTLISTED,
        TaskState.ANALYZED,
        TaskState.AWAITING_PLAN_APPROVAL,
        TaskState.IMPLEMENTING,
        TaskState.VALIDATING,
    ):
        storage.transition_task_state(conn, REPO, 8, target)
    storage.record_patch(
        conn,
        PatchRecord(
            repo_full_name=REPO,
            issue_number=8,
            provider="mock",
            patch_sha256=compute_patch_sha256(weak),
            diff=weak,
        ),
    )

    outcome = run_validation(
        conn,
        REPO,
        8,
        validator=isolated_validator(0),
        commands=ValidationCommands(tests=["python", "-m", "pytest"]),
    )
    assert outcome.passed is False
    assert outcome.issue.task_state is TaskState.FAILED_VALIDATION


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------


def test_wrong_state_is_refused(conn) -> None:
    seed(conn, state=TaskState.IMPLEMENTING)
    with pytest.raises(PipelineError, match="expected 'validating'"):
        run_validation(conn, REPO, 7, validator=isolated_validator(0))


def test_awaiting_plan_approval_is_refused(conn) -> None:
    seed(conn, state=TaskState.AWAITING_PLAN_APPROVAL)
    with pytest.raises(PipelineError, match="expected 'validating'"):
        run_validation(conn, REPO, 7, validator=isolated_validator(0))


def test_missing_issue_is_refused(conn) -> None:
    with pytest.raises(PipelineError, match="not in the database"):
        run_validation(conn, "no/such", 1, validator=isolated_validator(0))


def test_missing_patch_is_refused(conn) -> None:
    storage.upsert_repository(conn, Repository(full_name=REPO, stars=1))
    storage.upsert_issue(conn, Issue(repo_full_name=REPO, number=7, title="t", body="b"))
    for target in (
        TaskState.SHORTLISTED,
        TaskState.ANALYZED,
        TaskState.AWAITING_PLAN_APPROVAL,
        TaskState.IMPLEMENTING,
        TaskState.VALIDATING,
    ):
        storage.transition_task_state(conn, REPO, 7, target)
    with pytest.raises(PipelineError, match="no patch recorded"):
        run_validation(conn, REPO, 7, validator=isolated_validator(0))


def test_validation_uses_the_stored_patch(conn) -> None:
    """The report must be bound to the stored patch hash, not a fresh one."""
    seed(conn)
    outcome = run_validation(
        conn,
        REPO,
        7,
        validator=isolated_validator(0),
        commands=ValidationCommands(tests=["python", "-m", "pytest"]),
    )
    assert outcome.report.patch_sha256 == compute_patch_sha256(DIFF)


# --------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------


def test_passing_validation_is_audited(conn) -> None:
    from forgeflow import audit

    seed(conn)
    run_validation(
        conn,
        REPO,
        7,
        validator=isolated_validator(0),
        commands=ValidationCommands(tests=["python", "-m", "pytest"]),
    )
    events = audit.list_events(conn, event="validation.passed")
    assert events
    assert events[0].repo_full_name == REPO


def test_failing_validation_is_audited(conn) -> None:
    from forgeflow import audit

    seed(conn)
    run_validation(
        conn,
        REPO,
        7,
        validator=isolated_validator(1),
        commands=ValidationCommands(tests=["python", "-m", "pytest"]),
    )
    events = audit.list_events(conn, event="validation.failed")
    assert events
    assert events[0].severity == "warning"


def test_audit_records_the_patch_hash(conn) -> None:
    from forgeflow import audit

    seed(conn)
    run_validation(
        conn,
        REPO,
        7,
        validator=isolated_validator(1),
        commands=ValidationCommands(tests=["python", "-m", "pytest"]),
    )
    events = audit.list_events(conn, event="validation.failed")
    assert compute_patch_sha256(DIFF) in events[0].payload_json


# --------------------------------------------------------------------------
# The report is the same object the Validator produced
# --------------------------------------------------------------------------


def test_report_contains_every_planned_check(conn) -> None:
    seed(conn)
    outcome = run_validation(
        conn,
        REPO,
        7,
        validator=isolated_validator(0),
        commands=ValidationCommands(tests=["python", "-m", "pytest"], lint=["ruff", "check", "."]),
    )
    kinds = {check.kind.value for check in outcome.report.checks}
    assert {"tests", "lint", "format", "types"} <= kinds


def test_passing_report_has_no_skipped_checks_with_commands(conn) -> None:
    seed(conn)
    outcome = run_validation(
        conn,
        REPO,
        7,
        validator=isolated_validator(0),
        commands=ValidationCommands(tests=["python", "-m", "pytest"]),
    )
    applicable = [c for c in outcome.report.checks if c.command]
    assert all(c.status is CheckStatus.PASSED for c in applicable)


def test_validation_report_is_serialisable(conn) -> None:
    seed(conn)
    outcome = run_validation(
        conn,
        REPO,
        7,
        validator=isolated_validator(0),
        commands=ValidationCommands(tests=["python", "-m", "pytest"]),
    )
    restored = ValidationReport.model_validate_json(outcome.report.model_dump_json())
    assert restored.repo_full_name == REPO


def test_repair_attempts_are_recorded(conn) -> None:
    seed(conn)
    outcome = run_validation(
        conn,
        REPO,
        7,
        validator=isolated_validator(1),
        commands=ValidationCommands(tests=["python", "-m", "pytest"]),
        repair_attempts=2,
    )
    assert outcome.report.repair_attempts == 2


def test_check_model_carries_evidence(conn) -> None:
    check = ValidationCheck(
        kind="tests",
        name="pytest",
        command=["python", "-m", "pytest"],
        status=CheckStatus.PASSED,
        exit_code=0,
        output="1 passed",
    )
    assert check.passed is True
    assert check.exit_code == 0
    assert check.output
