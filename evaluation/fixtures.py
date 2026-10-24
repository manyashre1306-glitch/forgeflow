"""Known-outcome fixtures for the evaluation harness (SPEC section 11).

Every fixture is a *controlled* scenario: ForgeFlow records facts for it through
the real storage, outcome, and validation APIs, and the expected metrics are
known in advance. The point is to prove the metric arithmetic is correct and to
exercise the "not enough data" cases, not to measure real-world performance.

Nothing here touches the network. The ``SWE_BENCH_NOTE`` constant and the
``PATCH_CASES`` set are the honest boundary: real SWE-bench is *not* run here
(no dataset, no network, no container runtime), so instead there is a small set
of reproducible patch-application cases that check a diff applies to a known
base tree and produces a known result.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from forgeflow import audit, storage
from forgeflow.models import (
    CheckKind,
    CheckStatus,
    Issue,
    LLMUsage,
    OutcomeMetrics,
    PatchRecord,
    PullRequestOutcome,
    PullRequestStatus,
    Repository,
    ReviewDecision,
    ReviewRecord,
    ValidationCheck,
    ValidationReport,
)
from forgeflow.outcome_tracker import record_outcome, record_reviews
from forgeflow.patch import compute_patch_sha256

#: Repo used by every fixture. A valid ``owner/name`` but not a real repository.
FIXTURE_REPO = "eval/fixture"

#: Stated plainly so no reader mistakes the patch cases for a benchmark run.
SWE_BENCH_NOTE = (
    "SWE-bench is not run here: it needs a dataset, network access, and a "
    "container runtime, none of which are available. The patch cases below are "
    "a small reproducible substitute that only checks a diff applies to a known "
    "base tree. No benchmark performance is claimed."
)


@dataclass(frozen=True)
class FixtureTask:
    """One controlled task and the facts ForgeFlow records for it."""

    issue_number: int
    title: str
    has_patch: bool
    has_outcome: bool
    validation_status: CheckStatus | None
    outcome_status: PullRequestStatus | None
    approvals: int = 0
    requested_changes: int = 0
    cost_usd: float = 0.0
    review_body: str = ""
    note: str = ""


#: The fixture set. Every rate in the harness is computed over these tasks, so
#: the expected numbers are known by construction (see ``tests/test_evaluation``).
FIXTURE_TASKS: tuple[FixtureTask, ...] = (
    FixtureTask(
        issue_number=1,
        title="Merged with an approval",
        has_patch=True,
        has_outcome=True,
        validation_status=CheckStatus.PASSED,
        outcome_status=PullRequestStatus.MERGED,
        approvals=1,
        cost_usd=0.10,
        review_body="Looks good, thanks.",
        note="the happy path",
    ),
    FixtureTask(
        issue_number=2,
        title="Open draft, no review yet",
        has_patch=True,
        has_outcome=True,
        validation_status=CheckStatus.PASSED,
        outcome_status=PullRequestStatus.OPEN,
        cost_usd=0.20,
        note="waiting on a maintainer",
    ),
    FixtureTask(
        issue_number=3,
        title="Maintainer requested changes",
        has_patch=True,
        has_outcome=True,
        validation_status=CheckStatus.FAILED,
        outcome_status=PullRequestStatus.OPEN,
        requested_changes=1,
        cost_usd=0.30,
        review_body="Please add a regression test before this can land.",
        note="substantive revision requested; validation failed",
    ),
    FixtureTask(
        issue_number=4,
        title="Closed without merging",
        has_patch=True,
        has_outcome=True,
        validation_status=CheckStatus.PASSED,
        outcome_status=PullRequestStatus.CLOSED,
        cost_usd=0.40,
        note="a task failure",
    ),
    FixtureTask(
        issue_number=5,
        title="Discovered but never implemented",
        has_patch=False,
        has_outcome=False,
        validation_status=None,
        outcome_status=None,
        note="not enough data: no patch, no tracked PR",
    ),
)

_SAMPLE_DIFF = """\
--- a/notes.md
+++ b/notes.md
@@ -1,2 +1,3 @@
 # Notes
+Added a line.
 Existing line.
