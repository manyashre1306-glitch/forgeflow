"""Issue ranking: the six weighted factors from SPEC section 5.

Everything here is pure: given the same inputs, the same output is produced.
There is no network, no database, and no LLM. Model-backed analysis is a later
phase; the spec requires deterministic filters before any LLM call.

The six factors are scored 0.0 to 1.0, each with a human-readable reason, and
combined into a weighted total on a 0 to 100 scale.

Direction convention
-------------------
The spec names the sixth factor "duplicate/conflict risk". Because the total is
a weighted *sum*, every factor must point the same way, so that factor is stored
as a *safety* score: 1.0 means no duplicate or conflicting work was detected,
0.0 means a strong conflict signal. The field keeps its schema name
(``duplicate_conflict_risk``) for compatibility with the database, and the reason
string always states the direction in words.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from forgeflow.config import AppConfig
from forgeflow.models import (
    DEFAULT_WEIGHTS,
    SCORE_FACTORS,
    Issue,
    IssueScore,
    RankedIssue,
    Repository,
)

# Text signals used by more than one factor.
_REPRODUCTION_PATTERNS = (
    "steps to reproduce",
    "how to reproduce",
    "reproduction",
    "reproduce",
    "minimal example",
    "minimal repro",
    "to reproduce",
    "mcve",
)
_EXPECTATION_PATTERNS = (
    "expected",
    "actual",
    "should be",
    "instead of",
    "but got",
    "observed",
)
_ERROR_PATTERNS = (
    "traceback",
    "exception",
    "error",
    "stack trace",
    "assertionerror",
    "raises",
)
_TEST_PATTERNS = (
    "test_",
    "tests/",
    "pytest",
    "unit test",
    "regression test",
    "add a test",
    "test case",
)
_CODE_BLOCK = re.compile(r"```|\n {4}\S|\n\t\S")

# Labels that suggest a task is too open-ended to scope safely.
_OPEN_ENDED_LABELS = frozenset(
    {"epic", "design", "discussion", "rfc", "proposal", "question", "meta"}
)
# Labels that suggest a bounded, welcome change.
_SCOPE_FRIENDLY_LABELS = frozenset({"good first issue", "help wanted", "easy", "starter"})
# Labels that suggest the issue is contested or already resolved elsewhere.
_CONFLICT_LABELS = frozenset({"duplicate", "wontfix", "invalid", "stale"})


@dataclass(frozen=True)
class FactorResult:
    """One factor's score and the reason for it."""

    name: str
    score: float
    reason: str


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _haystack(issue: Issue) -> str:
    return f"{issue.title}\n{issue.body}".lower()


def _has_any(text: str, patterns: Iterable[str]) -> bool:
    return any(pattern in text for pattern in patterns)


def _labels_lower(issue: Issue) -> set[str]:
    return {label.strip().lower() for label in issue.labels}


# --- the six factors --------------------------------------------------------


def score_skill_relevance(issue: Issue, config: AppConfig | None = None) -> FactorResult:
    """How closely the issue matches the configured skill keywords.

    A match in the title counts most, then labels, then the body, because a
    keyword in the title is a stronger signal of the issue's actual topic.
    """
    keywords: dict[str, list[str]] = {}
    if config is not None:
        keywords = config.skill_keywords
    if not keywords:
        return FactorResult(
            "skill_relevance",
            0.5,
            "no skill keywords configured; neutral score of 0.5",
        )

    title = issue.title.lower()
    body = issue.body.lower()
    labels = _labels_lower(issue)

    weighted_hits = 0.0
    matched: list[str] = []
    matched_categories: set[str] = set()
    for category, words in keywords.items():
        for word in words:
            needle = word.lower()
            if needle in title:
                weighted_hits += 2.0
            elif needle in labels:
                weighted_hits += 1.5
            elif needle in body:
                weighted_hits += 0.5
            else:
                continue
            matched.append(needle)
            matched_categories.add(category)

    if not matched:
        return FactorResult(
            "skill_relevance",
            0.0,
            "no configured skill keyword found in title, body, or labels",
        )

    score = _clamp(weighted_hits / 3.0)
    unique = sorted(set(matched))
    reason = (
        f"matched {len(unique)} skill keyword(s) across "
        f"{len(matched_categories)} categor(y/ies) "
        f"({', '.join(sorted(matched_categories))}): {', '.join(unique)}"
    )
    return FactorResult("skill_relevance", score, reason)


