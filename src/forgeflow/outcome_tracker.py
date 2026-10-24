"""Track what happens to a published pull request (SPEC sections 2J, 11, and 15).

SPEC section 2J asks ForgeFlow to "track PR feedback, requested changes, merge
status, failures, and cost to improve future issue ranking." This module does
exactly that and nothing else.

Two rules define it:

* **Read-only.** It talks to GitHub only through the read-only client, which is
  constructed from ``GITHUB_TOKEN``. It never imports or reads the publishing
  credential environment variable; that credential is not needed to read a pull
  request's state, so it is not present. ``tests/test_publish_isolation.py``
  enforces that.
* **Factual.** Every stored value comes from the GitHub API or from ForgeFlow's
  own records. A status is never guessed, and a review decision with no reviews
  is ``NONE`` rather than an invented approval.

Nothing here writes to GitHub. There is no comment, no label, and no review.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any, Protocol

from forgeflow import storage
from forgeflow.models import (
    OutcomeMetrics,
    PullRequestOutcome,
    PullRequestStatus,
    ReviewDecision,
    ReviewRecord,
)
from forgeflow.ranking import RepoOutcomeStats


class OutcomeReader(Protocol):
    """The read-only GitHub surface this module needs.

    Declared here rather than imported from the publisher so this module never
    touches the publishing credential, even transitively. The real implementation
    is :class:`forgeflow.github_client.GitHubClient`.
    """

    def get_pull_request(self, full_name: str, number: int) -> dict[str, Any]: ...

    def list_pull_request_reviews(self, full_name: str, number: int) -> list[dict[str, Any]]: ...

    def list_pull_request_review_comments(
        self, full_name: str, number: int
    ) -> list[dict[str, Any]]: ...


OUTCOME_SCHEMA = """
CREATE TABLE IF NOT EXISTS pull_request_outcomes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at        TEXT NOT NULL,
    repo_full_name    TEXT NOT NULL,
    issue_number      INTEGER NOT NULL,
    pr_number         INTEGER NOT NULL,
    pr_url            TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL,
    draft             INTEGER NOT NULL DEFAULT 1,
    merged            INTEGER NOT NULL DEFAULT 0,
    merged_at         TEXT,
    closed_at         TEXT,
    review_decision   TEXT NOT NULL DEFAULT 'none',
    review_comments   INTEGER NOT NULL DEFAULT 0,
    requested_changes INTEGER NOT NULL DEFAULT 0,
    approvals         INTEGER NOT NULL DEFAULT 0,
    failures          INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL NOT NULL DEFAULT 0.0,
    UNIQUE (repo_full_name, issue_number, pr_number)
);

CREATE INDEX IF NOT EXISTS idx_outcomes_task
    ON pull_request_outcomes (repo_full_name, issue_number);

CREATE TABLE IF NOT EXISTS pr_reviews (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_full_name TEXT NOT NULL,
    issue_number   INTEGER NOT NULL,
    pr_number      INTEGER NOT NULL,
    review_id      INTEGER NOT NULL,
    author         TEXT NOT NULL DEFAULT '',
    state          TEXT NOT NULL DEFAULT '',
    submitted_at   TEXT,
    body           TEXT NOT NULL DEFAULT '',
    UNIQUE (repo_full_name, pr_number, review_id)
);

CREATE TABLE IF NOT EXISTS outcome_metrics (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    computed_at         TEXT NOT NULL,
    repo_full_name      TEXT NOT NULL,
    issue_number        INTEGER NOT NULL,
    patch_usefulness    REAL NOT NULL DEFAULT 0.0,
    test_quality        REAL NOT NULL DEFAULT 0.0,
    complexity          REAL NOT NULL DEFAULT 0.0,
    maintainer_feedback TEXT NOT NULL DEFAULT '',
    UNIQUE (repo_full_name, issue_number)
);
"""

# Review states that GitHub reports, mapped to ForgeFlow's own decision enum.
# A state we do not recognise maps to NONE, so an unexpected value is visible as
# "no decision" rather than silently treated as an approval.
_REVIEW_STATE_MAP: dict[str, ReviewDecision] = {
    "APPROVED": ReviewDecision.APPROVED,
    "CHANGES_REQUESTED": ReviewDecision.CHANGES_REQUESTED,
    "COMMENTED": ReviewDecision.COMMENTED,
    "DISMISSED": ReviewDecision.DISMISSED,
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the outcome-tracking tables. Safe to call repeatedly."""
    conn.executescript(OUTCOME_SCHEMA)
    conn.commit()


