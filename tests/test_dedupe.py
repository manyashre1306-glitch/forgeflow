"""Tests for deduplication and eligibility filtering."""

from __future__ import annotations

from forgeflow.dedupe import (
    DropReason,
    deduplicate,
    filter_issues,
    issue_key,
)
from forgeflow.models import Issue

REPO = "pydantic/pydantic"
EXCLUDE = ["duplicate", "wontfix", "invalid", "status: wontfix"]


def make_issue(number: int = 1, **overrides) -> Issue:
    data = {"repo_full_name": REPO, "number": number, "title": f"issue {number}"}
    data.update(overrides)
    return Issue(**data)


# --- identity dedupe --------------------------------------------------------


def test_issue_key_is_repo_and_number() -> None:
    assert issue_key(make_issue(7)) == (REPO, 7)


def test_deduplicate_collapses_same_issue_from_two_queries() -> None:
    """The same issue returned by two searches must yield one entry."""
    issues = [make_issue(5, title="first"), make_issue(5, title="second")]
    unique, duplicates = deduplicate(issues)
    assert len(unique) == 1
    assert len(duplicates) == 1
    assert duplicates[0].reason is DropReason.DUPLICATE
    assert duplicates[0].key == f"{REPO}#5"


def test_deduplicate_keeps_first_occurrence() -> None:
    issues = [make_issue(5, title="first"), make_issue(5, title="second")]
    unique, _ = deduplicate(issues)
    assert unique[0].title == "first"


def test_deduplicate_preserves_order() -> None:
    issues = [make_issue(3), make_issue(1), make_issue(2)]
    unique, _ = deduplicate(issues)
    assert [issue.number for issue in unique] == [3, 1, 2]


def test_deduplicate_distinguishes_repositories() -> None:
    issues = [make_issue(1), make_issue(1, repo_full_name="psf/requests")]
    unique, duplicates = deduplicate(issues)
    assert len(unique) == 2
    assert duplicates == []


def test_deduplicate_no_duplicates_returns_all() -> None:
    issues = [make_issue(1), make_issue(2)]
    unique, duplicates = deduplicate(issues)
    assert len(unique) == 2
    assert duplicates == []


# --- eligibility filtering --------------------------------------------------


def test_filter_keeps_clean_open_issue() -> None:
    result = filter_issues([make_issue(1)], exclude_labels=EXCLUDE)
    assert len(result.kept) == 1
    assert result.dropped == []


def test_filter_drops_closed_issue_with_reason() -> None:
    result = filter_issues([make_issue(1, state="closed")], exclude_labels=EXCLUDE)
    assert result.kept == []
    assert result.dropped[0].reason is DropReason.CLOSED
    assert "closed" in result.dropped[0].detail


def test_filter_drops_pull_request_with_reason() -> None:
    result = filter_issues([make_issue(1, is_pull_request=True)], exclude_labels=EXCLUDE)
    assert result.kept == []
    assert result.dropped[0].reason is DropReason.PULL_REQUEST


def test_filter_drops_assigned_issue_with_reason() -> None:
    result = filter_issues([make_issue(1, assignees=["someone"])], exclude_labels=EXCLUDE)
    assert result.kept == []
    assert result.dropped[0].reason is DropReason.ASSIGNED
    assert "someone" in result.dropped[0].detail


def test_filter_drops_issue_with_linked_open_pr() -> None:
    result = filter_issues([make_issue(1, linked_open_prs=[42])], exclude_labels=EXCLUDE)
    assert result.kept == []
    assert result.dropped[0].reason is DropReason.LINKED_OPEN_PR
    assert "#42" in result.dropped[0].detail


def test_filter_drops_issue_with_multiple_linked_prs() -> None:
    result = filter_issues([make_issue(1, linked_open_prs=[42, 43])], exclude_labels=EXCLUDE)
    assert "#42" in result.dropped[0].detail
    assert "#43" in result.dropped[0].detail


def test_filter_drops_duplicate_label() -> None:
    result = filter_issues([make_issue(1, labels=["duplicate"])], exclude_labels=EXCLUDE)
    assert result.kept == []
    assert result.dropped[0].reason is DropReason.EXCLUDED_LABEL
    assert "duplicate" in result.dropped[0].detail


def test_filter_drops_wontfix_label() -> None:
    result = filter_issues([make_issue(1, labels=["wontfix"])], exclude_labels=EXCLUDE)
    assert result.dropped[0].reason is DropReason.EXCLUDED_LABEL


