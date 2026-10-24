"""Tests for the ai-policy-override CLI command (SPEC sections 8 and 9).

A detected AI-use restriction blocks publishing. The only way past it is an
explicit, documented override bound to the exact patch hash. These tests prove
the command records that override, refuses without a reason, and writes an audit
event rather than allowing a silent bypass.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from forgeflow import audit, storage
from forgeflow.cli import app
from forgeflow.models import (
    AIPolicy,
    ContributionRules,
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
            "ai-policy-override",
            "acme/widget#7",
            "--db",
            str(db_path),
            "--config",
            str(config_path),
            *args,
        ],
    )


def test_override_is_recorded(db_path, config_path) -> None:
    result = invoke(db_path, config_path, "--actor", "manyashre", "--reason", "disclosed AI use")
    assert result.exit_code == 0, result.output

    conn = storage.connect(db_path)
    found = storage.ai_policy_override_for_patch(conn, REPO, 7, HASH)
    conn.close()
    assert found is not None
    assert found["authorized_by"] == "manyashre"
    assert found["reason"] == "disclosed AI use"


def test_override_reports_the_patch_hash(db_path, config_path) -> None:
    result = invoke(db_path, config_path, "--reason", "disclosed")
    assert HASH in result.output


def test_missing_reason_is_refused(db_path, config_path) -> None:
    result = invoke(db_path, config_path, "--actor", "manyashre")
    assert result.exit_code == 1
    assert "reason is required" in result.output


def test_blank_reason_is_refused(db_path, config_path) -> None:
    result = invoke(db_path, config_path, "--reason", "   ")
    assert result.exit_code == 1
    assert "reason is required" in result.output


def test_refused_override_is_not_recorded(db_path, config_path) -> None:
    invoke(db_path, config_path)
    conn = storage.connect(db_path)
    found = storage.ai_policy_override_for_patch(conn, REPO, 7, HASH)
    conn.close()
    assert found is None


def test_override_writes_an_audit_event(db_path, config_path) -> None:
    invoke(db_path, config_path, "--actor", "manyashre", "--reason", "disclosed AI use")

    conn = storage.connect(db_path)
    events = audit.list_events(conn, event="ai_policy.override_recorded")
    conn.close()
    assert len(events) == 1
    assert events[0].actor == "manyashre"
    assert events[0].severity == "warning"
    assert "disclosed AI use" in events[0].detail


def test_override_does_not_unlock_a_different_patch(db_path, config_path) -> None:
    invoke(db_path, config_path, "--reason", "disclosed")

    conn = storage.connect(db_path)
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
    found = storage.ai_policy_override_for_patch(conn, REPO, 7, compute_patch_sha256(other))
    conn.close()
    assert found is None


def test_missing_patch_is_refused(db_path, config_path) -> None:
    conn = storage.connect(db_path)
    storage.upsert_issue(conn, Issue(repo_full_name=REPO, number=99, title="t", body="b"))
    conn.close()

    result = runner.invoke(
        app,
        [
            "ai-policy-override",
            "acme/widget#99",
            "--db",
            str(db_path),
            "--config",
            str(config_path),
            "--reason",
            "disclosed",
        ],
    )
    assert result.exit_code == 1
    assert "no patch recorded" in result.output


def test_command_appears_in_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert "ai-policy-override" in result.output


def test_override_is_bound_to_the_hash_not_the_repo(db_path, config_path) -> None:
    """A recorded override must carry the patch hash, so it cannot be reused."""
    invoke(db_path, config_path, "--reason", "disclosed")
    conn = storage.connect(db_path)
    found = storage.ai_policy_override_for_patch(conn, REPO, 7, HASH)
    conn.close()
    assert found is not None
    assert found["patch_sha256"] == HASH


def test_restricted_rules_are_a_prerequisite_for_the_block(db_path, config_path) -> None:
    """Sanity: the stored rules, not the override command, are what block."""
    conn = storage.connect(db_path)
    storage.record_contribution_rules(
        conn,
        REPO,
        7,
        ContributionRules(ai_policy=AIPolicy.PROHIBITED),
    )
    rules = storage.latest_contribution_rules(conn, REPO, 7)
    conn.close()
    assert rules is not None and rules.is_ai_restricted