def _parse_time(value: object) -> datetime | None:
    """Parse a GitHub ISO 8601 timestamp, or return None.

    GitHub returns ``Z``-suffixed timestamps; ``fromisoformat`` only accepts that
    form from Python 3.11, so the suffix is normalised explicitly to stay
    correct on any supported interpreter.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _status_from_payload(payload: dict) -> PullRequestStatus:
    """Derive the status from a GitHub pull request payload.

    Merged is checked before closed: a merged pull request also reports
    ``state == "closed"``, so testing closed first would report every merge as a
    plain close and corrupt the merge rate.
    """
    if payload.get("merged_at") or payload.get("merged"):
        return PullRequestStatus.MERGED
    if str(payload.get("state", "")).lower() == "closed":
        return PullRequestStatus.CLOSED
    return PullRequestStatus.OPEN


def _latest_decision(reviews: list[dict]) -> ReviewDecision:
    """The most recent substantive review decision, or NONE.

    A COMMENTED review does not change the decision, matching how GitHub treats
    review state: only APPROVED, CHANGES_REQUESTED, and DISMISSED move it. When
    no review moves it, the answer is NONE, never an assumed approval.
    """
    decision = ReviewDecision.NONE
    for review in reviews:
        state = str(review.get("state", "")).upper()
        mapped = _REVIEW_STATE_MAP.get(state)
        if mapped in (ReviewDecision.APPROVED, ReviewDecision.CHANGES_REQUESTED):
            decision = mapped
        elif mapped is ReviewDecision.DISMISSED and decision is not ReviewDecision.NONE:
            decision = ReviewDecision.DISMISSED
    return decision


def _reviews_from_payload(
    repo_full_name: str,
    issue_number: int,
    pr_number: int,
    reviews: list[dict],
) -> list[ReviewRecord]:
    """Map raw review payloads to records, skipping entries without an id."""
    records: list[ReviewRecord] = []
    for review in reviews:
        review_id = review.get("id")
        if not isinstance(review_id, int):
            continue
        author = ""
        user = review.get("user")
        if isinstance(user, dict):
            author = str(user.get("login", ""))
        records.append(
            ReviewRecord(
                repo_full_name=repo_full_name,
                issue_number=issue_number,
                pr_number=pr_number,
                review_id=review_id,
                author=author,
                state=str(review.get("state", "")),
                submitted_at=_parse_time(review.get("submitted_at")),
                body=str(review.get("body") or ""),
            )
        )
    return records


def track_pull_request(
    conn: sqlite3.Connection,
    read_client: OutcomeReader,
    repo_full_name: str,
    issue_number: int,
    pr_number: int,
    *,
    patch_sha256: str | None = None,
) -> PullRequestOutcome:
    """Fetch one pull request's real state and record it.

    ``read_client`` is any object exposing ``get_pull_request``,
    ``list_pull_request_reviews``, and ``list_pull_request_review_comments``;
    the real one is :class:`forgeflow.github_client.GitHubClient`, built from the
    read-only ``GITHUB_TOKEN``. This function performs reads only.

    ``patch_sha256`` selects which recorded validation failures to count; when
    omitted, the latest recorded patch for the task is used, so a re-run after a
    repair counts the current patch rather than an old one.
    """
    payload = read_client.get_pull_request(repo_full_name, pr_number)
    reviews = read_client.list_pull_request_reviews(repo_full_name, pr_number)
    comments = read_client.list_pull_request_review_comments(repo_full_name, pr_number)

    if patch_sha256 is None:
        latest = storage.latest_patch(conn, repo_full_name, issue_number)
        patch_sha256 = latest.patch_sha256 if latest is not None else None

    failures = 0
    if patch_sha256:
        failures = sum(
            1
            for row in storage.list_validation_results(
                conn, repo_full_name, issue_number, patch_sha256=patch_sha256
            )
            if str(row.get("status") or "") in {"failed", "errored"}
        )

    record_reviews(conn, _reviews_from_payload(repo_full_name, issue_number, pr_number, reviews))

    outcome = PullRequestOutcome(
        checked_at=datetime.now(UTC),
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        pr_number=pr_number,
        pr_url=str(payload.get("html_url", "")),
        status=_status_from_payload(payload),
        draft=bool(payload.get("draft", False)),
        merged=bool(payload.get("merged_at") or payload.get("merged")),
        merged_at=_parse_time(payload.get("merged_at")),
        closed_at=_parse_time(payload.get("closed_at")),
        review_decision=_latest_decision(reviews),
        review_comments=len(reviews) + len(comments),
        requested_changes=sum(
            1 for r in reviews if str(r.get("state", "")).upper() == "CHANGES_REQUESTED"
        ),
        approvals=sum(1 for r in reviews if str(r.get("state", "")).upper() == "APPROVED"),
        failures=failures,
        cost_usd=cost_for_task(conn, repo_full_name, issue_number),
    )
    record_outcome(conn, outcome)
    return outcome


def cost_for_task(conn: sqlite3.Connection, repo_full_name: str, issue_number: int) -> float:
    """Total recorded model cost for one task, in US dollars."""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM llm_usage
        WHERE repo_full_name = ? AND issue_number = ?
        """,
        (repo_full_name, issue_number),
    ).fetchone()
    return round(float(row["total"]), 6)