def score_issue_clarity(issue: Issue) -> FactorResult:
    """How clearly the issue states the problem.

    Signals: a body long enough to explain the problem, reproduction steps, an
    expected-versus-actual statement, and concrete error output.
    """
    text = _haystack(issue)
    body_length = len(issue.body.strip())
    signals: list[str] = []
    score = 0.0

    if body_length >= 200:
        score += 0.30
        signals.append("body length >= 200 chars")
    elif body_length >= 80:
        score += 0.15
        signals.append("body length >= 80 chars")
    else:
        signals.append("body is very short")

    if _has_any(text, _REPRODUCTION_PATTERNS):
        score += 0.30
        signals.append("states reproduction steps")
    if _has_any(text, _EXPECTATION_PATTERNS):
        score += 0.20
        signals.append("contrasts expected and actual behaviour")
    if _has_any(text, _ERROR_PATTERNS):
        score += 0.20
        signals.append("includes error output")

    score = _clamp(score)
    return FactorResult("issue_clarity", score, "; ".join(signals))


def score_feasible_scope(issue: Issue) -> FactorResult:
    """How bounded the change is likely to be.

    Starts neutral, then rewards labels that mark a bounded task and penalises
    labels and body sizes that suggest an open-ended or very large change.
    """
    labels = _labels_lower(issue)
    body_length = len(issue.body.strip())
    score = 0.5
    notes: list[str] = ["starts from a neutral 0.5"]

    friendly = sorted(labels & _SCOPE_FRIENDLY_LABELS)
    if friendly:
        score += 0.3
        notes.append(f"bounded-task label(s): {', '.join(friendly)}")

    open_ended = sorted(labels & _OPEN_ENDED_LABELS)
    if open_ended:
        score -= 0.3
        notes.append(f"open-ended label(s): {', '.join(open_ended)}")

    if body_length > 4000:
        score -= 0.2
        notes.append("very long body (>4000 chars) may indicate a large change")
    elif body_length < 80:
        score -= 0.1
        notes.append("very short body may be under-specified")

    score = _clamp(score)
    return FactorResult("feasible_scope", score, "; ".join(notes))


def score_testability(issue: Issue) -> FactorResult:
    """How easy it would be to write a test that demonstrates the fix."""
    text = _haystack(issue)
    labels = _labels_lower(issue)
    score = 0.0
    signals: list[str] = []

    if _has_any(text, _REPRODUCTION_PATTERNS):
        score += 0.35
        signals.append("has a reproduction that can become a test")
    if "bug" in labels:
        score += 0.20
        signals.append("labelled 'bug'")
    if _has_any(text, _TEST_PATTERNS):
        score += 0.25
        signals.append("mentions tests")
    if _CODE_BLOCK.search(issue.body):
        score += 0.20
        signals.append("includes a code block")

    if not signals:
        signals.append("no testability signals found")

    score = _clamp(score)
    return FactorResult("testability", score, "; ".join(signals))


def score_repository_health(
    repo: Repository | None,
    *,
    now: datetime | None = None,
    stale_days: int = 365,
) -> FactorResult:
    """Health of the target repository: activity, popularity, and guidance.

    Returns a neutral 0.5 when no metadata is available, rather than guessing.
    """
    if repo is None:
        return FactorResult(
            "repository_health",
            0.5,
            "no repository metadata available; neutral score of 0.5",
        )

    score = 0.0
    notes: list[str] = []

    if repo.stars >= 10000:
        score += 0.4
        notes.append(f"{repo.stars} stars")
    elif repo.stars >= 1000:
        score += 0.3
        notes.append(f"{repo.stars} stars")
    elif repo.stars >= 100:
        score += 0.2
        notes.append(f"{repo.stars} stars")
    else:
        score += 0.1
        notes.append(f"{repo.stars} stars")

    if repo.last_pushed_at is None:
        notes.append("last push date unknown")
    else:
        reference = now or datetime.now(UTC)
        pushed = repo.last_pushed_at
        if pushed.tzinfo is None:
            pushed = pushed.replace(tzinfo=UTC)
        age_days = (reference - pushed).days
        if age_days <= 30:
            score += 0.3
            notes.append(f"last pushed {age_days} day(s) ago")
        elif age_days <= 90:
            score += 0.2
            notes.append(f"last pushed {age_days} day(s) ago")
        elif age_days <= stale_days:
            score += 0.1
            notes.append(f"last pushed {age_days} day(s) ago")
        else:
            notes.append(f"stale: last pushed {age_days} day(s) ago")

    if repo.has_contributing:
        score += 0.15
        notes.append("has CONTRIBUTING guidance")
    if repo.has_tests:
        score += 0.15
        notes.append("has a test suite")

    score = _clamp(score)
    return FactorResult("repository_health", score, "; ".join(notes))


