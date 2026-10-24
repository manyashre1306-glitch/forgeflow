"""Tests for pull-request outcome tracking (SPEC sections 2J, 11, and 15).

Everything runs against a fake read client and a real temporary SQLite
database. No test touches the network, and no test can reach the publishing
credential: the module under test does not import it, which
``test_publish_isolation.py`` enforces separately.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from forgeflow import outcome_tracker, storage
from forgeflow.models import (
    CheckKind,
    CheckStatus,
    Issue,
    LLMUsage,
    PatchRecord,
    PullRequestStatus,
    Repository,
    ReviewDecision,
    ValidationCheck,
    ValidationReport,
)
from forgeflow.patch import compute_patch_sha256

REPO = "manyashre1306-glitch/forgeflow-sandbox"
ISSUE = 1
PR = 2
DIFF = (
    "diff --git a/README.md b/README.md\n"
    "--- a/README.md\n"
    "+++ b/README.md\n"
    "@@ -1 +1,2 @@\n"
    " # x\n"
    "+## Notes\n"
)
HASH = compute_patch_sha256(DIFF)


class FakeReader:
    """A read-only client with canned PR data. Records every call."""

    def __init__(
        self,
        *,
        pr: dict | None = None,
        reviews: list[dict] | None = None,
        comments: list[dict] | None = None,
    ) -> None:
        self.pr = (
            pr
            if pr is not None
            else {
                "number": PR,
                "html_url": f"https://github.com/{REPO}/pull/{PR}",
                "state": "open",
                "draft": True,
                "merged": False,
                "merged_at": None,
                "closed_at": None,
            }
        )
        self.reviews = reviews or []
        self.comments = comments or []
        self.pr_calls: list[tuple[str, int]] = []
        self.review_calls: list[tuple[str, int]] = []

    def get_pull_request(self, full_name: str, number: int) -> dict:
        self.pr_calls.append((full_name, number))
        return dict(self.pr)

    def list_pull_request_reviews(self, full_name: str, number: int) -> list[dict]:
        self.review_calls.append((full_name, number))
        return list(self.reviews)

    def list_pull_request_review_comments(self, full_name: str, number: int) -> list[dict]:
        return list(self.comments)


def seed_task(conn, *, number: int = ISSUE) -> str:
    storage.upsert_repository(conn, Repository(full_name=REPO, stars=1))
    storage.upsert_issue(conn, Issue(repo_full_name=REPO, number=number, title="Add Notes"))
    storage.record_patch(
        conn,
        PatchRecord(
            repo_full_name=REPO,
            issue_number=number,
            provider="mock",
            patch_sha256=HASH,
            diff=DIFF,
            additions=4,
            deletions=1,
        ),
    )
    return HASH


def review(state: str, *, review_id: int = 1, body: str = "") -> dict:
    return {
        "id": review_id,
        "state": state,
        "user": {"login": "maintainer"},
        "submitted_at": "2026-10-24T08:00:00Z",
        "body": body,
    }


# ==========================================================================
# Credential isolation
# ==========================================================================


def test_outcome_tracker_does_not_read_the_publish_token() -> None:
    """SPEC section 8: the tracker reads with GITHUB_TOKEN only.

    The module may *name* the variable in prose to say it is not used; what must
    never appear is code that reads it. So the check is on the parsed module:
    no import of the credential module, and no environment access at all.
    """
    tree = ast.parse(Path(outcome_tracker.__file__).read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any("publish_token" in name for name in imported)

    # No os.environ / os.getenv usage, so the module cannot read any credential.
    env_reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv", "getenvb"}
    ]
    assert env_reads == [], "outcome_tracker must not read the environment itself"


def test_outcome_tracker_imports_no_publisher_module() -> None:
    """Importing the publisher would drag in the write credential module."""
    tree = ast.parse(Path(outcome_tracker.__file__).read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    assert not any("publisher" in name for name in modules)


# ==========================================================================
# Status derivation
# ==========================================================================


def test_tracks_an_open_draft_pr(conn) -> None:
    seed_task(conn)
    reader = FakeReader()
    outcome = outcome_tracker.track_pull_request(conn, reader, REPO, ISSUE, PR)
    assert outcome.status is PullRequestStatus.OPEN
    assert outcome.draft is True
    assert outcome.merged is False
    assert reader.pr_calls == [(REPO, PR)]


def test_merged_pr_is_not_reported_as_closed(conn) -> None:
    """A merged PR also reports state 'closed'; merged must win."""
    seed_task(conn)
    reader = FakeReader(
        pr={
            "number": PR,
            "html_url": "u",
            "state": "closed",
            "merged": True,
            "merged_at": "2026-10-24T09:00:00Z",
            "closed_at": "2026-10-24T09:00:00Z",
        }
    )
    outcome = outcome_tracker.track_pull_request(conn, reader, REPO, ISSUE, PR)
    assert outcome.status is PullRequestStatus.MERGED
    assert outcome.merged is True
    assert outcome.merged_at is not None


def test_closed_unmerged_pr_is_closed(conn) -> None:
    seed_task(conn)
    reader = FakeReader(
        pr={"number": PR, "html_url": "u", "state": "closed", "merged": False, "merged_at": None}
    )
    outcome = outcome_tracker.track_pull_request(conn, reader, REPO, ISSUE, PR)
    assert outcome.status is PullRequestStatus.CLOSED
    assert outcome.merged is False


# ==========================================================================
# Reviews
# ==========================================================================


def test_changes_requested_is_recorded(conn) -> None:
    seed_task(conn)
    reader = FakeReader(reviews=[review("CHANGES_REQUESTED", body="please rename")])
    outcome = outcome_tracker.track_pull_request(conn, reader, REPO, ISSUE, PR)
    assert outcome.review_decision is ReviewDecision.CHANGES_REQUESTED
    assert outcome.requested_changes == 1
    assert outcome.approvals == 0


def test_approval_is_recorded(conn) -> None:
    seed_task(conn)
    reader = FakeReader(reviews=[review("APPROVED")])
    outcome = outcome_tracker.track_pull_request(conn, reader, REPO, ISSUE, PR)
    assert outcome.review_decision is ReviewDecision.APPROVED
    assert outcome.approvals == 1


def test_no_reviews_is_none_not_approved(conn) -> None:
    """An unreviewed PR must never read as approved."""
    seed_task(conn)
    outcome = outcome_tracker.track_pull_request(conn, FakeReader(), REPO, ISSUE, PR)
    assert outcome.review_decision is ReviewDecision.NONE
    assert outcome.approvals == 0
    assert outcome.requested_changes == 0


def test_review_comments_count_includes_inline_comments(conn) -> None:
    seed_task(conn)
    reader = FakeReader(
        reviews=[review("COMMENTED")],
        comments=[{"id": 9, "body": "nit"}],
    )
    outcome = outcome_tracker.track_pull_request(conn, reader, REPO, ISSUE, PR)
    assert outcome.review_comments == 2


def test_reviews_are_persisted_and_deduplicated(conn) -> None:
    seed_task(conn)
    reader = FakeReader(reviews=[review("APPROVED", review_id=5)])
    outcome_tracker.track_pull_request(conn, reader, REPO, ISSUE, PR)
    outcome_tracker.track_pull_request(conn, reader, REPO, ISSUE, PR)
    stored = outcome_tracker.list_reviews(conn, REPO, ISSUE)
    assert len(stored) == 1
    assert stored[0].review_id == 5
    assert stored[0].state == "APPROVED"


def test_unknown_review_state_does_not_become_approval(conn) -> None:
    """An unrecognised review state maps to NONE, not an invented decision."""
    seed_task(conn)
    reader = FakeReader(reviews=[review("SOMETHING_NEW")])
    outcome = outcome_tracker.track_pull_request(conn, reader, REPO, ISSUE, PR)
    assert outcome.review_decision is ReviewDecision.NONE


# ==========================================================================
# Failures and cost
# ==========================================================================


def _report_with(conn, status: CheckStatus) -> None:
    storage.record_validation_report(
        conn,
        ValidationReport(
            repo_full_name=REPO,
            issue_number=ISSUE,
            patch_sha256=HASH,
            isolated=False,
            checks=[
                ValidationCheck(
                    kind=CheckKind.TESTS,
                    name="pytest",
                    command=["python", "-m", "pytest"],
                    status=status,
                    exit_code=1 if status is CheckStatus.FAILED else None,
                )
            ],
        ),
    )


def test_failures_counts_recorded_failed_checks(conn) -> None:
    seed_task(conn)
    _report_with(conn, CheckStatus.FAILED)
    outcome = outcome_tracker.track_pull_request(conn, FakeReader(), REPO, ISSUE, PR)
    assert outcome.failures == 1


def test_failures_ignores_skipped_checks(conn) -> None:
    seed_task(conn)
    _report_with(conn, CheckStatus.SKIPPED)
    outcome = outcome_tracker.track_pull_request(conn, FakeReader(), REPO, ISSUE, PR)
    assert outcome.failures == 0


def test_cost_is_read_from_recorded_usage(conn) -> None:
    seed_task(conn)
    storage.record_llm_usage(
        conn,
        LLMUsage(
            repo_full_name=REPO,
            issue_number=ISSUE,
            purpose="analysis",
            model="gemini-2.5-pro",
            prompt_tokens=100,
            completion_tokens=50,
            cost_usd=0.0123,
        ),
    )
    outcome = outcome_tracker.track_pull_request(conn, FakeReader(), REPO, ISSUE, PR)
    assert outcome.cost_usd == pytest.approx(0.0123)


# ==========================================================================
# Persistence and idempotence
# ==========================================================================


def test_re_tracking_updates_the_same_row(conn) -> None:
    seed_task(conn)
    outcome_tracker.track_pull_request(conn, FakeReader(), REPO, ISSUE, PR)
    merged = FakeReader(
        pr={
            "number": PR,
            "html_url": "u",
            "state": "closed",
            "merged": True,
            "merged_at": "2026-10-24T09:00:00Z",
        }
    )
    outcome_tracker.track_pull_request(conn, merged, REPO, ISSUE, PR)
    all_outcomes = outcome_tracker.list_outcomes(conn)
    assert len(all_outcomes) == 1
    assert all_outcomes[0].status is PullRequestStatus.MERGED


def test_latest_outcome_returns_the_newest(conn) -> None:
    seed_task(conn)
    outcome_tracker.track_pull_request(conn, FakeReader(), REPO, ISSUE, PR)
    latest = outcome_tracker.latest_outcome(conn, REPO, ISSUE)
    assert latest is not None
    assert latest.pr_number == PR


def test_latest_outcome_is_none_without_a_tracked_pr(conn) -> None:
    assert outcome_tracker.latest_outcome(conn, REPO, ISSUE) is None


# ==========================================================================
# SPEC section 11 evaluation fields
# ==========================================================================


def test_evaluate_merged_pr_is_max_usefulness(conn) -> None:
    seed_task(conn)
    merged = FakeReader(
        pr={
            "number": PR,
            "html_url": "u",
            "state": "closed",
            "merged": True,
            "merged_at": "2026-10-24T09:00:00Z",
        }
    )
    outcome_tracker.track_pull_request(conn, merged, REPO, ISSUE, PR)
    metrics = outcome_tracker.evaluate_task(conn, REPO, ISSUE)
    assert metrics.patch_usefulness == 1.0


def test_evaluate_test_quality_counts_only_executed_checks(conn) -> None:
    """One passed and one skipped is 1.0, because a skip is not a failure."""
    seed_task(conn)
    storage.record_validation_report(
        conn,
        ValidationReport(
            repo_full_name=REPO,
            issue_number=ISSUE,
            patch_sha256=HASH,
            isolated=False,
            checks=[
                ValidationCheck(
                    kind=CheckKind.TESTS,
                    name="pytest",
                    command=["pytest"],
                    status=CheckStatus.PASSED,
                    exit_code=0,
                ),
                ValidationCheck(
                    kind=CheckKind.LINT,
                    name="ruff",
                    command=[],
                    status=CheckStatus.SKIPPED,
                    skip_reason="no container",
                ),
            ],
        ),
    )
    metrics = outcome_tracker.evaluate_task(conn, REPO, ISSUE)
    assert metrics.test_quality == 1.0


def test_evaluate_test_quality_is_zero_when_nothing_executed(conn) -> None:
    seed_task(conn)
    _report_with(conn, CheckStatus.SKIPPED)
    metrics = outcome_tracker.evaluate_task(conn, REPO, ISSUE)
    assert metrics.test_quality == 0.0


def test_evaluate_complexity_saturates(conn) -> None:
    seed_task(conn)
    metrics = outcome_tracker.evaluate_task(conn, REPO, ISSUE)
    # additions 4 + deletions 1 = 5 lines out of 400.
    assert metrics.complexity == pytest.approx(5 / 400)


def test_evaluate_collects_maintainer_feedback_verbatim(conn) -> None:
    seed_task(conn)
    reader = FakeReader(reviews=[review("CHANGES_REQUESTED", body="Please add a test.")])
    outcome_tracker.track_pull_request(conn, reader, REPO, ISSUE, PR)
    metrics = outcome_tracker.evaluate_task(conn, REPO, ISSUE)
    assert metrics.maintainer_feedback == "Please add a test."


def test_metrics_report_has_every_spec_metric(conn) -> None:
    assert set(outcome_tracker.metrics_report(conn)) == set(outcome_tracker.SPEC_METRICS)


def test_metrics_report_on_empty_database_is_all_zero(conn) -> None:
    report = outcome_tracker.metrics_report(conn)
    for name in outcome_tracker.SPEC_METRICS:
        assert report[name] == 0 or report[name] == 0.0


def test_metrics_report_time_saved_is_not_fabricated(conn) -> None:
    """A constant estimate would be a fabricated benchmark (SPEC section 15)."""
    seed_task(conn)
    outcome_tracker.track_pull_request(conn, FakeReader(), REPO, ISSUE, PR)
    report = outcome_tracker.metrics_report(conn)
    assert report["time_saved_hours"] == 0.0


def test_metrics_report_merge_rate(conn) -> None:
    seed_task(conn)
    outcome_tracker.track_pull_request(conn, FakeReader(), REPO, ISSUE, PR)
    report = outcome_tracker.metrics_report(conn)
    assert report["pr_merge_rate"] == 0.0

    merged = FakeReader(
        pr={
            "number": PR,
            "html_url": "u",
            "state": "closed",
            "merged": True,
            "merged_at": "2026-10-24T09:00:00Z",
        }
    )
    outcome_tracker.track_pull_request(conn, merged, REPO, ISSUE, PR)
    report = outcome_tracker.metrics_report(conn)
    assert report["pr_merge_rate"] == 1.0


def test_metrics_report_counts_critical_audit_events_as_incidents(conn) -> None:
    from forgeflow import audit

    assert outcome_tracker.metrics_report(conn)["security_incidents"] == 0
    audit.record(conn, "security.test", severity="critical", detail="simulated")
    assert outcome_tracker.metrics_report(conn)["security_incidents"] == 1


def test_schema_is_created_by_init_db(conn) -> None:
    """init_db must create the outcome tables so a fresh database works."""
    names = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"pull_request_outcomes", "pr_reviews", "outcome_metrics"} <= names