def record_outcome(conn: sqlite3.Connection, outcome: PullRequestOutcome) -> int:
    """Insert or update the tracked state of one pull request.

    Keyed on ``(repo, issue, pr_number)`` so re-tracking updates the row rather
    than appending a duplicate. The newest fetch wins, because the point is the
    current state.
    """
    from forgeflow.security import redact

    checked = (outcome.checked_at or datetime.now(UTC)).isoformat()
    conn.execute(
        """
        INSERT INTO pull_request_outcomes (
            checked_at, repo_full_name, issue_number, pr_number, pr_url, status,
            draft, merged, merged_at, closed_at, review_decision, review_comments,
            requested_changes, approvals, failures, cost_usd
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo_full_name, issue_number, pr_number) DO UPDATE SET
            checked_at = excluded.checked_at,
            pr_url = excluded.pr_url,
            status = excluded.status,
            draft = excluded.draft,
            merged = excluded.merged,
            merged_at = excluded.merged_at,
            closed_at = excluded.closed_at,
            review_decision = excluded.review_decision,
            review_comments = excluded.review_comments,
            requested_changes = excluded.requested_changes,
            approvals = excluded.approvals,
            failures = excluded.failures,
            cost_usd = excluded.cost_usd
        """,
        (
            checked,
            outcome.repo_full_name,
            outcome.issue_number,
            outcome.pr_number,
            redact(outcome.pr_url),
            outcome.status.value,
            1 if outcome.draft else 0,
            1 if outcome.merged else 0,
            outcome.merged_at.isoformat() if outcome.merged_at else None,
            outcome.closed_at.isoformat() if outcome.closed_at else None,
            outcome.review_decision.value,
            outcome.review_comments,
            outcome.requested_changes,
            outcome.approvals,
            outcome.failures,
            outcome.cost_usd,
        ),
    )
    conn.commit()
    row = conn.execute(
        """
        SELECT id FROM pull_request_outcomes
        WHERE repo_full_name = ? AND issue_number = ? AND pr_number = ?
        """,
        (outcome.repo_full_name, outcome.issue_number, outcome.pr_number),
    ).fetchone()
    return int(row["id"]) if row else 0


def record_reviews(conn: sqlite3.Connection, reviews: list[ReviewRecord]) -> int:
    """Insert or update review submissions. Returns the number written."""
    from forgeflow.security import redact

    written = 0
    for review in reviews:
        conn.execute(
            """
            INSERT INTO pr_reviews (
                repo_full_name, issue_number, pr_number, review_id, author,
                state, submitted_at, body
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_full_name, pr_number, review_id) DO UPDATE SET
                state = excluded.state,
                submitted_at = excluded.submitted_at,
                body = excluded.body
            """,
            (
                review.repo_full_name,
                review.issue_number,
                review.pr_number,
                review.review_id,
                redact(review.author),
                review.state,
                review.submitted_at.isoformat() if review.submitted_at else None,
                redact(review.body),
            ),
        )
        written += 1
    conn.commit()
    return written


