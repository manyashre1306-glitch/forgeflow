"""The human approval boundary for ForgeFlow Phase 3 (SPEC section 8).

This module answers one question: does a human's decision authorize publishing
*this exact patch* to *this exact branch* of *this exact repository*? Anything
that changes the patch, the repository, or the branch invalidates the approval.

Two design points carry the security weight:

* **The boundary is a data check, not a convention.** :func:`require_approval`
  recomputes the patch hash from the diff bytes and compares it with the stored
  approval. A caller cannot bypass it by holding a stale record, because the
  hash is derived, not trusted.
* **This module holds no credential and imports no network client.** Approving
  a patch is a local decision. Nothing here can reach GitHub, so approving
  cannot itself cause an external write. The same is true of the discovery and
  coding modules: credentials live in ``config.Settings`` and are passed
  directly to the one client that needs them, so there is no shared credential
  object for other components to reach through.

The module deliberately does *not* publish. Publishing is Phase 4, and its
absence here is what makes "approval cannot publish by itself" true.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from forgeflow import audit, storage
from forgeflow.models import (
    ApprovalAction,
    ApprovalRecord,
    TaskState,
    validate_transition,
)
from forgeflow.patch import compute_patch_sha256

APPROVAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    repo_full_name TEXT NOT NULL,
    issue_number   INTEGER NOT NULL,
    action         TEXT NOT NULL,
    patch_sha256   TEXT NOT NULL,
    target_branch  TEXT NOT NULL DEFAULT '',
    actor          TEXT NOT NULL DEFAULT 'operator',
    reason         TEXT NOT NULL DEFAULT '',
    superseded     INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (repo_full_name, issue_number)
        REFERENCES issues(repo_full_name, number)
);
"""


class ApprovalError(RuntimeError):
    """Raised when an approval action is not permitted."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(APPROVAL_SCHEMA)
    conn.commit()


def _row_to_approval(row: sqlite3.Row) -> ApprovalRecord:
    created = row["created_at"]
    return ApprovalRecord(
        id=row["id"],
        created_at=datetime.fromisoformat(created) if created else None,
        repo_full_name=row["repo_full_name"],
        issue_number=row["issue_number"],
        action=ApprovalAction(row["action"]),
        patch_sha256=row["patch_sha256"],
        target_branch=row["target_branch"],
        actor=row["actor"],
        reason=row["reason"],
        superseded=bool(row["superseded"]),
    )


def _supersede_existing(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
) -> int:
    """Mark every prior approval for this issue superseded. Returns the count.

    Superseding rather than deleting keeps the history: an auditor can see that
    a patch was approved, then changed, then approved again.
    """
    cursor = conn.execute(
        """
        UPDATE approvals SET superseded = 1
        WHERE repo_full_name = ? AND issue_number = ? AND superseded = 0
        """,
        (repo_full_name, issue_number),
    )
    conn.commit()
    return cursor.rowcount


def record_decision(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
    action: ApprovalAction,
    *,
    patch_sha256: str,
    target_branch: str = "",
    actor: str = "operator",
    reason: str = "",
) -> ApprovalRecord:
    """Store a human decision and return it.

    Any earlier live approval for the issue is superseded first, so at most one
    decision is live at a time. A newer decision always wins: there is no way to
    leave an old approval standing by recording a second one.
    """
    superseded_count = _supersede_existing(conn, repo_full_name, issue_number)

    created = datetime.now(UTC)
    cursor = conn.execute(
        """
        INSERT INTO approvals (
            created_at, repo_full_name, issue_number, action,
            patch_sha256, target_branch, actor, reason, superseded
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            created.isoformat(),
            repo_full_name,
            issue_number,
            action.value,
            patch_sha256,
            target_branch,
            actor,
            reason,
        ),
    )
    conn.commit()

    audit.record(
        conn,
        f"approval.{action.value}",
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        actor=actor,
        severity="info" if action is ApprovalAction.APPROVE else "warning",
        detail=reason or f"{action.value} for patch {patch_sha256[:12]}",
        payload={
            "patch_sha256": patch_sha256,
            "target_branch": target_branch,
            "superseded_prior": superseded_count,
        },
    )

    return ApprovalRecord(
        id=int(cursor.lastrowid or 0),
        created_at=created,
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        action=action,
        patch_sha256=patch_sha256,
        target_branch=target_branch,
        actor=actor,
        reason=reason,
        superseded=False,
    )


def latest_decision(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
) -> ApprovalRecord | None:
    """Return the most recent decision, or None if none was recorded."""
    row = conn.execute(
        """
        SELECT * FROM approvals
        WHERE repo_full_name = ? AND issue_number = ?
        ORDER BY id DESC LIMIT 1
        """,
        (repo_full_name, issue_number),
    ).fetchone()
    return _row_to_approval(row) if row else None


def live_approval(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
) -> ApprovalRecord | None:
    """Return the live (not superseded) approval, or None."""
    row = conn.execute(
        """
        SELECT * FROM approvals
        WHERE repo_full_name = ? AND issue_number = ? AND superseded = 0
        ORDER BY id DESC LIMIT 1
        """,
        (repo_full_name, issue_number),
    ).fetchone()
    return _row_to_approval(row) if row else None


def list_decisions(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
) -> list[ApprovalRecord]:
    """Return every decision for an issue, oldest first."""
    rows = conn.execute(
        """
        SELECT * FROM approvals
        WHERE repo_full_name = ? AND issue_number = ?
        ORDER BY id
        """,
        (repo_full_name, issue_number),
    ).fetchall()
    return [_row_to_approval(row) for row in rows]


