"""Tests for Markdown report generation."""

from __future__ import annotations

from datetime import UTC, datetime

from forgeflow.dedupe import DroppedIssue, DropReason
from forgeflow.models import DEFAULT_WEIGHTS, SCORE_FACTORS, Issue, IssueScore, RankedIssue
from forgeflow.ranking import total_for
from forgeflow.report import (
    HEURISTIC_CAVEAT,
    default_report_path,
    render_report,
    write_report,
)

REPO = "pydantic/pydantic"
NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)


def make_ranked(number: int = 1234, rank: int = 1) -> RankedIssue:
    issue = Issue(
        repo_full_name=REPO,
        number=number,
        title=f"Bug: validation fails on nested models {number}",
        url=f"https://github.com/{REPO}/issues/{number}",
        labels=["bug"],
    )
    score = IssueScore(
        repo_full_name=REPO,
        issue_number=number,
        skill_relevance=0.9,
        issue_clarity=0.8,
        feasible_scope=0.7,
        testability=0.6,
        repository_health=0.5,
        duplicate_conflict_risk=1.0,
        reasons={factor: f"reason for {factor}" for factor in SCORE_FACTORS},
    )
    return RankedIssue(issue=issue, score=score, rank=rank)


def make_dropped(number: int = 99, reason: DropReason = DropReason.CLOSED) -> DroppedIssue:
    issue = Issue(
        repo_full_name=REPO,
        number=number,
        title=f"Excluded issue {number}",
        url=f"https://github.com/{REPO}/issues/{number}",
    )
    return DroppedIssue(issue=issue, reason=reason, detail="because of a test reason")


# --- caveat -----------------------------------------------------------------


def test_report_contains_the_heuristic_caveat() -> None:
    text = render_report([make_ranked()], [], DEFAULT_WEIGHTS, generated_at=NOW)
    assert "heuristics" in text
    assert "not proven predictors" in text
    assert "PR acceptance" in text


def test_caveat_constant_mentions_spec_section_5() -> None:
    assert "SPEC section 5" in HEURISTIC_CAVEAT


def test_caveat_present_even_with_no_issues() -> None:
    text = render_report([], [], DEFAULT_WEIGHTS, generated_at=NOW)
    assert "heuristics" in text


# --- required per-issue fields ----------------------------------------------


def test_report_includes_every_required_field() -> None:
    text = render_report([make_ranked()], [], DEFAULT_WEIGHTS, generated_at=NOW)
    assert REPO in text
    assert "1234" in text
    assert "Bug: validation fails on nested models 1234" in text
    assert f"https://github.com/{REPO}/issues/1234" in text
    assert "Total score" in text
    assert "/ 100" in text


def test_report_includes_every_factor_and_its_reason() -> None:
    text = render_report([make_ranked()], [], DEFAULT_WEIGHTS, generated_at=NOW)
    for factor in SCORE_FACTORS:
        assert factor in text
        assert f"reason for {factor}" in text


def test_report_shows_correct_total() -> None:
    entry = make_ranked()
    expected = total_for(entry.score, DEFAULT_WEIGHTS)
    text = render_report([entry], [], DEFAULT_WEIGHTS, generated_at=NOW)
    assert f"{expected:.2f}" in text


def test_report_includes_weights_table() -> None:
    text = render_report([make_ranked()], [], DEFAULT_WEIGHTS, generated_at=NOW)
    assert "Ranking weights" in text
    assert "0.25" in text  # skill_relevance


# --- excluded issues --------------------------------------------------------


def test_report_lists_excluded_issues_with_reasons() -> None:
    dropped = [
        make_dropped(11, DropReason.CLOSED),
        make_dropped(12, DropReason.ASSIGNED),
        make_dropped(13, DropReason.LINKED_OPEN_PR),
    ]
    text = render_report([make_ranked()], dropped, DEFAULT_WEIGHTS, generated_at=NOW)
    assert "Excluded issues" in text
    assert "closed" in text
    assert "assigned" in text
    assert "linked_open_pr" in text
    assert "because of a test reason" in text


def test_report_states_when_nothing_was_excluded() -> None:
    text = render_report([make_ranked()], [], DEFAULT_WEIGHTS, generated_at=NOW)
    assert "No issues were excluded." in text


def test_report_states_when_nothing_was_ranked() -> None:
    text = render_report([], [], DEFAULT_WEIGHTS, generated_at=NOW)
    assert "No issues were ranked." in text


def test_exclusion_counts_section() -> None:
    dropped = [make_dropped(1, DropReason.CLOSED), make_dropped(2, DropReason.CLOSED)]
    text = render_report([], dropped, DEFAULT_WEIGHTS, generated_at=NOW)
    assert "Exclusion counts" in text
    assert "`closed`: 2" in text


# --- ordering and rendering -------------------------------------------------


def test_report_renders_multiple_issues_in_rank_order() -> None:
    ranked = [make_ranked(100, rank=1), make_ranked(200, rank=2)]
    text = render_report(ranked, [], DEFAULT_WEIGHTS, generated_at=NOW)
    assert text.index("issues/100") < text.index("issues/200")


def test_report_escapes_pipes_in_exclusion_table() -> None:
    """Pipes inside table cells must be escaped or the table breaks."""
    dropped = make_dropped(9)
    dropped = DroppedIssue(
        issue=dropped.issue.model_copy(update={"title": "x | y"}),
        reason=dropped.reason,
        detail="d | e",
    )
    text = render_report([], [dropped], DEFAULT_WEIGHTS, generated_at=NOW)
    assert "x \\| y" in text
    assert "d \\| e" in text


def test_report_ranked_title_is_a_bullet_not_a_table_cell() -> None:
    """The ranked title sits in a bullet list, so a raw pipe is safe there."""
    entry = make_ranked()
    entry = entry.model_copy(update={"issue": entry.issue.model_copy(update={"title": "a | b"})})
    text = render_report([entry], [], DEFAULT_WEIGHTS, generated_at=NOW)
    assert "- **Title:** a | b" in text


def test_report_handles_missing_url() -> None:
    entry = make_ranked()
    entry = entry.model_copy(update={"issue": entry.issue.model_copy(update={"url": ""})})
    text = render_report([entry], [], DEFAULT_WEIGHTS, generated_at=NOW)
    assert "(none)" in text


def test_report_records_generation_time_and_source() -> None:
    text = render_report(
        [make_ranked()], [], DEFAULT_WEIGHTS, generated_at=NOW, source="test source"
    )
    assert NOW.isoformat() in text
    assert "test source" in text


# --- writing to disk --------------------------------------------------------


def test_write_report_creates_file(tmp_path) -> None:
    target = tmp_path / "nested" / "report.md"
    written = write_report(target, [make_ranked()], [], DEFAULT_WEIGHTS, generated_at=NOW)
    assert written == target
    assert target.is_file()
    assert "ForgeFlow Phase 1 Report" in target.read_text(encoding="utf-8")


def test_write_report_creates_parent_directories(tmp_path) -> None:
    target = tmp_path / "a" / "b" / "c" / "report.md"
    write_report(target, [], [], DEFAULT_WEIGHTS, generated_at=NOW)
    assert target.is_file()


def test_default_report_path_is_under_data_reports() -> None:
    path = default_report_path()
    assert path.parent == __import__("pathlib").Path("data/reports")
    assert path.name.startswith("phase1-report-")
    assert path.suffix == ".md"


def test_default_report_path_accepts_directory_override(tmp_path) -> None:
    path = default_report_path(tmp_path)
    assert path.parent == tmp_path