def _row_to_outcome(row: sqlite3.Row) -> PullRequestOutcome:
    return PullRequestOutcome(
        id=row["id"],
        checked_at=datetime.fromisoformat(row["checked_at"]) if row["checked_at"] else None,
        repo_full_name=row["repo_full_name"],
        issue_number=row["issue_number"],
        pr_number=row["pr_number"],
        pr_url=row["pr_url"],
        status=PullRequestStatus(row["status"]),
        draft=bool(row["draft"]),
        merged=bool(row["merged"]),
        merged_at=datetime.fromisoformat(row["merged_at"]) if row["merged_at"] else None,
        closed_at=datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None,
        review_decision=ReviewDecision(row["review_decision"]),
        review_comments=row["review_comments"],
        requested_changes=row["requested_changes"],
        approvals=row["approvals"],
        failures=row["failures"],
        cost_usd=row["cost_usd"],
    )


def latest_outcome(
    conn: sqlite3.Connection, repo_full_name: str, issue_number: int
) -> PullRequestOutcome | None:
    """The most recently checked outcome for a task, or None."""
    row = conn.execute(
        """
        SELECT * FROM pull_request_outcomes
        WHERE repo_full_name = ? AND issue_number = ?
        ORDER BY checked_at DESC, id DESC LIMIT 1
        """,
        (repo_full_name, issue_number),
    ).fetchone()
    return _row_to_outcome(row) if row else None


def list_outcomes(conn: sqlite3.Connection) -> list[PullRequestOutcome]:
    """Every tracked pull request, newest first."""
    rows = conn.execute(
        "SELECT * FROM pull_request_outcomes ORDER BY checked_at DESC, id DESC"
    ).fetchall()
    return [_row_to_outcome(row) for row in rows]


def list_reviews(
    conn: sqlite3.Connection, repo_full_name: str, issue_number: int
) -> list[ReviewRecord]:
    """Every stored review for a task, oldest first."""
    rows = conn.execute(
        """
        SELECT * FROM pr_reviews
        WHERE repo_full_name = ? AND issue_number = ?
        ORDER BY id
        """,
        (repo_full_name, issue_number),
    ).fetchall()
    return [
        ReviewRecord(
            id=row["id"],
            repo_full_name=row["repo_full_name"],
            issue_number=row["issue_number"],
            pr_number=row["pr_number"],
            review_id=row["review_id"],
            author=row["author"],
            state=row["state"],
            submitted_at=(
                datetime.fromisoformat(row["submitted_at"]) if row["submitted_at"] else None
            ),
            body=row["body"],
        )
        for row in rows
    ]


# --------------------------------------------------------------------------
# SPEC section 11 evaluation fields
# --------------------------------------------------------------------------

# The nine SPEC section 11 metrics, in the order the spec lists them. Kept as a
# literal so a report can iterate the required set and a missing metric is a
# visible gap rather than a silent omission.
SPEC_METRICS: tuple[str, ...] = (
    "issue_relevance_rate",
    "patch_review_acceptance_rate",
    "test_pass_rate",
    "pr_merge_rate",
    "substantive_revision_rate",
    "cost_per_reviewed_patch",
    "time_saved_hours",
    "task_failure_rate",
    "security_incidents",
)


