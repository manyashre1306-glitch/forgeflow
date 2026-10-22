"""Unit tests for the models and the task state machine."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from forgeflow.models import (
    DEFAULT_WEIGHTS,
    LEGAL_TRANSITIONS,
    SCORE_FACTORS,
    TERMINAL_STATES,
    IllegalTransitionError,
    Issue,
    IssueScore,
    RankedIssue,
    Repository,
    TaskState,
    allowed_transitions,
    is_legal_transition,
    validate_transition,
)

ALL_STATES = list(TaskState)


def make_issue(**overrides) -> Issue:
    data = {
        "repo_full_name": "pydantic/pydantic",
        "number": 1234,
        "title": "Bug: something is wrong",
        "body": "Steps to reproduce...",
        "labels": ["bug"],
        "url": "https://github.com/pydantic/pydantic/issues/1234",
    }
    data.update(overrides)
    return Issue(**data)


def make_score(**overrides) -> IssueScore:
    data = {
        "repo_full_name": "pydantic/pydantic",
        "issue_number": 1234,
        "skill_relevance": 1.0,
        "issue_clarity": 0.5,
        "feasible_scope": 0.5,
        "testability": 0.5,
        "repository_health": 0.5,
        "duplicate_conflict_risk": 1.0,
        "reasons": {"skill_relevance": "matches python"},
    }
    data.update(overrides)
    return IssueScore(**data)


# --- state machine coverage -------------------------------------------------


def test_all_spec_states_exist() -> None:
    """Every state named in SPEC section 10 is defined."""
    expected = {
        "discovered",
        "shortlisted",
        "analyzed",
        "awaiting_plan_approval",
        "implementing",
        "validating",
        "failed_validation",
        "awaiting_publish_approval",
        "published",
        "merged",
        "closed",
        "discarded",
    }
    assert {state.value for state in TaskState} == expected


def test_every_state_has_a_transition_entry() -> None:
    """The transition map is total over the state enum."""
    assert set(LEGAL_TRANSITIONS) == set(TaskState)


def test_terminal_states_have_no_exits() -> None:
    for state in TERMINAL_STATES:
        assert allowed_transitions(state) == frozenset()


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (TaskState.DISCOVERED, TaskState.SHORTLISTED),
        (TaskState.SHORTLISTED, TaskState.ANALYZED),
        (TaskState.ANALYZED, TaskState.AWAITING_PLAN_APPROVAL),
        (TaskState.AWAITING_PLAN_APPROVAL, TaskState.IMPLEMENTING),
        (TaskState.IMPLEMENTING, TaskState.VALIDATING),
        (TaskState.IMPLEMENTING, TaskState.FAILED_VALIDATION),
        (TaskState.VALIDATING, TaskState.AWAITING_PUBLISH_APPROVAL),
        (TaskState.VALIDATING, TaskState.FAILED_VALIDATION),
        (TaskState.FAILED_VALIDATION, TaskState.IMPLEMENTING),
        (TaskState.AWAITING_PUBLISH_APPROVAL, TaskState.PUBLISHED),
        (TaskState.PUBLISHED, TaskState.MERGED),
        (TaskState.PUBLISHED, TaskState.CLOSED),
        (TaskState.DISCOVERED, TaskState.DISCARDED),
        (TaskState.SHORTLISTED, TaskState.DISCARDED),
    ],
)
def test_legal_transitions_are_accepted(current: TaskState, new: TaskState) -> None:
    assert is_legal_transition(current, new)
    validate_transition(current, new)  # must not raise


@pytest.mark.parametrize(
    ("current", "new"),
    [
        # skipping a stage
        (TaskState.DISCOVERED, TaskState.IMPLEMENTING),
        (TaskState.DISCOVERED, TaskState.PUBLISHED),
        (TaskState.SHORTLISTED, TaskState.PUBLISHED),
        (TaskState.ANALYZED, TaskState.IMPLEMENTING),
        # going backwards
        (TaskState.ANALYZED, TaskState.DISCOVERED),
        (TaskState.SHORTLISTED, TaskState.DISCOVERED),
        (TaskState.IMPLEMENTING, TaskState.ANALYZED),
        # out of a terminal state
        (TaskState.MERGED, TaskState.PUBLISHED),
        (TaskState.CLOSED, TaskState.PUBLISHED),
        (TaskState.DISCARDED, TaskState.DISCOVERED),
        # validating cannot jump straight to published
        (TaskState.VALIDATING, TaskState.PUBLISHED),
        # implementing cannot publish without validation
        (TaskState.IMPLEMENTING, TaskState.AWAITING_PUBLISH_APPROVAL),
    ],
)
def test_illegal_transitions_are_rejected(current: TaskState, new: TaskState) -> None:
    assert not is_legal_transition(current, new)
    with pytest.raises(IllegalTransitionError) as excinfo:
        validate_transition(current, new)
    message = str(excinfo.value)
    assert current.value in message
    assert new.value in message


@pytest.mark.parametrize("state", ALL_STATES)
def test_transition_to_same_state_is_rejected(state: TaskState) -> None:
    with pytest.raises(IllegalTransitionError, match="already in state"):
        validate_transition(state, state)


def test_illegal_transition_error_is_a_value_error() -> None:
    """Callers can catch this as a ValueError."""
    assert issubclass(IllegalTransitionError, ValueError)


def test_illegal_transition_message_lists_allowed_states() -> None:
    with pytest.raises(IllegalTransitionError) as excinfo:
        validate_transition(TaskState.DISCOVERED, TaskState.PUBLISHED)
    assert "allowed from 'discovered'" in str(excinfo.value)


# --- model validation -------------------------------------------------------


def test_repository_rejects_bad_full_name() -> None:
    with pytest.raises(ValidationError):
        Repository(full_name="not-a-repo")


def test_repository_accepts_owner_name() -> None:
    repo = Repository(full_name="psf/requests", stars=50000)
    assert repo.full_name == "psf/requests"
    assert repo.stars == 50000


def test_issue_defaults_to_discovered() -> None:
    assert make_issue().task_state is TaskState.DISCOVERED


def test_issue_rejects_unknown_github_state() -> None:
    with pytest.raises(ValidationError):
        make_issue(state="merged")


def test_issue_rejects_zero_number() -> None:
    with pytest.raises(ValidationError):
        make_issue(number=0)


def test_issue_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        make_issue(unexpected_field=True)


def test_default_weights_sum_to_one() -> None:
    assert pytest.approx(sum(DEFAULT_WEIGHTS.values()), abs=1e-9) == 1.0


def test_score_factors_match_default_weights() -> None:
    assert set(SCORE_FACTORS) == set(DEFAULT_WEIGHTS)


def test_score_rejects_out_of_range_value() -> None:
    with pytest.raises(ValidationError):
        make_score(skill_relevance=1.5)
    with pytest.raises(ValidationError):
        make_score(skill_relevance=-0.1)


def test_compute_total_is_weighted_sum_on_100_scale() -> None:
    score = make_score(
        skill_relevance=1.0,
        issue_clarity=1.0,
        feasible_scope=1.0,
        testability=1.0,
        repository_health=1.0,
        duplicate_conflict_risk=1.0,
    )
    assert score.compute_total() == pytest.approx(100.0)


def test_compute_total_zero_when_all_factors_zero() -> None:
    score = make_score(
        skill_relevance=0.0,
        issue_clarity=0.0,
        feasible_scope=0.0,
        testability=0.0,
        repository_health=0.0,
        duplicate_conflict_risk=0.0,
    )
    assert score.compute_total() == pytest.approx(0.0)


def test_compute_total_rejects_incomplete_weights() -> None:
    score = make_score()
    with pytest.raises(ValueError, match="missing factors"):
        score.compute_total({"skill_relevance": 1.0})


def test_ranked_issue_requires_matching_score() -> None:
    issue = make_issue()
    mismatched = make_score(issue_number=9999)
    with pytest.raises(ValidationError):
        RankedIssue(issue=issue, score=mismatched, rank=1)


def test_ranked_issue_accepts_matching_score() -> None:
    ranked = RankedIssue(issue=make_issue(), score=make_score(), rank=1)
    assert ranked.rank == 1
    assert ranked.issue.number == 1234