def check_approval(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
    *,
    diff: str,
    target_branch: str,
) -> tuple[bool, str]:
    """Whether a live approval authorizes publishing this exact patch.

    The patch hash is recomputed from ``diff`` rather than read from the
    database, so an approval cannot be reused for a different patch that happens
    to be stored under the same issue. Returns ``(ok, reason)``.
    """
    if not diff:
        return False, "no patch diff supplied; nothing to approve"

    approval = live_approval(conn, repo_full_name, issue_number)
    if approval is None:
        return False, (
            f"no live approval for {repo_full_name}#{issue_number}; "
            "run 'forgeflow approve' after reviewing the bundle"
        )

    actual = compute_patch_sha256(diff)
    return approval.is_valid_for(
        patch_sha256=actual,
        repo_full_name=repo_full_name,
        target_branch=target_branch,
    )


def require_approval(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
    *,
    diff: str,
    target_branch: str,
) -> ApprovalRecord:
    """Return the live approval, or raise if publishing is not authorized.

    This is the single gate Phase 4 must call immediately before any external
    write. It fails closed on every mismatch.
    """
    ok, reason = check_approval(
        conn,
        repo_full_name,
        issue_number,
        diff=diff,
        target_branch=target_branch,
    )
    if not ok:
        raise ApprovalError(f"refusing to publish: {reason}")

    approval = live_approval(conn, repo_full_name, issue_number)
    assert approval is not None  # check_approval already proved it exists
    return approval


# --- state transitions per action -------------------------------------------

_ACTION_TARGET: dict[ApprovalAction, TaskState] = {
    ApprovalAction.APPROVE: TaskState.AWAITING_PUBLISH_APPROVAL,
    ApprovalAction.REQUEST_CHANGES: TaskState.IMPLEMENTING,
    ApprovalAction.REJECT: TaskState.DISCARDED,
    ApprovalAction.DISCARD: TaskState.DISCARDED,
}


def require_validation_or_exception(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
    *,
    patch_sha256: str,
    from_failed_state: bool = False,
) -> tuple[bool, str]:
    """Whether a task may advance to awaiting_publish_approval.

    Returns ``(ok, reason)``. Passing requires one of two things:

    * a recorded check that actually executed and exited zero for this exact
      patch (``validation_passed_for_patch``), or
    * a documented exception bound to this exact patch.

    Everything else refuses, including the case that matters most: a report
    whose checks were all skipped, which is the state this environment is in
    when no container runtime is available. Skipped is not passed.
    """
    if not from_failed_state and storage.validation_passed_for_patch(
        conn, repo_full_name, issue_number, patch_sha256
    ):
        return True, ""

    exception = storage.validation_exception_for_patch(
        conn, repo_full_name, issue_number, patch_sha256
    )
    if exception is not None:
        return True, f"authorized by exception from {exception['authorized_by']}"

    # The failed state is itself evidence of a failure, so report that even when
    # no rows were kept. Checked before the empty case so the reason names the
    # real problem rather than claiming validation never ran.
    if from_failed_state or storage.validation_failed_for_patch(
        conn, repo_full_name, issue_number, patch_sha256
    ):
        return False, (
            "validation failed for this patch; fix it and re-validate, or record "
            "an exception with 'forgeflow validation-exception'"
        )

    rows = storage.list_validation_results(
        conn, repo_full_name, issue_number, patch_sha256=patch_sha256
    )
    if not rows:
        return False, ("no validation has been run for this patch; run 'forgeflow validate' first")

    return False, (
        "validation recorded no executed checks for this patch (all checks were "
        "skipped, usually because no container isolation was available); an "
        "unverified patch cannot be approved for publishing. Record an exception "
        "with 'forgeflow validation-exception' if you have verified it another way"
    )


def apply_action(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
    action: ApprovalAction,
    *,
    actor: str = "operator",
    reason: str = "",
    target_branch: str = "",
) -> tuple[ApprovalRecord, TaskState]:
    """Record a decision and move the task to the matching state.

    The state move is validated by the Phase 1 state machine, so an action that
    does not fit the current state raises rather than being forced through.
    ``request_changes`` returns the task to ``implementing``, which is the loop
    a maintainer's feedback creates.
    """
    patch = storage.latest_patch(conn, repo_full_name, issue_number)
    if patch is None:
        raise ApprovalError(
            f"no patch recorded for {repo_full_name}#{issue_number}; "
            "run 'forgeflow implement' first"
        )

    issue = storage.get_issue(conn, repo_full_name, issue_number)
    if issue is None:
        raise ApprovalError(f"{repo_full_name}#{issue_number} is not in the database")

    target = _ACTION_TARGET[action]

    # Refuse before writing anything. Recording the decision first would leave
    # an approval on record even when the transition is rejected, which would
    # make the audit trail claim a decision that never took effect.
    if issue.task_state is not target:
        validate_transition(issue.task_state, target)

    if action is ApprovalAction.APPROVE:
        ok, why = require_validation_or_exception(
            conn,
            repo_full_name,
            issue_number,
            patch_sha256=patch.patch_sha256,
            from_failed_state=issue.task_state is TaskState.FAILED_VALIDATION,
        )
        if not ok:
            raise ApprovalError(f"refusing to approve {repo_full_name}#{issue_number}: {why}")

    decision = record_decision(
        conn,
        repo_full_name,
        issue_number,
        action,
        patch_sha256=patch.patch_sha256,
        target_branch=target_branch,
        actor=actor,
        reason=reason,
    )

    if issue.task_state is target:
        return decision, issue.task_state

    updated = storage.transition_task_state(
        conn,
        repo_full_name,
        issue_number,
        target,
        actor=actor,
        reason=f"{action.value}: {reason}" if reason else action.value,
    )
    return decision, updated.task_state