def _rate(numerator: int, denominator: int) -> float:
    """A 0.0 to 1.0 rate, or 0.0 when there is nothing to divide by.

    Zero is chosen over None because the metric is a rate over a known set; an
    empty set has a rate of zero by convention, and a caller that needs to tell
    "no data" from "all failures" should check the denominator it passed in.
    """
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def evaluate_task(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
) -> OutcomeMetrics:
    """Compute the SPEC section 11 per-task fields from recorded facts.

    Each field is a 0.0 to 1.0 measurement, not a claim to a maintainer:

    * ``patch_usefulness`` — how far the PR got: 1.0 merged, 0.5 open with an
      approval, 0.25 open, 0.0 closed without merging.
    * ``test_quality`` — the fraction of recorded checks that passed, out of the
      checks that actually executed. A skipped check is not a pass.
    * ``complexity`` — the size of the patch, saturating at 400 changed lines.
    * ``maintainer_feedback`` — the concatenated review bodies, verbatim.
    """
    outcome = latest_outcome(conn, repo_full_name, issue_number)

    if outcome is None:
        usefulness = 0.0
    elif outcome.status is PullRequestStatus.MERGED:
        usefulness = 1.0
    elif (
        outcome.status is PullRequestStatus.OPEN
        and outcome.review_decision is ReviewDecision.APPROVED
    ):
        usefulness = 0.5
    elif outcome.status is PullRequestStatus.OPEN:
        usefulness = 0.25
    else:
        usefulness = 0.0

    patch = storage.latest_patch(conn, repo_full_name, issue_number)
    passed = 0
    executed = 0
    changed_lines = 0
    if patch is not None:
        changed_lines = patch.additions + patch.deletions
        for row in storage.list_validation_results(
            conn, repo_full_name, issue_number, patch_sha256=patch.patch_sha256
        ):
            status = str(row.get("status") or "")
            if status in {"passed", "failed", "errored"}:
                executed += 1
                if status == "passed":
                    passed += 1

    feedback = "\n\n".join(
        review.body.strip()
        for review in list_reviews(conn, repo_full_name, issue_number)
        if review.body.strip()
    )

    metrics = OutcomeMetrics(
        computed_at=datetime.now(UTC),
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        patch_usefulness=usefulness,
        test_quality=_rate(passed, executed),
        complexity=min(changed_lines / 400.0, 1.0),
        maintainer_feedback=feedback,
    )
    record_metrics(conn, metrics)
    return metrics


