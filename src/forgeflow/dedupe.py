"""Deduplication and eligibility filtering.

Two distinct operations, kept separate because they answer different questions:

* ``deduplicate`` collapses the same issue seen more than once (across search
  queries or runs) to a single entry.
* ``filter_issues`` applies the eligibility rules from SPEC section 2B: closed
  issues, pull requests, assigned issues, issues with linked open PRs, and
  excluded labels.

Every exclusion records a machine-readable ``reason`` and a human-readable
``detail``, so reports can explain what was dropped and why.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from forgeflow.models import Issue


class DropReason(StrEnum):
    """Why an issue was excluded from consideration."""

    DUPLICATE = "duplicate"
    CLOSED = "closed"
    PULL_REQUEST = "pull_request"
    ASSIGNED = "assigned"
    LINKED_OPEN_PR = "linked_open_pr"
    EXCLUDED_LABEL = "excluded_label"


@dataclass(frozen=True)
class DroppedIssue:
    """An issue that was excluded, with the reason."""

    issue: Issue
    reason: DropReason
    detail: str

    @property
    def key(self) -> str:
        return f"{self.issue.repo_full_name}#{self.issue.number}"


@dataclass
class FilterResult:
    """Issues that survived filtering, plus everything that was dropped."""

    kept: list[Issue] = field(default_factory=list)
    dropped: list[DroppedIssue] = field(default_factory=list)

    def drop_reasons(self) -> dict[str, int]:
        """Count drops by reason, for reporting."""
        counts: dict[str, int] = {}
        for item in self.dropped:
            counts[item.reason.value] = counts.get(item.reason.value, 0) + 1
        return counts


def issue_key(issue: Issue) -> tuple[str, int]:
    """The identity of an issue: repository plus number."""
    return (issue.repo_full_name, issue.number)


def deduplicate(issues: Iterable[Issue]) -> tuple[list[Issue], list[DroppedIssue]]:
    """Collapse duplicate issues, keeping the first occurrence.

    Order is preserved so results stay deterministic. Duplicates are reported
    rather than silently discarded.
    """
    seen: set[tuple[str, int]] = set()
    unique: list[Issue] = []
    duplicates: list[DroppedIssue] = []
    for issue in issues:
        key = issue_key(issue)
        if key in seen:
            duplicates.append(
                DroppedIssue(
                    issue=issue,
                    reason=DropReason.DUPLICATE,
                    detail="duplicate entry for the same repository and issue number",
                )
            )
            continue
        seen.add(key)
        unique.append(issue)
    return unique, duplicates


def _is_assigned(issue: Issue) -> bool:
    return bool(issue.assignees)


def _excluded_label(issue: Issue, exclude_labels: Iterable[str]) -> str | None:
    excluded = {label.strip().lower() for label in exclude_labels}
    for label in issue.labels:
        if label.strip().lower() in excluded:
            return label
    return None


def filter_issues(
    issues: Iterable[Issue],
    *,
    exclude_labels: Iterable[str] = (),
) -> FilterResult:
    """Apply the eligibility rules, recording a reason for every drop.

    An issue may match several rules; the first match in the order below wins, so
    the reported reason is deterministic.
    """
    excluded = list(exclude_labels)
    result = FilterResult()
    for issue in issues:
        if issue.is_pull_request:
            result.dropped.append(
                DroppedIssue(issue, DropReason.PULL_REQUEST, "item is a pull request, not an issue")
            )
            continue
        if issue.state != "open":
            result.dropped.append(
                DroppedIssue(issue, DropReason.CLOSED, f"issue state is '{issue.state}'")
            )
            continue
        if _is_assigned(issue):
            result.dropped.append(
                DroppedIssue(
                    issue,
                    DropReason.ASSIGNED,
                    f"already assigned to {', '.join(issue.assignees)}",
                )
            )
            continue
        if issue.linked_open_prs:
            numbers = ", ".join(f"#{number}" for number in issue.linked_open_prs)
            result.dropped.append(
                DroppedIssue(
                    issue,
                    DropReason.LINKED_OPEN_PR,
                    f"linked to open pull request(s): {numbers}",
                )
            )
            continue
        matched_label = _excluded_label(issue, excluded)
        if matched_label is not None:
            result.dropped.append(
                DroppedIssue(
                    issue,
                    DropReason.EXCLUDED_LABEL,
                    f"carries excluded label '{matched_label}'",
                )
            )
            continue
        result.kept.append(issue)
    return result
