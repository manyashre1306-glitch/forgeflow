"""Tests for the approval boundary (SPEC sections 8, 10, and 14).

The load-bearing tests here are the ones SPEC section 14 names: a modified patch
invalidates approval, and approval is bound to repository and branch as well as
hash. No network, no container runtime.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from forgeflow import approval, storage
from forgeflow.approval import ApprovalError
from forgeflow.models import (
    ApprovalAction,
    IllegalTransitionError,
    Issue,
    PatchRecord,
    Repository,
    TaskState,
)
from forgeflow.patch import compute_patch_sha256

REPO = "acme/widget"
BRANCH = "main"

DIFF = (
    "diff --git a/widget/parser.py b/widget/parser.py\n"
    "--- a/widget/parser.py\n"
    "+++ b/widget/parser.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def parse(v):\n"
    "+    # guard added\n"
    "     return int(v)\n"
)

OTHER_DIFF = DIFF.replace("guard added", "different change entirely")


@pytest.fixture
def conn(tmp_path):
    connection = storage.connect(tmp_path / "t.db")
    storage.init_db(connection)
    yield connection
    connection.close()


def seed(conn, state: TaskState = TaskState.VALIDATING, diff: str = DIFF) -> PatchRecord:
    """Seed a repository, issue, and patch, then move to the requested state."""
    storage.upsert_repository(conn, Repository(full_name=REPO, stars=10))
    storage.upsert_issue(
        conn, Issue(repo_full_name=REPO, number=7, title="Bug: parser fails", body="x")
    )
    patch = PatchRecord(
        repo_full_name=REPO,
        issue_number=7,
        provider="mock",
        patch_sha256=compute_patch_sha256(diff),
        diff=diff,
        files_changed=1,
        additions=1,
        deletions=0,
    )
    storage.record_patch(conn, patch)

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
    return patch


def store_passing_validation(conn, patch_sha256: str, number: int = 7) -> None:
    """Record one passing check, which the approval gate requires.

    Approval is gated on execution evidence (SPEC 7), so a test that approves
    must first establish that a check ran and passed for the patch.
    """
    from forgeflow.models import CheckKind, CheckStatus, ValidationCheck, ValidationReport

    storage.record_validation_report(
        conn,
        ValidationReport(
            repo_full_name=REPO,
            issue_number=number,
            patch_sha256=patch_sha256,
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


# --------------------------------------------------------------------------
# The core guarantee: approval is bound to the exact patch
# --------------------------------------------------------------------------


def test_approve_then_verify_succeeds(conn) -> None:
    seed(conn)
    approval.record_decision(
        conn,
        REPO,
        7,
        ApprovalAction.APPROVE,
        patch_sha256=compute_patch_sha256(DIFF),
        target_branch=BRANCH,
    )
    ok, reason = approval.check_approval(conn, REPO, 7, diff=DIFF, target_branch=BRANCH)
    assert ok is True
    assert reason == ""


def test_modified_patch_invalidates_approval(conn) -> None:
    """SPEC 14: a modified patch must invalidate approval."""
    seed(conn)
    approval.record_decision(
        conn,
        REPO,
        7,
        ApprovalAction.APPROVE,
        patch_sha256=compute_patch_sha256(DIFF),
        target_branch=BRANCH,
    )
    ok, reason = approval.check_approval(conn, REPO, 7, diff=OTHER_DIFF, target_branch=BRANCH)
    assert ok is False
    assert "patch changed since approval" in reason


def test_require_approval_raises_on_modified_patch(conn) -> None:
    seed(conn)
    approval.record_decision(
        conn,
        REPO,
        7,
        ApprovalAction.APPROVE,
        patch_sha256=compute_patch_sha256(DIFF),
        target_branch=BRANCH,
    )
    with pytest.raises(ApprovalError, match="patch changed"):
        approval.require_approval(conn, REPO, 7, diff=OTHER_DIFF, target_branch=BRANCH)


def test_single_byte_change_invalidates_approval(conn) -> None:
    """The hash is over exact bytes, so a whitespace change counts."""
    seed(conn)
    approval.record_decision(
        conn,
        REPO,
        7,
        ApprovalAction.APPROVE,
        patch_sha256=compute_patch_sha256(DIFF),
        target_branch=BRANCH,
    )
    ok, _ = approval.check_approval(conn, REPO, 7, diff=DIFF + "\n", target_branch=BRANCH)
    assert ok is False


def test_branch_mismatch_invalidates_approval(conn) -> None:
    seed(conn)
    approval.record_decision(
        conn,
        REPO,
        7,
        ApprovalAction.APPROVE,
        patch_sha256=compute_patch_sha256(DIFF),
        target_branch=BRANCH,
    )
    ok, reason = approval.check_approval(conn, REPO, 7, diff=DIFF, target_branch="develop")
    assert ok is False
    assert "branch" in reason


def test_repository_mismatch_invalidates_approval(conn) -> None:
    """An approval for one repository must not authorize another."""
    seed(conn)
    approval.record_decision(
        conn,
        REPO,
        7,
        ApprovalAction.APPROVE,
        patch_sha256=compute_patch_sha256(DIFF),
        target_branch=BRANCH,
    )
    ok, reason = approval.check_approval(conn, "other/project", 7, diff=DIFF, target_branch=BRANCH)
    assert ok is False
    assert "other/project" in reason


def test_no_approval_refuses(conn) -> None:
    seed(conn)
    ok, reason = approval.check_approval(conn, REPO, 7, diff=DIFF, target_branch=BRANCH)
    assert ok is False
    assert "no live approval" in reason


def test_empty_diff_refuses(conn) -> None:
    seed(conn)
    ok, reason = approval.check_approval(conn, REPO, 7, diff="", target_branch=BRANCH)
    assert ok is False
    assert "no patch diff" in reason


# --------------------------------------------------------------------------
# The four actions
# --------------------------------------------------------------------------


def test_request_changes_is_not_approval(conn) -> None:
    seed(conn)
    approval.record_decision(
        conn,
        REPO,
        7,
        ApprovalAction.REQUEST_CHANGES,
        patch_sha256=compute_patch_sha256(DIFF),
        target_branch=BRANCH,
    )
    ok, reason = approval.check_approval(conn, REPO, 7, diff=DIFF, target_branch=BRANCH)
    assert ok is False
    assert "request_changes" in reason


def test_reject_is_not_approval(conn) -> None:
    seed(conn)
    approval.record_decision(
        conn,
        REPO,
        7,
        ApprovalAction.REJECT,
        patch_sha256=compute_patch_sha256(DIFF),
        target_branch=BRANCH,
    )
    ok, _ = approval.check_approval(conn, REPO, 7, diff=DIFF, target_branch=BRANCH)
    assert ok is False


def test_a_newer_decision_supersedes_an_older_approval(conn) -> None:
    """Approving then requesting changes must revoke the authorization.

    ``live_approval`` returns the newest live decision, which is now the
    request_changes row. The older approve row is superseded, and the gate
    refuses because the newest decision is not an approval.
    """
    seed(conn)
    approval.record_decision(
        conn,
        REPO,
        7,
        ApprovalAction.APPROVE,
        patch_sha256=compute_patch_sha256(DIFF),
        target_branch=BRANCH,
    )
    approval.record_decision(
        conn,
        REPO,
        7,
        ApprovalAction.REQUEST_CHANGES,
        patch_sha256=compute_patch_sha256(DIFF),
        target_branch=BRANCH,
    )

    live = approval.live_approval(conn, REPO, 7)
    assert live is not None
    assert live.action is ApprovalAction.REQUEST_CHANGES

    history = approval.list_decisions(conn, REPO, 7)
    assert history[0].action is ApprovalAction.APPROVE
    assert history[0].superseded is True

    ok, reason = approval.check_approval(conn, REPO, 7, diff=DIFF, target_branch=BRANCH)
    assert ok is False
    assert "request_changes" in reason


def test_reapproval_after_changes_restores_authorization(conn) -> None:
    seed(conn)
    approval.record_decision(
        conn,
        REPO,
        7,
        ApprovalAction.REQUEST_CHANGES,
        patch_sha256=compute_patch_sha256(DIFF),
        target_branch=BRANCH,
    )
    approval.record_decision(
        conn,
        REPO,
        7,
        ApprovalAction.APPROVE,
        patch_sha256=compute_patch_sha256(DIFF),
        target_branch=BRANCH,
    )
    ok, _ = approval.check_approval(conn, REPO, 7, diff=DIFF, target_branch=BRANCH)
    assert ok is True


def test_decision_history_is_preserved(conn) -> None:
    """Superseding marks rows, so an auditor can see the sequence."""
    seed(conn)
    approval.record_decision(
        conn, REPO, 7, ApprovalAction.APPROVE, patch_sha256=compute_patch_sha256(DIFF)
    )
    approval.record_decision(
        conn, REPO, 7, ApprovalAction.REJECT, patch_sha256=compute_patch_sha256(DIFF)
    )
    history = approval.list_decisions(conn, REPO, 7)
    assert len(history) == 2
    assert history[0].superseded is True
    assert history[1].superseded is False


def test_at_most_one_live_decision(conn) -> None:
    seed(conn)
    for _ in range(3):
        approval.record_decision(
            conn, REPO, 7, ApprovalAction.APPROVE, patch_sha256=compute_patch_sha256(DIFF)
        )
    rows = conn.execute("SELECT COUNT(*) AS n FROM approvals WHERE superseded = 0").fetchone()
    assert rows["n"] == 1


# --------------------------------------------------------------------------
# Actions move state, and illegal moves are refused
# --------------------------------------------------------------------------


def test_approve_moves_to_awaiting_publish_approval(conn) -> None:
    seed(conn, state=TaskState.VALIDATING)
    store_passing_validation(conn, compute_patch_sha256(DIFF))
    _, state = approval.apply_action(conn, REPO, 7, ApprovalAction.APPROVE)
    assert state is TaskState.AWAITING_PUBLISH_APPROVAL


def test_request_changes_moves_to_implementing(conn) -> None:
    seed(conn, state=TaskState.VALIDATING)
    _, state = approval.apply_action(conn, REPO, 7, ApprovalAction.REQUEST_CHANGES)
    assert state is TaskState.IMPLEMENTING


def test_reject_moves_to_discarded(conn) -> None:
    seed(conn, state=TaskState.VALIDATING)
    _, state = approval.apply_action(conn, REPO, 7, ApprovalAction.REJECT)
    assert state is TaskState.DISCARDED


def test_discard_moves_to_discarded(conn) -> None:
    seed(conn, state=TaskState.VALIDATING)
    _, state = approval.apply_action(conn, REPO, 7, ApprovalAction.DISCARD)
    assert state is TaskState.DISCARDED


def test_action_without_a_patch_refuses(conn) -> None:
    storage.upsert_repository(conn, Repository(full_name=REPO, stars=1))
    storage.upsert_issue(conn, Issue(repo_full_name=REPO, number=7, title="t", body="b"))
    with pytest.raises(ApprovalError, match="no patch recorded"):
        approval.apply_action(conn, REPO, 7, ApprovalAction.APPROVE)


def test_illegal_transition_is_refused(conn) -> None:
    """Discarding an already-discarded task must raise, not silently pass."""
    seed(conn, state=TaskState.VALIDATING)
    approval.apply_action(conn, REPO, 7, ApprovalAction.DISCARD)
    with pytest.raises(IllegalTransitionError):
        approval.apply_action(conn, REPO, 7, ApprovalAction.APPROVE)


def test_cannot_publish_without_approval_state(conn) -> None:
    """A validating task has no approval, so the gate must refuse."""
    seed(conn, state=TaskState.VALIDATING)
    ok, _ = approval.check_approval(conn, REPO, 7, diff=DIFF, target_branch=BRANCH)
    assert ok is False


# --------------------------------------------------------------------------
# Approval writes to the audit log
# --------------------------------------------------------------------------


def test_approval_is_audited(conn) -> None:
    from forgeflow import audit

    seed(conn)
    approval.record_decision(
        conn,
        REPO,
        7,
        ApprovalAction.APPROVE,
        patch_sha256=compute_patch_sha256(DIFF),
        actor="manyashre",
    )
    events = audit.list_events(conn, event="approval.approve")
    assert events
    assert events[0].actor == "manyashre"


def test_approval_audit_records_the_hash(conn) -> None:
    from forgeflow import audit

    seed(conn)
    digest = compute_patch_sha256(DIFF)
    approval.record_decision(conn, REPO, 7, ApprovalAction.APPROVE, patch_sha256=digest)
    events = audit.list_events(conn, event="approval.approve")
    assert digest in events[0].payload_json


# --------------------------------------------------------------------------
# Credential separation (SPEC section 8)
# --------------------------------------------------------------------------


def test_approval_module_holds_no_credential() -> None:
    """Approval must not be able to reach a network or a secret."""
    source = Path(approval.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    forbidden = ("requests", "urllib", "httpx", "github_client", "PyGithub")
    for name in imported:
        assert not any(bad in name for bad in forbidden), f"approval imports {name}"


def test_approval_module_never_reads_a_token() -> None:
    source = Path(approval.__file__).read_text(encoding="utf-8")
    for marker in ("GITHUB_TOKEN", "get_secret_value", "GEMINI_API_KEY"):
        assert marker not in source


def test_discovery_module_has_no_embedded_credential() -> None:
    """Discovery takes an injected client; it must not read a token itself."""
    from forgeflow import discovery

    source = Path(discovery.__file__).read_text(encoding="utf-8")
    assert "get_secret_value" not in source
    assert "os.environ" not in source


def test_agent_runner_has_no_github_credential() -> None:
    """The coding agent must not hold a publishing credential."""
    from forgeflow import agent_runner

    source = Path(agent_runner.__file__).read_text(encoding="utf-8")
    assert "GITHUB_TOKEN" not in source
    assert "github_client" not in source


def test_no_module_in_the_package_creates_a_pull_request() -> None:
    """Phase 3 must contain no external write action anywhere."""
    package = Path(approval.__file__).parent
    offenders: list[str] = []
    for path in sorted(package.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for marker in (
            "create_pull_request",
            "create_pull(",
            "requests.post",
            "requests.put",
            "requests.patch",
            "requests.delete",
        ):
            if marker in text:
                offenders.append(f"{path.name}: {marker}")
    assert offenders == []
