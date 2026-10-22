"""Tests for the pure ranking functions.

No network, no database, no LLM. Every test calls ranking functions directly
with in-memory models.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from forgeflow.config import AppConfig
from forgeflow.models import DEFAULT_WEIGHTS, SCORE_FACTORS, Issue, Repository
from forgeflow.ranking import (
    explain,
    rank_issues,
    resolve_weights,
    score_duplicate_conflict_risk,
    score_feasible_scope,
    score_issue,
    score_issue_clarity,
    score_repository_health,
    score_skill_relevance,
    score_testability,
    total_for,
)

REPO = "pydantic/pydantic"
NOW = datetime(2026, 10, 22, tzinfo=UTC)

SKILL_KEYWORDS = {
    "python": ["python", "typing", "mypy"],
    "ai_ml": ["machine learning", "inference"],
    "devtools": ["pytest", "lint"],
}


def make_config(**overrides) -> AppConfig:
    data = {
        "repositories": [REPO],
        "weights": dict(DEFAULT_WEIGHTS),
        "skill_keywords": SKILL_KEYWORDS,
        "exclude_labels": ["duplicate", "wontfix", "invalid"],
    }
    data.update(overrides)
    return AppConfig(**data)


def make_issue(number: int = 1, **overrides) -> Issue:
    data = {
        "repo_full_name": REPO,
        "number": number,
        "title": "Bug: validation fails",
        "body": (
            "Steps to reproduce:\n"
            "1. define a nested model\n"
            "2. validate it\n\n"
            "Expected: validation succeeds.\n"
            "Actual: raises a ValidationError.\n\n"
            "Traceback:\n"
            "    ValidationError: invalid input\n"
        ),
        "labels": ["bug"],
    }
    data.update(overrides)
    return Issue(**data)


def make_repo(**overrides) -> Repository:
    data = {
        "full_name": REPO,
        "stars": 21000,
        "open_issues": 300,
        "has_contributing": True,
        "has_tests": True,
        "last_pushed_at": NOW - timedelta(days=3),
    }
    data.update(overrides)
    return Repository(**data)


# --- weights -----------------------------------------------------------------


def test_default_weights_sum_to_one() -> None:
    assert sum(DEFAULT_WEIGHTS.values()) == pytest.approx(1.0)


def test_default_weights_match_spec_percentages() -> None:
    """SPEC section 5: 25/20/20/15/10/10."""
    assert DEFAULT_WEIGHTS == {
        "skill_relevance": 0.25,
        "issue_clarity": 0.20,
        "feasible_scope": 0.20,
        "testability": 0.15,
        "repository_health": 0.10,
        "duplicate_conflict_risk": 0.10,
    }


def test_resolve_weights_defaults_without_config() -> None:
    assert resolve_weights(None) == DEFAULT_WEIGHTS


def test_resolve_weights_uses_config() -> None:
    custom = dict(DEFAULT_WEIGHTS)
    custom["skill_relevance"] = 0.30
    custom["issue_clarity"] = 0.15
    config = make_config(weights=custom)
    assert resolve_weights(config)["skill_relevance"] == pytest.approx(0.30)


def test_total_for_rejects_incomplete_weights() -> None:
    score = score_issue(make_issue(), make_config())
    with pytest.raises(ValueError, match="missing factors"):
        total_for(score, {"skill_relevance": 1.0})


# --- changing a weight changes the ordering ---------------------------------


def test_changing_a_weight_changes_the_ordering() -> None:
    """Two issues that trade off skill relevance against clarity swap places."""
    skilled_unclear = make_issue(
        1,
        title="python typing bug",  # strong skill relevance
        body="broken",  # no clarity signals
        labels=["bug"],
    )
    unskilled_clear = make_issue(
        2,
        title="something is wrong",
        body=(
            "Steps to reproduce the problem in detail.\n"
            "Expected the correct result, but got the wrong one.\n"
            "Traceback: Error raised here.\n" + "x" * 250
        ),
        labels=["bug"],
    )

    skill_heavy = dict(DEFAULT_WEIGHTS)
    skill_heavy["skill_relevance"] = 0.70
    skill_heavy["issue_clarity"] = 0.05
    skill_heavy["feasible_scope"] = 0.05
    skill_heavy["testability"] = 0.05
    skill_heavy["repository_health"] = 0.05
    skill_heavy["duplicate_conflict_risk"] = 0.10

    clarity_heavy = dict(DEFAULT_WEIGHTS)
    clarity_heavy["skill_relevance"] = 0.05
    clarity_heavy["issue_clarity"] = 0.70
    clarity_heavy["feasible_scope"] = 0.05
    clarity_heavy["testability"] = 0.05
    clarity_heavy["repository_health"] = 0.05
    clarity_heavy["duplicate_conflict_risk"] = 0.10

    issues = [skilled_unclear, unskilled_clear]
    repos = {REPO: make_repo()}

    first = rank_issues(issues, make_config(weights=skill_heavy), repos, now=NOW)
    second = rank_issues(issues, make_config(weights=clarity_heavy), repos, now=NOW)

    assert first[0].issue.number == 1
    assert second[0].issue.number == 2


def test_weights_change_total_but_not_factor_scores() -> None:
    issue = make_issue()
    config = make_config()
    score = score_issue(issue, config, make_repo(), now=NOW)

    low = dict(DEFAULT_WEIGHTS)
    low["skill_relevance"] = 0.01
    low["issue_clarity"] = 0.99
    high = dict(DEFAULT_WEIGHTS)
    high["skill_relevance"] = 0.99
    high["issue_clarity"] = 0.01

    assert total_for(score, low) != total_for(score, high)
    # The per-factor values themselves are weight-independent.
    assert score.skill_relevance == score_issue(issue, config).skill_relevance


# --- reasons for every factor -----------------------------------------------


def test_every_factor_has_a_non_empty_reason() -> None:
    score = score_issue(make_issue(), make_config(), make_repo(), now=NOW)
    for factor in SCORE_FACTORS:
        reason = score.reasons.get(factor)
        assert reason, f"missing reason for {factor}"
        assert reason.strip(), f"blank reason for {factor}"


def test_every_factor_has_a_reason_even_with_sparse_input() -> None:
    """A bare issue with no signals still produces a reason for each factor."""
    bare = Issue(repo_full_name=REPO, number=99, title="x", body="")
    score = score_issue(bare, make_config(), None, now=NOW)
    for factor in SCORE_FACTORS:
        assert score.reasons.get(factor, "").strip()


def test_ranked_issues_all_have_reasons() -> None:
    ranked = rank_issues(
        [make_issue(1), make_issue(2), make_issue(3)],
        make_config(),
        {REPO: make_repo()},
        now=NOW,
    )
    assert len(ranked) == 3
    for entry in ranked:
        for factor in SCORE_FACTORS:
            assert entry.score.reasons[factor]


# --- closed issues are never ranked -----------------------------------------


def test_closed_issue_is_never_ranked() -> None:
    issues = [make_issue(1), make_issue(2, state="closed")]
    ranked = rank_issues(issues, make_config(), {REPO: make_repo()}, now=NOW)
    numbers = [entry.issue.number for entry in ranked]
    assert numbers == [1]
    assert 2 not in numbers


def test_all_closed_issues_produce_empty_ranking() -> None:
    issues = [make_issue(1, state="closed"), make_issue(2, state="closed")]
    assert rank_issues(issues, make_config(), {REPO: make_repo()}, now=NOW) == []


def test_only_open_issues_are_ranked_in_a_mixed_batch() -> None:
    issues = [
        make_issue(1),
        make_issue(2, state="closed"),
        make_issue(3),
        make_issue(4, state="closed"),
    ]
    ranked = rank_issues(issues, make_config(), {REPO: make_repo()}, now=NOW)
    assert {entry.issue.number for entry in ranked} == {1, 3}


# --- total scale ------------------------------------------------------------


def test_total_is_on_a_0_to_100_scale() -> None:
    ranked = rank_issues([make_issue()], make_config(), {REPO: make_repo()}, now=NOW)
    total = total_for(ranked[0].score, DEFAULT_WEIGHTS)
    assert 0.0 <= total <= 100.0


def test_all_factors_at_one_gives_100() -> None:
    config = make_config()
    perfect = make_issue(
        title="python typing bug with pytest",
        body=("Steps to reproduce\nExpected x, actual y\nTraceback: Error\n" + "z" * 300),
        labels=["bug", "good first issue"],
    )
    score = score_issue(perfect, config, make_repo(), now=NOW)
    # Force every factor to 1.0 to prove the scale tops out at 100.
    forced = score.model_copy(update=dict.fromkeys(SCORE_FACTORS, 1.0))
    assert total_for(forced, DEFAULT_WEIGHTS) == pytest.approx(100.0)


def test_ranks_are_sequential_from_one() -> None:
    ranked = rank_issues(
        [make_issue(1), make_issue(2), make_issue(3)],
        make_config(),
        {REPO: make_repo()},
        now=NOW,
    )
    assert [entry.rank for entry in ranked] == [1, 2, 3]


def test_ranking_is_sorted_descending_by_total() -> None:
    ranked = rank_issues(
        [make_issue(1), make_issue(2), make_issue(3)],
        make_config(),
        {REPO: make_repo()},
        now=NOW,
    )
    totals = [total_for(entry.score, DEFAULT_WEIGHTS) for entry in ranked]
    assert totals == sorted(totals, reverse=True)


# --- determinism and purity -------------------------------------------------


def test_ranking_is_deterministic() -> None:
    issues = [make_issue(1), make_issue(2), make_issue(3)]
    config = make_config()
    repos = {REPO: make_repo()}
    first = rank_issues(issues, config, repos, now=NOW)
    second = rank_issues(issues, config, repos, now=NOW)
    assert [entry.issue.number for entry in first] == [entry.issue.number for entry in second]
    assert [total_for(entry.score, DEFAULT_WEIGHTS) for entry in first] == [
        total_for(entry.score, DEFAULT_WEIGHTS) for entry in second
    ]


def test_ties_are_broken_deterministically() -> None:
    """Identical issues in different repositories order by repo then number."""
    a = make_issue(5, repo_full_name="psf/requests")
    b = make_issue(5, repo_full_name="pydantic/pydantic")
    ranked = rank_issues(
        [a, b],
        make_config(repositories=["psf/requests", REPO]),
        {"psf/requests": make_repo(full_name="psf/requests"), REPO: make_repo()},
        now=NOW,
    )
    # "psf/requests" sorts before "pydantic/pydantic" lexicographically
    # ("psf" < "pydantic" because 's' < 'y').
    assert [entry.issue.repo_full_name for entry in ranked] == [
        "psf/requests",
        "pydantic/pydantic",
    ]


def test_scoring_does_not_mutate_the_issue() -> None:
    issue = make_issue()
    before = issue.model_dump()
    score_issue(issue, make_config(), make_repo(), now=NOW)
    assert issue.model_dump() == before


# --- individual factors -----------------------------------------------------


def test_skill_relevance_rewards_title_match() -> None:
    title_match = make_issue(1, title="python typing issue", body="")
    body_match = make_issue(2, title="something", body="python typing appears here")
    assert (
        score_skill_relevance(title_match, make_config()).score
        > score_skill_relevance(body_match, make_config()).score
    )


def test_skill_relevance_is_zero_without_any_match() -> None:
    result = score_skill_relevance(make_issue(title="zzz", body="zzz"), make_config())
    assert result.score == 0.0
    assert "no configured skill keyword" in result.reason


def test_skill_relevance_neutral_without_config() -> None:
    result = score_skill_relevance(make_issue(), None)
    assert result.score == 0.5
    assert "no skill keywords configured" in result.reason


def test_clarity_rewards_reproduction_steps() -> None:
    with_steps = make_issue(
        1, body="Steps to reproduce: run it. Expected x, actual y. Traceback: Error."
    )
    without = make_issue(2, body="it is broken")
    assert score_issue_clarity(with_steps).score > score_issue_clarity(without).score


def test_clarity_short_body_is_penalised() -> None:
    result = score_issue_clarity(make_issue(body="broken"))
    assert "very short" in result.reason


def test_feasible_scope_rewards_bounded_labels() -> None:
    bounded = make_issue(1, labels=["good first issue"], body="x" * 200)
    open_ended = make_issue(2, labels=["epic"], body="x" * 200)
    assert score_feasible_scope(bounded).score > score_feasible_scope(open_ended).score


def test_feasible_scope_is_neutral_without_signals() -> None:
    result = score_feasible_scope(make_issue(labels=[], body="x" * 200))
    assert result.score == pytest.approx(0.5)


def test_testability_rewards_bug_label_and_reproduction() -> None:
    result = score_testability(make_issue(labels=["bug"]))
    assert result.score > 0.0
    assert "bug" in result.reason


def test_testability_zero_without_signals() -> None:
    result = score_testability(Issue(repo_full_name=REPO, number=1, title="x", body="y"))
    assert result.score == 0.0
    assert "no testability signals" in result.reason


def test_repository_health_rewards_active_repo() -> None:
    healthy = score_repository_health(make_repo(), now=NOW)
    stale = score_repository_health(
        make_repo(stars=5, last_pushed_at=NOW - timedelta(days=900)), now=NOW
    )
    assert healthy.score > stale.score
    assert "stale" in stale.reason


def test_repository_health_neutral_without_metadata() -> None:
    result = score_repository_health(None, now=NOW)
    assert result.score == 0.5
    assert "no repository metadata" in result.reason


def test_duplicate_conflict_risk_is_safety_score() -> None:
    clean = make_issue(1)
    conflicting = make_issue(2, linked_open_prs=[10, 11])
    assert score_duplicate_conflict_risk(clean).score == 1.0
    assert score_duplicate_conflict_risk(conflicting).score < 1.0
    assert "linked to open pull request" in score_duplicate_conflict_risk(conflicting).reason


def test_duplicate_conflict_risk_penalises_conflict_labels() -> None:
    result = score_duplicate_conflict_risk(make_issue(labels=["duplicate"]))
    assert result.score < 1.0
    assert "conflict-prone" in result.reason


def test_duplicate_conflict_risk_penalises_long_threads() -> None:
    result = score_duplicate_conflict_risk(make_issue(comments_count=40))
    assert result.score < 1.0
    assert "40 comments" in result.reason


def test_factor_scores_are_always_within_range() -> None:
    extremes = [
        make_issue(1, body="", labels=[]),
        make_issue(2, body="x" * 10000, labels=["epic", "duplicate"], comments_count=99),
        make_issue(3, title="duplicate python typing", linked_open_prs=[1, 2, 3]),
    ]
    config = make_config()
    for issue in extremes:
        score = score_issue(issue, config, make_repo(), now=NOW)
        for factor in SCORE_FACTORS:
            value = getattr(score, factor)
            assert 0.0 <= value <= 1.0, f"{factor} out of range: {value}"


# --- explain ----------------------------------------------------------------


def test_explain_includes_every_factor_and_total() -> None:
    ranked = rank_issues([make_issue()], make_config(), {REPO: make_repo()}, now=NOW)
    text = explain(ranked[0], DEFAULT_WEIGHTS)
    assert "total" in text
    assert "/100" in text
    for factor in SCORE_FACTORS:
        assert factor in text