def record_metrics(conn: sqlite3.Connection, metrics: OutcomeMetrics) -> int:
    """Insert or update the evaluation fields for one task."""
    from forgeflow.security import redact

    computed = (metrics.computed_at or datetime.now(UTC)).isoformat()
    conn.execute(
        """
        INSERT INTO outcome_metrics (
            computed_at, repo_full_name, issue_number, patch_usefulness,
            test_quality, complexity, maintainer_feedback
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo_full_name, issue_number) DO UPDATE SET
            computed_at = excluded.computed_at,
            patch_usefulness = excluded.patch_usefulness,
            test_quality = excluded.test_quality,
            complexity = excluded.complexity,
            maintainer_feedback = excluded.maintainer_feedback
        """,
        (
            computed,
            metrics.repo_full_name,
            metrics.issue_number,
            metrics.patch_usefulness,
            metrics.test_quality,
            metrics.complexity,
            redact(metrics.maintainer_feedback),
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM outcome_metrics WHERE repo_full_name = ? AND issue_number = ?",
        (metrics.repo_full_name, metrics.issue_number),
    ).fetchone()
    return int(row["id"]) if row else 0


def latest_metrics(
    conn: sqlite3.Connection, repo_full_name: str, issue_number: int
) -> OutcomeMetrics | None:
    """The most recently computed evaluation fields for a task, or None."""
    row = conn.execute(
        """
        SELECT * FROM outcome_metrics
        WHERE repo_full_name = ? AND issue_number = ?
        ORDER BY computed_at DESC, id DESC LIMIT 1
        """,
        (repo_full_name, issue_number),
    ).fetchone()
    if row is None:
        return None
    return OutcomeMetrics(
        id=row["id"],
        computed_at=datetime.fromisoformat(row["computed_at"]) if row["computed_at"] else None,
        repo_full_name=row["repo_full_name"],
        issue_number=row["issue_number"],
        patch_usefulness=row["patch_usefulness"],
        test_quality=row["test_quality"],
        complexity=row["complexity"],
        maintainer_feedback=row["maintainer_feedback"],
    )


def metrics_report(conn: sqlite3.Connection) -> dict[str, float | int]:
    """Aggregate the SPEC section 11 metrics across every tracked task.

    Every key in :data:`SPEC_METRICS` is present, so a missing metric is visible
    as a zero rather than an absent key. Rates are over the set of tracked pull
    requests, so an empty set yields zero for every rate.

    ``time_saved_hours`` is reported as ``0.0`` rather than estimated. ForgeFlow
    does not measure how long a human would have taken, and inventing a constant
    multiplier would be a fabricated benchmark, which SPEC section 15 forbids.
    ``security_incidents`` is read from the append-only audit log.
    """
    outcomes = list_outcomes(conn)
    total = len(outcomes)
    merged = sum(1 for o in outcomes if o.status is PullRequestStatus.MERGED)
    closed_unmerged = sum(1 for o in outcomes if o.status is PullRequestStatus.CLOSED)
    with_changes_requested = sum(1 for o in outcomes if o.requested_changes > 0)
    # A maintainer engaged if they approved, requested changes, or merged. This
    # is the signal that the issue was a real, relevant task rather than noise.
    engaged = sum(
        1
        for o in outcomes
        if o.approvals > 0 or o.requested_changes > 0 or o.status is PullRequestStatus.MERGED
    )
    accepted = sum(1 for o in outcomes if o.approvals > 0 or o.status is PullRequestStatus.MERGED)

    # Test pass rate and cost are read from ForgeFlow's own records, keyed per
    # task, so a task with no validation evidence contributes zero executed
    # checks rather than being silently counted as a pass.
    tasks = {
        (row["repo_full_name"], row["issue_number"])
        for row in conn.execute("SELECT DISTINCT repo_full_name, issue_number FROM patches")
    }
    executed = 0
    passed = 0
    cost_total = 0.0
    for repo_full_name, issue_number in tasks:
        cost_total += cost_for_task(conn, repo_full_name, issue_number)
        patch = storage.latest_patch(conn, repo_full_name, issue_number)
        if patch is None:
            continue
        for row in storage.list_validation_results(
            conn, repo_full_name, issue_number, patch_sha256=patch.patch_sha256
        ):
            status = str(row.get("status") or "")
            if status in {"passed", "failed", "errored"}:
                executed += 1
                if status == "passed":
                    passed += 1

    return {
        "issue_relevance_rate": _rate(engaged, total),
        "patch_review_acceptance_rate": _rate(accepted, total),
        "test_pass_rate": _rate(passed, executed),
        "pr_merge_rate": _rate(merged, total),
        "substantive_revision_rate": _rate(with_changes_requested, total),
        "cost_per_reviewed_patch": round(cost_total / total, 6) if total else 0.0,
        # Not measured. Reported as zero rather than estimated from a constant.
        "time_saved_hours": 0.0,
        "task_failure_rate": _rate(closed_unmerged, total),
        "security_incidents": _security_incidents(conn),
    }


def _security_incidents(conn: sqlite3.Connection) -> int:
    """Count security-relevant audit events, from the append-only log.

    Only ``critical`` severity events count as incidents; a ``warning`` such as a
    refused publish is a guard working, not an incident.
    """
    row = conn.execute("SELECT COUNT(*) AS n FROM audit_log WHERE severity = 'critical'").fetchone()
    return int(row["n"]) if row else 0


# --------------------------------------------------------------------------
# Outcome history for ranking (SPEC section 11)
# --------------------------------------------------------------------------


def outcome_stats_by_repo(
    conn: sqlite3.Connection,
    *,
    lookback: int = 20,
) -> dict[str, RepoOutcomeStats]:
    """Aggregate tracked outcomes per repository, for the ranking adjustment.

    Only the most recent ``lookback`` rows per repository count, so old history
    cannot dominate. This reads ForgeFlow's own records; it performs no network
    access and touches no credential.
    """
    stats: dict[str, RepoOutcomeStats] = {}
    repos = [
        row["repo_full_name"]
        for row in conn.execute("SELECT DISTINCT repo_full_name FROM pull_request_outcomes")
    ]
    for repo_full_name in repos:
        rows = conn.execute(
            """
            SELECT status, merged, approvals, requested_changes
            FROM pull_request_outcomes
            WHERE repo_full_name = ?
            ORDER BY checked_at DESC, id DESC
            LIMIT ?
            """,
            (repo_full_name, lookback),
        ).fetchall()
        merged = sum(1 for row in rows if row["merged"])
        approvals = sum(1 for row in rows if row["approvals"] > 0)
        changes_requested = sum(1 for row in rows if row["requested_changes"] > 0)
        closed_unmerged = sum(
            1
            for row in rows
            if row["status"] == PullRequestStatus.CLOSED.value and not row["merged"]
        )
        stats[repo_full_name] = RepoOutcomeStats(
            merged=merged,
            approvals=approvals,
            changes_requested=changes_requested,
            closed_unmerged=closed_unmerged,
            total=len(rows),
        )
    return stats