"""


def _record_patch(conn: sqlite3.Connection, task: FixtureTask) -> str:
    diff = _SAMPLE_DIFF
    sha = compute_patch_sha256(diff)
    storage.record_patch(
        conn,
        PatchRecord(
            repo_full_name=FIXTURE_REPO,
            issue_number=task.issue_number,
            provider="fixture",
            patch_sha256=sha,
            diff=diff,
            files_changed=1,
            additions=1,
            deletions=0,
            isolated=True,
            notes=task.note,
        ),
    )
    return sha


def _record_validation(
    conn: sqlite3.Connection,
    task: FixtureTask,
    patch_sha256: str,
) -> None:
    if task.validation_status is None:
        return
    status = task.validation_status
    checks = [
        ValidationCheck(
            kind=CheckKind.TESTS,
            name="pytest",
            command=["pytest", "-q"],
            status=status,
            exit_code=0 if status is CheckStatus.PASSED else 1,
            output="ok" if status is CheckStatus.PASSED else "1 failed",
        ),
        ValidationCheck(
            kind=CheckKind.LINT,
            name="ruff",
            command=["ruff", "check", "."],
            status=CheckStatus.PASSED,
            exit_code=0,
            output="ok",
        ),
    ]
    storage.record_validation_report(
        conn,
        ValidationReport(
            repo_full_name=FIXTURE_REPO,
            issue_number=task.issue_number,
            patch_sha256=patch_sha256,
            isolated=True,
            checks=checks,
        ),
    )


def _record_outcome(conn: sqlite3.Connection, task: FixtureTask) -> None:
    if not task.has_outcome or task.outcome_status is None:
        return
    status = task.outcome_status
    decision = (
        ReviewDecision.APPROVED
        if task.approvals
        else (ReviewDecision.CHANGES_REQUESTED if task.requested_changes else ReviewDecision.NONE)
    )
    record_outcome(
        conn,
        PullRequestOutcome(
            checked_at=datetime.now(UTC),
            repo_full_name=FIXTURE_REPO,
            issue_number=task.issue_number,
            pr_number=100 + task.issue_number,
            pr_url=f"https://example.invalid/{FIXTURE_REPO}/pull/{100 + task.issue_number}",
            status=status,
            draft=status is PullRequestStatus.OPEN,
            merged=status is PullRequestStatus.MERGED,
            review_decision=decision,
            approvals=task.approvals,
            requested_changes=task.requested_changes,
            review_comments=1 if task.review_body else 0,
            cost_usd=task.cost_usd,
        ),
    )
    if task.review_body:
        record_reviews(
            conn,
            [
                ReviewRecord(
                    repo_full_name=FIXTURE_REPO,
                    issue_number=task.issue_number,
                    pr_number=100 + task.issue_number,
                    review_id=task.issue_number,
                    author="fixture-maintainer",
                    state=decision.value.upper(),
                    body=task.review_body,
                )
            ],
        )


def _record_cost(conn: sqlite3.Connection, task: FixtureTask) -> None:
    if task.cost_usd <= 0:
        return
    storage.record_llm_usage(
        conn,
        LLMUsage(
            repo_full_name=FIXTURE_REPO,
            issue_number=task.issue_number,
            purpose="analysis",
            model="fixture-model",
            prompt_tokens=1000,
            completion_tokens=500,
            cost_usd=task.cost_usd,
        ),
    )


def build_fixture_db() -> sqlite3.Connection:
    """A fresh in-memory database with every fixture task recorded.

    Returns a live connection the caller owns and must close.
    """
    conn = storage.connect(":memory:")
    storage.init_db(conn)
    storage.upsert_repository(conn, Repository(full_name=FIXTURE_REPO))
    for task in FIXTURE_TASKS:
        storage.upsert_issue(
            conn,
            Issue(repo_full_name=FIXTURE_REPO, number=task.issue_number, title=task.title),
        )
    for task in FIXTURE_TASKS:
        if task.has_patch:
            sha = _record_patch(conn, task)
            _record_validation(conn, task, sha)
        _record_cost(conn, task)
        _record_outcome(conn, task)
    return conn


# --- reproducible patch-application cases (not SWE-bench) -------------------
#
# Each case applies a diff to a known base file and checks the result, or checks
# that a bad diff is refused. Deterministic, offline, and reproducible anywhere.

_PATCH_BASE = "# Notes\nExisting line.\n"


@dataclass(frozen=True)
class PatchCase:
    """One reproducible patch-application case."""

    name: str
    base_files: dict[str, str]
    diff: str
    should_apply: bool
    expected_content: str = ""


PATCH_CASES: tuple[PatchCase, ...] = (
    PatchCase(
        name="applies_a_clean_addition",
        base_files={"notes.md": _PATCH_BASE},
        diff=_SAMPLE_DIFF,
        should_apply=True,
        expected_content="# Notes\nAdded a line.\nExisting line.\n",
    ),
    PatchCase(
        name="refuses_context_mismatch",
        base_files={"notes.md": "# Notes\nA DIFFERENT line.\n"},
        diff=_SAMPLE_DIFF,
        should_apply=False,
    ),
    PatchCase(
        name="refuses_a_missing_base_file",
        base_files={},
        diff=_SAMPLE_DIFF,
        should_apply=False,
    ),
)


#: Fields the harness reports per task, beyond the aggregate SPEC metrics.
PER_TASK_FIELDS: tuple[str, ...] = (
    "patch_usefulness",
    "test_quality",
    "complexity",
    "maintainer_feedback",
)


def per_task_metrics(conn: sqlite3.Connection) -> list[tuple[int, OutcomeMetrics]]:
    """The per-task evaluation fields for every fixture task that has a patch."""
    from forgeflow.outcome_tracker import evaluate_task

    results: list[tuple[int, OutcomeMetrics]] = []
    for task in FIXTURE_TASKS:
        if not task.has_patch:
            continue
        results.append((task.issue_number, evaluate_task(conn, FIXTURE_REPO, task.issue_number)))
    return results


def record_critical_incident(conn: sqlite3.Connection) -> None:
    """Record one critical audit event, to exercise the incident counter.

    Kept out of :func:`build_fixture_db` so the default report shows zero
    incidents rather than a scary-looking fabricated one.
    """
    audit.record(
        conn,
        "security.fixture_incident",
        repo_full_name=FIXTURE_REPO,
        severity="critical",
        detail="fixture-only critical event used to test the incident counter",
    )