def test_filter_drops_invalid_label() -> None:
    result = filter_issues([make_issue(1, labels=["invalid"])], exclude_labels=EXCLUDE)
    assert result.dropped[0].reason is DropReason.EXCLUDED_LABEL


def test_filter_label_matching_is_case_insensitive() -> None:
    result = filter_issues([make_issue(1, labels=["Duplicate"])], exclude_labels=EXCLUDE)
    assert result.dropped[0].reason is DropReason.EXCLUDED_LABEL


def test_filter_label_matching_handles_multiword_label() -> None:
    result = filter_issues([make_issue(1, labels=["status: wontfix"])], exclude_labels=EXCLUDE)
    assert result.dropped[0].reason is DropReason.EXCLUDED_LABEL


def test_filter_keeps_issue_with_unrelated_labels() -> None:
    result = filter_issues([make_issue(1, labels=["bug", "help wanted"])], exclude_labels=EXCLUDE)
    assert len(result.kept) == 1


def test_filter_mixed_batch_splits_correctly() -> None:
    issues = [
        make_issue(1),
        make_issue(2, state="closed"),
        make_issue(3, is_pull_request=True),
        make_issue(4, assignees=["dev"]),
        make_issue(5, linked_open_prs=[7]),
        make_issue(6, labels=["duplicate"]),
        make_issue(7),
    ]
    result = filter_issues(issues, exclude_labels=EXCLUDE)
    assert [issue.number for issue in result.kept] == [1, 7]
    assert len(result.dropped) == 5


def test_filter_precedence_is_deterministic() -> None:
    """An issue matching several rules reports the first rule in order."""
    issue = make_issue(1, is_pull_request=True, state="closed", assignees=["x"])
    result = filter_issues([issue], exclude_labels=EXCLUDE)
    assert result.dropped[0].reason is DropReason.PULL_REQUEST


def test_drop_reasons_counts_by_reason() -> None:
    issues = [
        make_issue(1, state="closed"),
        make_issue(2, state="closed"),
        make_issue(3, is_pull_request=True),
    ]
    result = filter_issues(issues, exclude_labels=EXCLUDE)
    assert result.drop_reasons() == {"closed": 2, "pull_request": 1}


def test_every_drop_has_a_non_empty_reason_and_detail() -> None:
    issues = [
        make_issue(1, state="closed"),
        make_issue(2, is_pull_request=True),
        make_issue(3, assignees=["dev"]),
        make_issue(4, linked_open_prs=[9]),
        make_issue(5, labels=["duplicate"]),
    ]
    result = filter_issues(issues, exclude_labels=EXCLUDE)
    assert len(result.dropped) == 5
    for dropped in result.dropped:
        assert dropped.reason
        assert dropped.detail


# --- opt-out labels (SPEC section 2B) ---------------------------------------


def test_filter_drops_opted_out_issue_with_reason() -> None:
    """A repository opting out of automated contributions is excluded."""
    result = filter_issues(
        [make_issue(1, labels=["no-bot"])],
        exclude_labels=EXCLUDE,
        opt_out_labels=["no-bot", "do-not-contribute"],
    )
    assert result.kept == []
    assert result.dropped[0].reason is DropReason.OPT_OUT
    assert "opts out" in result.dropped[0].detail


def test_opt_out_takes_precedence_over_excluded_label() -> None:
    issue = make_issue(1, labels=["no-bot", "duplicate"])
    result = filter_issues([issue], exclude_labels=EXCLUDE, opt_out_labels=["no-bot"])
    assert result.dropped[0].reason is DropReason.OPT_OUT


def test_opt_out_matching_is_case_insensitive() -> None:
    result = filter_issues(
        [make_issue(1, labels=["No-Bot"])],
        exclude_labels=EXCLUDE,
        opt_out_labels=["no-bot"],
    )
    assert result.dropped[0].reason is DropReason.OPT_OUT


def test_no_opt_out_labels_configured_keeps_issue() -> None:
    result = filter_issues([make_issue(1, labels=["no-bot"])], exclude_labels=EXCLUDE)
    assert len(result.kept) == 1


def test_opt_out_reason_appears_in_drop_counts() -> None:
    result = filter_issues(
        [make_issue(1, labels=["no-bot"]), make_issue(2, labels=["no-bot"])],
        exclude_labels=EXCLUDE,
        opt_out_labels=["no-bot"],
    )
    assert result.drop_reasons() == {"opt_out": 2}