def score_duplicate_conflict_risk(issue: Issue) -> FactorResult:
    """Safety score for duplicate or competing work. 1.0 means no signal found.

    See the module docstring for why this factor is stored as a safety score
    rather than a raw risk value.
    """
    labels = _labels_lower(issue)
    score = 1.0
    notes: list[str] = []

    if issue.linked_open_prs:
        numbers = ", ".join(f"#{number}" for number in issue.linked_open_prs)
        score -= 0.6
        notes.append(f"linked to open pull request(s): {numbers}")

    conflict = sorted(labels & _CONFLICT_LABELS)
    if conflict:
        score -= 0.5
        notes.append(f"carries conflict-prone label(s): {', '.join(conflict)}")

    if "duplicate" in issue.title.lower():
        score -= 0.2
        notes.append("title mentions 'duplicate'")

    if issue.comments_count > 15:
        score -= 0.2
        notes.append(f"{issue.comments_count} comments suggests an active or contested thread")

    score = _clamp(score)
    if not notes:
        notes.append("no competing pull requests or duplicate markers detected")
    return FactorResult("duplicate_conflict_risk", score, "; ".join(notes))


# --- combining factors ------------------------------------------------------


def score_issue(
    issue: Issue,
    config: AppConfig | None = None,
    repo: Repository | None = None,
    *,
    now: datetime | None = None,
) -> IssueScore:
    """Compute all six factors for one issue and return a validated ``IssueScore``."""
    results = [
        score_skill_relevance(issue, config),
        score_issue_clarity(issue),
        score_feasible_scope(issue),
        score_testability(issue),
        score_repository_health(repo, now=now),
        score_duplicate_conflict_risk(issue),
    ]
    values = {result.name: result.score for result in results}
    reasons = {result.name: result.reason for result in results}
    return IssueScore(
        repo_full_name=issue.repo_full_name,
        issue_number=issue.number,
        reasons=reasons,
        **values,
    )


def resolve_weights(config: AppConfig | None = None) -> dict[str, float]:
    """Return the weights to use, falling back to the spec defaults."""
    if config is None:
        return dict(DEFAULT_WEIGHTS)
    return dict(config.weights)


def total_for(score: IssueScore, weights: Mapping[str, float]) -> float:
    """Weighted total on a 0 to 100 scale."""
    missing = set(SCORE_FACTORS) - set(weights)
    if missing:
        raise ValueError(f"weights missing factors: {sorted(missing)}")
    return score.compute_total(dict(weights))


def rank_issues(
    issues: Iterable[Issue],
    config: AppConfig | None = None,
    repositories: Mapping[str, Repository] | None = None,
    *,
    now: datetime | None = None,
) -> list[RankedIssue]:
    """Score and rank issues, highest total first.

    Issues that are not open are never ranked. Ties are broken by repository
    name and then issue number, so the output is deterministic.
    """
    repos = repositories or {}
    weights = resolve_weights(config)
    scored: list[tuple[float, Issue, IssueScore]] = []

    for issue in issues:
        if issue.state != "open":
            continue
        score = score_issue(issue, config, repos.get(issue.repo_full_name), now=now)
        scored.append((total_for(score, weights), issue, score))

    scored.sort(key=lambda item: (-item[0], item[1].repo_full_name, item[1].number))
    return [
        RankedIssue(issue=issue, score=score, rank=index)
        for index, (_total, issue, score) in enumerate(scored, start=1)
    ]


def explain(ranked: RankedIssue, weights: Mapping[str, float]) -> str:
    """Render one ranked issue as a multi-line explanation for reports."""
    lines = [
        f"#{ranked.rank} {ranked.issue.repo_full_name}#{ranked.issue.number} "
        f"- total {total_for(ranked.score, weights):.2f}/100",
        f"    {ranked.issue.title}",
    ]
    for factor in SCORE_FACTORS:
        value = getattr(ranked.score, factor)
        weight = weights.get(factor, 0.0)
        reason = ranked.score.reasons.get(factor, "(no reason recorded)")
        lines.append(f"    {factor} (weight {weight:.2f}): {value:.2f} - {reason}")
    return "\n".join(lines)
