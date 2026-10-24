"""Markdown report generation.

Writes a human-readable report of the ranked issues and the issues that were
excluded, so every decision ForgeFlow made is visible and auditable.

Reports are written under ``data/reports/``, which is gitignored (SPEC section 6
requires keeping local databases and logs out of version control).

The report always carries the caveat from SPEC section 5: the scores are initial
heuristics, not proven predictors of whether a pull request will be accepted.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from forgeflow.dedupe import DroppedIssue
from forgeflow.models import DEFAULT_WEIGHTS, SCORE_FACTORS, RankedIssue
from forgeflow.ranking import adjusted_total, total_for

# The caveat is required by SPEC section 5 and is emitted in every report.
HEURISTIC_CAVEAT = (
    "These scores are **initial heuristics, not proven predictors of PR "
    "acceptance**. SPEC section 5 states this explicitly. A high score means an "
    "issue matched the configured signals; it does not mean the issue is suitable "
    "or that a pull request would be accepted. Always check the issue's current "
    "state and the repository's contribution instructions before starting work."
)

DEFAULT_REPORT_DIR = Path("data/reports")


def default_report_path(directory: str | Path = DEFAULT_REPORT_DIR) -> Path:
    """Return a timestamped report path under the given directory."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return Path(directory) / f"phase1-report-{stamp}.md"


def _reason_text(reason: object) -> str:
    """Render a reason as readable text, whatever type it arrives as."""
    return str(getattr(reason, "value", reason))


def render_report(
    ranked: Sequence[RankedIssue],
    dropped: Iterable[DroppedIssue] = (),
    weights: Mapping[str, float] | None = None,
    *,
    generated_at: datetime | None = None,
    source: str = "GitHub discovery",
) -> str:
    """Render the full Markdown report as a string.

    Kept separate from file writing so it can be tested without touching disk.
    """
    active_weights = dict(weights or DEFAULT_WEIGHTS)
    dropped_list = list(dropped)
    moment = generated_at or datetime.now(UTC)

    lines: list[str] = [
        "# ForgeFlow Phase 1 Report",
        "",
        f"Generated: {moment.isoformat()}",
        f"Source: {source}",
        "",
        f"- Ranked issues: {len(ranked)}",
        f"- Excluded issues: {len(dropped_list)}",
        "",
        "> **Caveat.** " + HEURISTIC_CAVEAT,
        "",
        "## Ranking weights",
        "",
        "| Factor | Weight |",
        "| --- | ---: |",
    ]
    for factor in SCORE_FACTORS:
        lines.append(f"| {factor} | {active_weights.get(factor, 0.0):.2f} |")

    lines.extend(["", "## Ranked issues", ""])

    if not ranked:
        lines.append("No issues were ranked.")
    else:
        for entry in ranked:
            issue = entry.issue
            total = adjusted_total(entry.score, active_weights, entry.outcome_adjustment)
            lines.extend(
                [
                    f"### {entry.rank}. {issue.repo_full_name}#{issue.number}",
                    "",
                    f"- **Repository:** {issue.repo_full_name}",
                    f"- **Issue number:** {issue.number}",
                    f"- **Title:** {issue.title}",
                    f"- **URL:** {issue.url or '(none)'}",
                    f"- **Total score:** {total:.2f} / 100",
                    f"- **Labels:** {', '.join(issue.labels) if issue.labels else '(none)'}",
                    "",
                    "| Factor | Score | Weight | Weighted | Reason |",
                    "| --- | ---: | ---: | ---: | --- |",
                ]
            )
            if entry.outcome_adjustment:
                base_total = total_for(entry.score, active_weights)
                lines.append(
                    f"- **Outcome adjustment:** {entry.outcome_adjustment:+.2f} points "
                    f"(from tracked PR history; base {base_total:.2f}, "
                    f"capped and off by default per SPEC section 11)"
                )
            for factor in SCORE_FACTORS:
                value = float(getattr(entry.score, factor))
                weight = active_weights.get(factor, 0.0)
                reason = entry.score.reasons.get(factor, "(no reason recorded)")
                reason = reason.replace("|", "\\|")
                lines.append(
                    f"| {factor} | {value:.2f} | {weight:.2f} | "
                    f"{value * weight * 100:.2f} | {reason} |"
                )
            lines.append("")

    lines.extend(["## Excluded issues", ""])

    if not dropped_list:
        lines.append("No issues were excluded.")
    else:
        lines.extend(
            [
                "Every exclusion is listed with the reason it was dropped.",
                "",
                "| Repository | Issue | Title | Reason | Detail |",
                "| --- | ---: | --- | --- | --- |",
            ]
        )
        for item in dropped_list:
            title = item.issue.title.replace("|", "\\|")
            detail = item.detail.replace("|", "\\|")
            lines.append(
                f"| {item.issue.repo_full_name} | {item.issue.number} | {title} | "
                f"{_reason_text(item.reason)} | {detail} |"
            )
        lines.append("")
        lines.extend(["### Exclusion counts", ""])
        counts: dict[str, int] = {}
        for item in dropped_list:
            key = _reason_text(item.reason)
            counts[key] = counts.get(key, 0) + 1
        for reason, count in sorted(counts.items()):
            lines.append(f"- `{reason}`: {count}")

    lines.append("")
    return "\n".join(lines)


def write_report(
    path: str | Path,
    ranked: Sequence[RankedIssue],
    dropped: Iterable[DroppedIssue] = (),
    weights: Mapping[str, float] | None = None,
    *,
    generated_at: datetime | None = None,
    source: str = "GitHub discovery",
) -> Path:
    """Render the report and write it to ``path``. Returns the path written."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = render_report(ranked, dropped, weights, generated_at=generated_at, source=source)
    target.write_text(content, encoding="utf-8")
    return target
