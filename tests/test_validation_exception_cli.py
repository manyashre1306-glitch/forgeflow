"""Tests for the validation-exception CLI command (SPEC sections 7 and 9).

An exception is the only documented way to publish past a failing check, so it
must be explicit (a reason is required), recorded in the audit log, and bound to
the exact patch.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from forgeflow import audit, storage
from forgeflow.cli import app
from forgeflow.models import (
    ApprovalAction,
    Issue,
    PatchRecord,
    Repository,
    TaskState,
)
from forgeflow.patch import compute_patch_sha256

REPO = "acme/widget"
DIFF = "diff --git a/p.py b/p.py\n--- a/p.py\n+++ b/p.py\n@@ -1 +1,2 @@\n def f():\n+    pass\n"
HASH = compute_patch_sha256(DIFF)

runner = CliRunner()


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "ff.db"
    connection = storage.connect(path)
    storage.init_db(connection)
    storage.upsert_repository(connection, Repository(full_name=REPO, stars=1))
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
    connection.close()
    return path


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "repos.yaml"
    path.write_text("repositories:\n  - acme/widget\n", encoding="utf-8")
    return path


def invoke(db_path, config_path, *args):
    return runner.invoke(
        app,
        [
            "validation-exception",
            "acme/widget#7",
            "--db",
            str(db_path),
            "--config",
            str(config_path),
            *args,
        ],
    )


# --------------------------------------------------------------------------
# The command records an exception
# --------------------------------------------------------------------------


def test_exception_is_recorded(db_path, config_path) -> None:
    result = invoke(db_path, config_path, "--actor", "manyashre", "--reason", "flaky upstream")
    assert result.exit_code == 0, result.output

    conn = storage.connect(db_path)
    found = storage.validation_exception_for_patch(conn, REPO, 7, HASH)
    conn.close()
    assert found is not None
    assert found["authorized_by"] == "manyashre"
    assert found["reason"] == "flaky upstream"


def test_exception_reports_the_patch_hash(db_path, config_path) -> None:
    result = invoke(db_path, config_path, "--reason", "flaky")
    assert HASH in result.output


# --------------------------------------------------------------------------
# A reason is mandatory
# --------------------------------------------------------------------------


def test_missing_reason_is_refused(db_path, config_path) -> None:
    result = invoke(db_path, config_path, "--actor", "manyashre")
    assert result.exit_code == 1
    assert "reason is required" in result.output


def test_blank_reason_is_refused(db_path, config_path) -> None:
    result = invoke(db_path, config_path, "--reason", "   ")
    assert result.exit_code == 1
    assert "reason is required" in result.output


def test_refused_exception_is_not_recorded(db_path, config_path) -> None:
    invoke(db_path, config_path)
    conn = storage.connect(db_path)
    found = storage.validation_exception_for_patch(conn, REPO, 7, HASH)
    conn.close()
    assert found is None


# --------------------------------------------------------------------------
# The audit event
# --------------------------------------------------------------------------


def test_exception_writes_an_audit_event(db_path, config_path) -> None:
    invoke(db_path, config_path, "--actor", "manyashre", "--reason", "flaky upstream")

    conn = storage.connect(db_path)
    events = audit.list_events(conn, event="validation.exception_recorded")
    conn.close()
    assert len(events) == 1
    assert events[0].actor == "manyashre"
    assert events[0].severity == "warning"
    assert "flaky upstream" in events[0].detail


def test_audit_event_records_the_patch_hash(db_path, config_path) -> None:
    invoke(db_path, config_path, "--reason", "flaky upstream")
    conn = storage.connect(db_path)
    events = audit.list_events(conn, event="validation.exception_recorded")
    conn.close()
    assert HASH in events[0].payload_json


def test_no_audit_event_when_refused(db_path, config_path) -> None:
    invoke(db_path, config_path)
    conn = storage.connect(db_path)
    events = audit.list_events(conn, event="validation.exception_recorded")
    conn.close()
    assert events == []


def test_audit_event_reason_is_redacted(db_path, config_path) -> None:
    """A pasted credential in the reason must not reach the audit log."""
    invoke(
        db_path,
        config_path,
        "--reason",
        "see token ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    )
    conn = storage.connect(db_path)
    events = audit.list_events(conn, event="validation.exception_recorded")
    conn.close()
    assert "ghp_" not in events[0].detail


# --------------------------------------------------------------------------
# The exception unlocks approval, and only for that patch
# --------------------------------------------------------------------------


def test_exception_allows_approval(db_path, config_path) -> None:
    from forgeflow import approval

    invoke(db_path, config_path, "--reason", "flaky upstream")
    conn = storage.connect(db_path)
    _, state = approval.apply_action(conn, REPO, 7, ApprovalAction.APPROVE)
    conn.close()
    assert state is TaskState.AWAITING_PUBLISH_APPROVAL


def test_exception_does_not_unlock_a_different_patch(db_path, config_path) -> None:
    from forgeflow import approval
    from forgeflow.approval import ApprovalError

    invoke(db_path, config_path, "--reason", "flaky upstream")

    conn = storage.connect(db_path)
    # Replace the patch with a different one under the same issue.
    other = DIFF + "+# tweaked\n"
    storage.record_patch(
        conn,
        PatchRecord(
            repo_full_name=REPO,
            issue_number=7,
            provider="mock",
            patch_sha256=compute_patch_sha256(other),
            diff=other,
        ),
    )
    with pytest.raises(ApprovalError, match="no validation has been run"):
        approval.apply_action(conn, REPO, 7, ApprovalAction.APPROVE)
    conn.close()


def test_exception_alone_does_not_count_as_a_passing_check(db_path, config_path) -> None:
    invoke(db_path, config_path, "--reason", "flaky upstream")
    conn = storage.connect(db_path)
    assert storage.validation_passed_for_patch(conn, REPO, 7, HASH) is False
    conn.close()


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------


def test_missing_patch_is_refused(db_path, config_path) -> None:
    conn = storage.connect(db_path)
    storage.upsert_issue(conn, Issue(repo_full_name=REPO, number=99, title="t", body="b"))
    conn.close()

    result = runner.invoke(
        app,
        [
            "validation-exception",
            "acme/widget#99",
            "--db",
            str(db_path),
            "--config",
            str(config_path),
            "--reason",
            "flaky",
        ],
    )
    assert result.exit_code == 1
    assert "no patch recorded" in result.output


def test_command_appears_in_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert "validation-exception" in result.output
