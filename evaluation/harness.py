"""Compute and render the SPEC section 11 evaluation metrics.

The harness is deliberately honest:

* A rate whose denominator is zero is reported as ``not enough data``, never as
  ``0.0``. Zero would read as "measured, and the answer is none".
* ``time_saved_hours`` is reported as ``not measured``. ForgeFlow does not time a
  human baseline, and a constant estimate would be a fabricated benchmark.
* The patch cases are reproducible diff-application checks, not SWE-bench. No
  benchmark performance is claimed.

Everything runs offline against an in-memory SQLite database built from
:mod:`evaluation.fixtures`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from evaluation.fixtures import (
    FIXTURE_REPO,
    PATCH_CASES,
    SWE_BENCH_NOTE,
    build_fixture_db,
    per_task_metrics,
)
from forgeflow.diff_apply import DiffApplyError, apply_diff
from forgeflow.outcome_tracker import metrics_report

#: Sentinels used in the rendered report. Kept as constants so the wording is
#: consistent and greppable in tests.
NOT_ENOUGH_DATA = "not enough data"
NOT_MEASURED = "not measured"


@dataclass(frozen=True)
class Metric:
    """One reported metric: a value plus the denominator it was computed over.

    ``denominator`` of zero means there was nothing to measure, so the display
    string is :data:`NOT_ENOUGH_DATA` rather than ``0.0``.
    """

    name: str
    value: float | int
    denominator: int
    unit: str = "rate"

    @property
    def has_data(self) -> bool:
        return self.denominator > 0

    @property
    def display(self) -> str:
        if not self.has_data:
            return NOT_ENOUGH_DATA
        if self.unit == "usd":
            return f"${self.value:.4f}"
        if self.unit == "count":
            return str(self.value)
        return f"{self.value:.4f}"


@dataclass(frozen=True)
class PatchCaseResult:
    """The outcome of one reproducible patch-application case."""

    name: str
    ok: bool
    detail: str


@dataclass
class EvaluationReport:
    """Everything the harness measured, in one object."""

    metrics: list[Metric]
    per_task: list[tuple[int, dict[str, object]]] = field(default_factory=list)
    patch_cases: list[PatchCaseResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def patch_case_pass_rate(self) -> Metric:
        total = len(self.patch_cases)
        passed = sum(1 for c in self.patch_cases if c.ok)
        return Metric(
            name="patch_case_pass_rate",
            value=round(passed / total, 4) if total else 0.0,
            denominator=total,
        )


def _run_patch_cases() -> list[PatchCaseResult]:
    results: list[PatchCaseResult] = []
    for case in PATCH_CASES:
        try:
            applied = apply_diff(case.diff, dict(case.base_files))
        except DiffApplyError as exc:
            ok = not case.should_apply
            results.append(PatchCaseResult(case.name, ok, f"refused: {exc}"))
            continue
        if not case.should_apply:
            results.append(PatchCaseResult(case.name, False, "applied but should have refused"))
            continue
        got = applied.get("notes.md", "")
        ok = got == case.expected_content
        results.append(
            PatchCaseResult(
                case.name,
                ok,
                "content matches" if ok else f"content mismatch: {got!r}",
            )
        )
    return results


def collect_metrics(conn: sqlite3.Connection) -> EvaluationReport:
    """Compute the SPEC section 11 metrics over whatever the database holds."""
    raw = metrics_report(conn)
    tracked = len(
        {
            (row["repo_full_name"], row["issue_number"], row["pr_number"])
            for row in conn.execute(
                "SELECT repo_full_name, issue_number, pr_number FROM pull_request_outcomes"
            )
        }
    )
    executed = 0
    for row in conn.execute("SELECT DISTINCT repo_full_name, issue_number FROM patches"):
        patch = _latest_patch_sha(conn, row["repo_full_name"], row["issue_number"])
        if patch is None:
            continue
        for check in conn.execute(
            "SELECT status FROM validation_results WHERE repo_full_name = ? AND issue_number = ? "
            "AND patch_sha256 = ?",
            (row["repo_full_name"], row["issue_number"], patch),
        ):
            if str(check["status"]) in {"passed", "failed", "errored"}:
                executed += 1

    metrics = [
        Metric("issue_relevance_rate", raw["issue_relevance_rate"], tracked),
        Metric("patch_review_acceptance_rate", raw["patch_review_acceptance_rate"], tracked),
        Metric("test_pass_rate", raw["test_pass_rate"], executed),
        Metric("pr_merge_rate", raw["pr_merge_rate"], tracked),
        Metric("substantive_revision_rate", raw["substantive_revision_rate"], tracked),
        Metric("cost_per_reviewed_patch", raw["cost_per_reviewed_patch"], tracked, unit="usd"),
        Metric("task_failure_rate", raw["task_failure_rate"], tracked),
        Metric("security_incidents", raw["security_incidents"], 1, unit="count"),
    ]

    per_task = [
        (
            issue_number,
            {
                "patch_usefulness": m.patch_usefulness,
                "test_quality": m.test_quality,
                "complexity": m.complexity,
                "maintainer_feedback": m.maintainer_feedback,
            },
        )
        for issue_number, m in per_task_metrics(conn)
    ]

    notes = [
        "time_saved_hours: " + NOT_MEASURED,
        SWE_BENCH_NOTE,
    ]
    return EvaluationReport(
        metrics=metrics,
        per_task=per_task,
        patch_cases=_run_patch_cases(),
        notes=notes,
    )


def _latest_patch_sha(conn: sqlite3.Connection, repo: str, issue: int) -> str | None:
    row = conn.execute(
        "SELECT patch_sha256 FROM patches WHERE repo_full_name = ? AND issue_number = ? "
        "ORDER BY id DESC LIMIT 1",
        (repo, issue),
    ).fetchone()
    return str(row["patch_sha256"]) if row else None


def render_report(report: EvaluationReport) -> str:
    """Render a report as plain text, stable for snapshotting."""
    lines: list[str] = []
    lines.append("ForgeFlow evaluation report (SPEC section 11)")
    lines.append("=" * 48)
    lines.append("")
    lines.append(f"fixture repository: {FIXTURE_REPO}")
    lines.append("")
    lines.append("Aggregate metrics")
    lines.append("-" * 48)
    for metric in report.metrics:
        denom = f" (n={metric.denominator})" if metric.unit != "count" else ""
        lines.append(f"  {metric.name:<32} {metric.display}{denom}")
    lines.append(f"  {'time_saved_hours':<32} {NOT_MEASURED}")
    lines.append("")
    lines.append("Per-task fields")
    lines.append("-" * 48)
    for issue_number, fields in report.per_task:
        lines.append(f"  issue #{issue_number}")
        lines.append(f"    patch_usefulness   {fields['patch_usefulness']}")
        lines.append(f"    test_quality       {fields['test_quality']}")
        lines.append(f"    complexity         {fields['complexity']}")
        feedback = str(fields["maintainer_feedback"]).replace("\n", " ")
        lines.append(f"    maintainer_feedback {feedback!r}")
    lines.append("")
    lines.append("Reproducible patch cases (not SWE-bench)")
    lines.append("-" * 48)
    for case in report.patch_cases:
        mark = "ok" if case.ok else "FAIL"
        lines.append(f"  [{mark}] {case.name}: {case.detail}")
    lines.append(f"  patch_case_pass_rate: {report.patch_case_pass_rate.display}")
    lines.append("")
    lines.append("Notes")
    lines.append("-" * 48)
    for note in report.notes:
        lines.append(f"  - {note}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    """Entry point: build the fixture database and print the report."""
    conn = build_fixture_db()
    try:
        report = collect_metrics(conn)
    finally:
        conn.close()
    print(render_report(report))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via -m
    raise SystemExit(main())
