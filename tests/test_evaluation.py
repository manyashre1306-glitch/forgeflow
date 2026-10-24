"""Tests for the evaluation harness (SPEC section 11).

These assert the metric arithmetic over the known fixtures, the "not enough
data" behaviour, and that the reproducible patch cases behave as described. They
also run the harness end to end so ``python -m evaluation`` cannot silently
break.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from evaluation import harness
from evaluation.fixtures import (
    FIXTURE_REPO,
    FIXTURE_TASKS,
    build_fixture_db,
    record_critical_incident,
)


@pytest.fixture
def fixture_conn():
    conn = build_fixture_db()
    try:
        yield conn
    finally:
        conn.close()


def _metric(report, name):
    return next(m for m in report.metrics if m.name == name)


def test_fixture_set_is_documented():
    assert FIXTURE_TASKS, "the harness must have fixtures"
    assert all(t.issue_number >= 1 for t in FIXTURE_TASKS)


def test_relevance_rate_counts_engagement_over_tracked(fixture_conn):
    report = harness.collect_metrics(fixture_conn)
    # 4 tracked tasks; 3 engaged (merged, changes-requested, ... merged only here)
    # Task 1 merged, task 3 changes-requested -> 2 engaged; task 2 open no review,
    # task 4 closed unmerged -> not engaged.
    metric = _metric(report, "issue_relevance_rate")
    assert metric.denominator == 4
    assert metric.value == pytest.approx(2 / 4)


def test_merge_rate(fixture_conn):
    report = harness.collect_metrics(fixture_conn)
    metric = _metric(report, "pr_merge_rate")
    assert metric.denominator == 4
    assert metric.value == pytest.approx(1 / 4)


def test_substantive_revision_rate(fixture_conn):
    report = harness.collect_metrics(fixture_conn)
    metric = _metric(report, "substantive_revision_rate")
    assert metric.denominator == 4
    assert metric.value == pytest.approx(1 / 4)


def test_task_failure_rate(fixture_conn):
    report = harness.collect_metrics(fixture_conn)
    metric = _metric(report, "task_failure_rate")
    assert metric.denominator == 4
    assert metric.value == pytest.approx(1 / 4)


def test_test_pass_rate_over_executed_checks(fixture_conn):
    report = harness.collect_metrics(fixture_conn)
    metric = _metric(report, "test_pass_rate")
    # 4 tasks with patches, 2 checks each = 8 executed; task 3 has one failure.
    assert metric.denominator == 8
    assert metric.value == pytest.approx(7 / 8)


def test_cost_per_reviewed_patch(fixture_conn):
    report = harness.collect_metrics(fixture_conn)
    metric = _metric(report, "cost_per_reviewed_patch")
    assert metric.denominator == 4
    assert metric.value == pytest.approx((0.10 + 0.20 + 0.30 + 0.40) / 4)


def test_security_incidents_zero_by_default(fixture_conn):
    report = harness.collect_metrics(fixture_conn)
    assert _metric(report, "security_incidents").value == 0


def test_security_incidents_counts_critical(fixture_conn):
    record_critical_incident(fixture_conn)
    report = harness.collect_metrics(fixture_conn)
    assert _metric(report, "security_incidents").value == 1


def test_time_saved_is_not_measured(fixture_conn):
    report = harness.collect_metrics(fixture_conn)
    assert any(harness.NOT_MEASURED in note for note in report.notes)
    rendered = harness.render_report(report)
    assert "time_saved_hours" in rendered
    assert harness.NOT_MEASURED in rendered


def test_not_enough_data_when_no_tracked_prs():
    from forgeflow import storage

    conn = storage.connect(":memory:")
    storage.init_db(conn)
    try:
        report = harness.collect_metrics(conn)
        for metric in report.metrics:
            if metric.name == "security_incidents":
                continue
            assert not metric.has_data, metric.name
            assert metric.display == harness.NOT_ENOUGH_DATA
    finally:
        conn.close()


def test_per_task_fields_present(fixture_conn):
    report = harness.collect_metrics(fixture_conn)
    assert report.per_task, "fixtures have patches"
    for _issue, fields in report.per_task:
        for key in ("patch_usefulness", "test_quality", "complexity", "maintainer_feedback"):
            assert key in fields


def test_maintainer_feedback_is_recorded_verbatim(fixture_conn):
    report = harness.collect_metrics(fixture_conn)
    feedback = {issue: f["maintainer_feedback"] for issue, f in report.per_task}
    assert feedback[3] == "Please add a regression test before this can land."


def test_patch_usefulness_matches_outcome(fixture_conn):
    report = harness.collect_metrics(fixture_conn)
    usefulness = {issue: f["patch_usefulness"] for issue, f in report.per_task}
    assert usefulness[1] == 1.0  # merged
    assert usefulness[2] == 0.25  # open, no approval
    assert usefulness[4] == 0.0  # closed unmerged


def test_patch_cases_all_pass(fixture_conn):
    report = harness.collect_metrics(fixture_conn)
    failures = [c for c in report.patch_cases if not c.ok]
    assert not failures, failures
    assert report.patch_case_pass_rate.value == 1.0


def test_swe_bench_is_not_claimed():
    assert "not run here" in harness.SWE_BENCH_NOTE
    assert "No benchmark performance is claimed" in harness.SWE_BENCH_NOTE


def test_harness_runs_as_a_module():
    result = subprocess.run(
        [sys.executable, "-m", "evaluation"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert FIXTURE_REPO in result.stdout
    assert harness.NOT_MEASURED in result.stdout
