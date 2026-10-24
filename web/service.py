"""The service layer the pages call.

Each function here is pure-ish: it takes a client, a config, and validated
inputs, and returns a plain dataclass the page renders as text. No Streamlit is
imported here, so every function is testable without a running server, and the
pages stay thin.

Discovery, dedupe, and ranking are the real ForgeFlow functions; this module only
sequences them for a single repository and attaches the per-factor reasons the
page shows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from forgeflow import discovery, ranking
from forgeflow.config import AppConfig, LLMConfig, ModelPricing
from forgeflow.dedupe import FilterResult, deduplicate, filter_issues
from forgeflow.github_client import GitHubReader
from forgeflow.models import Issue, RankedIssue
from web.context import ContextBudget, build_hosted_context
from web.limits import SessionLimits, validate_full_name

#: Factors shown for every ranked issue, in the spec's order, with their
#: configured weights resolved at render time.
FACTOR_LABELS: tuple[tuple[str, str], ...] = (
    ("skill_relevance", "Skill relevance"),
    ("issue_clarity", "Issue clarity"),
    ("feasible_scope", "Feasible scope"),
    ("testability", "Testability"),
    ("repository_health", "Repository health"),
    ("duplicate_conflict_risk", "Duplicate/conflict risk"),
)

HEURISTIC_CAVEAT = (
    "These scores are initial heuristics (SPEC section 5), not proven predictors "
    "of whether a maintainer will accept a change. Use them to shortlist, then "
    "read the issue and the repository's contribution guide before deciding."
)


@dataclass
class FactorRow:
    """One ranking factor for display: its weight, value, and reason."""

    key: str
    label: str
    weight: float
    value: float
    points: float
    reason: str


@dataclass
class RankedRow:
    """One ranked issue, flattened for display."""

    rank: int
    repo_full_name: str
    number: int
    title: str
    url: str
    labels: list[str]
    total: float
    outcome_adjustment: float
    factors: list[FactorRow]


@dataclass
class FinderResult:
    """Everything the Issue Finder page shows."""

    repo_full_name: str
    repository_stars: int
    has_contributing: bool
    has_tests: bool
    issues_found: int
    ranked: list[RankedRow] = field(default_factory=list)
    dropped: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    heuristic_caveat: str = HEURISTIC_CAVEAT


def find_and_rank(
    client: GitHubReader,
    config: AppConfig,
    repo_full_name: str,
    *,
    limits: SessionLimits | None = None,
    per_repo_limit: int = 50,
    now: datetime | None = None,
) -> FinderResult:
    """Discover, dedupe, filter, and rank one repository's open issues.

    The repository name is validated first, so a crafted value cannot become a
    request to another host. ``limits`` counts the API requests when supplied.
    """
    full_name = validate_full_name(repo_full_name)
    tracker = limits if limits is not None else SessionLimits()

    result = FinderResult(
        repo_full_name=full_name,
        repository_stars=0,
        has_contributing=False,
        has_tests=False,
        issues_found=0,
    )

    try:
        repo = discovery.fetch_repository(client, full_name)
    except Exception as exc:
        result.errors.append(f"could not read {full_name}: {type(exc).__name__}")
        return result

    result.repository_stars = repo.stars
    result.has_contributing = repo.has_contributing
    result.has_tests = repo.has_tests

    try:
        payloads = client.search_issues(
            discovery.build_search_query(full_name), limit=per_repo_limit
        )
    except Exception as exc:
        result.errors.append(f"issue search failed: {type(exc).__name__}")
        return result
    tracker.record_github_request(len(payloads) or 1)

    issues: list[Issue] = []
    for payload in payloads:
        try:
            issues.append(discovery.issue_from_api(payload, full_name))
        except Exception:
            continue

    unique, duplicates = deduplicate(issues)
    filtered: FilterResult = filter_issues(
        unique,
        exclude_labels=config.exclude_labels,
        opt_out_labels=config.opt_out_labels,
    )
    result.issues_found = len(filtered.kept)
    result.dropped = [
        (f"{item.issue.repo_full_name}#{item.issue.number}", f"{item.reason.value}: {item.detail}")
        for item in (*duplicates, *filtered.dropped)
    ]

    repositories = {repo.full_name: repo}
    ranked = ranking.rank_issues(filtered.kept, config, repositories, now=now)
    weights = ranking.resolve_weights(config)
    result.ranked = [_to_row(item, weights) for item in ranked]
    return result


def _to_row(ranked: RankedIssue, weights: dict[str, float]) -> RankedRow:
    score = ranked.score
    factors: list[FactorRow] = []
    for key, label in FACTOR_LABELS:
        value = float(getattr(score, key))
        weight = float(weights.get(key, 0.0))
        factors.append(
            FactorRow(
                key=key,
                label=label,
                weight=weight,
                value=value,
                points=round(value * weight * 100, 2),
                reason=score.reasons.get(key, "no reason recorded"),
            )
        )
    return RankedRow(
        rank=ranked.rank,
        repo_full_name=ranked.issue.repo_full_name,
        number=ranked.issue.number,
        title=ranked.issue.title,
        url=ranked.issue.url,
        labels=list(ranked.issue.labels),
        total=round(ranking.total_for(score, weights) + ranked.outcome_adjustment, 2),
        outcome_adjustment=round(ranked.outcome_adjustment, 2),
        factors=factors,
    )


# --------------------------------------------------------------------------
# Repo rules checker
# --------------------------------------------------------------------------


@dataclass
class RulesResult:
    """What the Repo Rules Checker shows for one repository."""

    repo_full_name: str
    verdict: str
    verdict_explanation: str
    evidence: list[str]
    requires_tests: bool
    requires_signoff: bool
    requires_cla: bool
    requires_ai_disclosure: bool
    sources: list[str]
    documents_read: list[str]
    unknown_is_not_permission: str = (
        'A verdict of "unknown" is not permission. ForgeFlow treats an absent '
        "statement as no statement, and blocks publishing unless a human records "
        "a documented override."
    )


def check_repo_rules(
    client: GitHubReader,
    repo_full_name: str,
    *,
    limits: SessionLimits | None = None,
) -> RulesResult:
    """Read CONTRIBUTING and README through the read-only API and detect rules.

    Uses the real :func:`forgeflow.repository_context.detect_contribution_rules`.
    No clone, and every file is size-limited.
    """
    full_name = validate_full_name(repo_full_name)
    tracker = limits if limits is not None else SessionLimits()

    from forgeflow.models import UntrustedText
    from forgeflow.repository_context import (
        CONTRIBUTING_CANDIDATES,
        MAX_FILE_BYTES,
        README_CANDIDATES,
        detect_contribution_rules,
    )

    documents_read: list[str] = []
    contributing = []

    for name in CONTRIBUTING_CANDIDATES:
        text = client.get_file_content(full_name, name, max_bytes=MAX_FILE_BYTES)
        tracker.record_github_request()
        if text is not None:
            contributing.append(UntrustedText(text=text, source=name))
            documents_read.append(name)

    readme = None
    for name in README_CANDIDATES:
        text = client.get_file_content(full_name, name, max_bytes=MAX_FILE_BYTES)
        tracker.record_github_request()
        if text is not None:
            readme = UntrustedText(text=text, source=name)
            documents_read.append(name)
            break

    rules = detect_contribution_rules(contributing, readme)
    verdict, explanation = _verdict_text(rules)
    return RulesResult(
        repo_full_name=full_name,
        verdict=verdict,
        verdict_explanation=explanation,
        evidence=list(rules.ai_policy_evidence),
        requires_tests=rules.requires_tests,
        requires_signoff=rules.requires_signoff,
        requires_cla=rules.requires_cla,
        requires_ai_disclosure=rules.requires_ai_disclosure,
        sources=list(rules.sources),
        documents_read=documents_read,
    )


def _verdict_text(rules) -> tuple[str, str]:
    from forgeflow.models import AIPolicy

    if rules.ai_policy is AIPolicy.PROHIBITED:
        return (
            "restricted (AI contributions prohibited)",
            "The contribution guidance appears to prohibit AI-generated "
            "contributions. ForgeFlow will refuse to publish unless a human "
            "records a documented override bound to the exact patch.",
        )
    if rules.ai_policy is AIPolicy.RESTRICTED:
        return (
            "restricted",
            "The contribution guidance restricts AI use. Publishing is blocked "
            "unless a human records a documented override.",
        )
    if rules.requires_ai_disclosure:
        return (
            "disclosure required",
            "The contribution guidance requires disclosure of AI assistance. "
            "Publishing is blocked until that disclosure is handled and an "
            "override is recorded.",
        )
    if rules.ai_policy is AIPolicy.ALLOWED:
        return ("allowed", "The contribution guidance appears to permit AI use.")
    return (
        "unknown",
        "No AI-use statement was found. Unknown is not permission.",
    )


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------


@dataclass
class AnalysisView:
    """The analysis result flattened for display, plus token and cost totals."""

    problem_explanation: str
    minimal_fix_proposal: str
    risks: list[str]
    test_plan: list[str]
    files_likely_involved: list[str]
    unverified_assumptions: list[str]
    confidence: float
    triage_summary: str
    injection_flagged: bool
    model_calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    cost_breakdown: list[tuple[str, float]]


def analyse_issue(
    client: GitHubReader,
    analyzer,
    issue: Issue,
    *,
    limits: SessionLimits,
    max_files: int = 20,
) -> tuple[AnalysisView, ContextBudget]:
    """Build a hosted context and run the real triage + analysis.

    ``analyzer`` is an :class:`~forgeflow.issue_analyzer.IssueAnalyzer` built with
    the visitor's key and the session cap. The context comes from the read-only
    API, so no clone happens.
    """
    budget = ContextBudget()
    context = build_hosted_context(client, issue, budget=budget, max_files=max_files)
    limits.record_github_request(budget.total)

    analysis = analyzer.analyze(context)
    triage = analysis.triage
    view = AnalysisView(
        problem_explanation=analysis.problem_explanation,
        minimal_fix_proposal=analysis.minimal_fix_proposal,
        risks=list(analysis.risks),
        test_plan=list(analysis.test_plan),
        files_likely_involved=list(analysis.files_likely_involved),
        unverified_assumptions=list(analysis.unverified_assumptions),
        confidence=analysis.confidence,
        triage_summary=triage.problem_summary if triage else "(triage skipped)",
        injection_flagged=bool(triage.injection_flagged) if triage else False,
        model_calls=limits.model_calls,
        prompt_tokens=limits.prompt_tokens,
        completion_tokens=limits.completion_tokens,
        cost_usd=limits.cost_usd,
        cost_breakdown=list(limits.cost_breakdown),
    )
    return view, budget


def issue_from_inputs(repo_full_name: str, issue_number: int, *, title: str = "", body: str = ""):
    """Build a minimal :class:`Issue` from page inputs, validating the repo name."""
    from web.limits import cap_text, validate_issue_number

    full_name = validate_full_name(repo_full_name)
    number = validate_issue_number(issue_number)
    capped_title, _ = cap_text(title)
    capped_body, _ = cap_text(body)
    return Issue(
        repo_full_name=full_name,
        number=number,
        title=capped_title or f"Issue #{number}",
        body=capped_body,
        state="open",
    )


def default_llm_config() -> LLMConfig:
    """A minimal LLM config for tests and offline pages.

    Real pages load ``config/repositories.yaml``; this exists so a unit test can
    exercise the service layer without a config file on disk.
    """
    return LLMConfig(
        triage_model="gemini-3.5-flash-lite",
        analysis_model="gemini-3.6-flash",
        pricing={
            "gemini-3.5-flash-lite": ModelPricing(input_per_million=0.30, output_per_million=2.50),
            "gemini-3.6-flash": ModelPricing(input_per_million=0.75, output_per_million=3.75),
        },
        monthly_cap_usd=0.25,
    )


def now_utc() -> datetime:
    return datetime.now(UTC)
