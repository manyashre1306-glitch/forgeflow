"""Validated data models for ForgeFlow.

Phase 1 uses these models for issue discovery, ranking, and persistence. The
task state machine from SPEC section 10 lives here so the models and the
storage layer enforce exactly the same rules.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TaskState(StrEnum):
    """Explicit task states required by SPEC section 10."""

    DISCOVERED = "discovered"
    SHORTLISTED = "shortlisted"
    ANALYZED = "analyzed"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    IMPLEMENTING = "implementing"
    VALIDATING = "validating"
    FAILED_VALIDATION = "failed_validation"
    AWAITING_PUBLISH_APPROVAL = "awaiting_publish_approval"
    PUBLISHED = "published"
    MERGED = "merged"
    CLOSED = "closed"
    DISCARDED = "discarded"


TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.MERGED, TaskState.CLOSED, TaskState.DISCARDED}
)

# Legal transitions for the Phase 1 pipeline. Every state that is not terminal
# can be discarded or closed, because a human may abandon a task at any point.
# Repair attempts flow failed_validation -> implementing, matching the spec's
# "limit automated repair attempts to two".
LEGAL_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.DISCOVERED: frozenset({TaskState.SHORTLISTED, TaskState.DISCARDED, TaskState.CLOSED}),
    TaskState.SHORTLISTED: frozenset({TaskState.ANALYZED, TaskState.DISCARDED, TaskState.CLOSED}),
    TaskState.ANALYZED: frozenset(
        {TaskState.AWAITING_PLAN_APPROVAL, TaskState.DISCARDED, TaskState.CLOSED}
    ),
    TaskState.AWAITING_PLAN_APPROVAL: frozenset(
        {TaskState.IMPLEMENTING, TaskState.DISCARDED, TaskState.CLOSED}
    ),
    TaskState.IMPLEMENTING: frozenset(
        {TaskState.VALIDATING, TaskState.FAILED_VALIDATION, TaskState.DISCARDED}
    ),
    TaskState.VALIDATING: frozenset(
        {TaskState.AWAITING_PUBLISH_APPROVAL, TaskState.FAILED_VALIDATION}
    ),
    TaskState.FAILED_VALIDATION: frozenset({TaskState.IMPLEMENTING, TaskState.DISCARDED}),
    TaskState.AWAITING_PUBLISH_APPROVAL: frozenset({TaskState.PUBLISHED, TaskState.DISCARDED}),
    TaskState.PUBLISHED: frozenset({TaskState.MERGED, TaskState.CLOSED}),
    TaskState.MERGED: frozenset(),
    TaskState.CLOSED: frozenset(),
    TaskState.DISCARDED: frozenset(),
}


class IllegalTransitionError(ValueError):
    """Raised when a task state change is not permitted."""


def allowed_transitions(current: TaskState) -> frozenset[TaskState]:
    """Return the states reachable from ``current``."""
    return LEGAL_TRANSITIONS[current]


def is_legal_transition(current: TaskState, new: TaskState) -> bool:
    """Return True only if ``current -> new`` is an allowed change."""
    return new in LEGAL_TRANSITIONS[current]


def validate_transition(current: TaskState, new: TaskState) -> None:
    """Raise ``IllegalTransitionError`` unless ``current -> new`` is allowed.

    A transition to the same state is rejected: it is not a transition, and
    silently accepting it would hide a bug in the caller.
    """
    if current == new:
        raise IllegalTransitionError(
            f"illegal transition: task is already in state '{current.value}'"
        )
    if not is_legal_transition(current, new):
        allowed = ", ".join(sorted(s.value for s in LEGAL_TRANSITIONS[current])) or "none"
        raise IllegalTransitionError(
            f"illegal transition: '{current.value}' -> '{new.value}'; "
            f"allowed from '{current.value}': {allowed}"
        )


def _validate_repo_full_name(value: str) -> str:
    parts = value.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"full_name must look like 'owner/name', got {value!r}")
    return value


class Repository(BaseModel):
    """A GitHub repository on the allowlist."""

    model_config = ConfigDict(extra="forbid")

    full_name: str
    stars: int = Field(default=0, ge=0)
    open_issues: int = Field(default=0, ge=0)
    has_contributing: bool = False
    has_tests: bool = False
    last_pushed_at: datetime | None = None
    fetched_at: datetime | None = None

    @field_validator("full_name")
    @classmethod
    def _check_full_name(cls, value: str) -> str:
        return _validate_repo_full_name(value)


class Issue(BaseModel):
    """A GitHub issue plus ForgeFlow's pipeline state for it.

    ``state`` is the GitHub issue state (open or closed). ``task_state`` is
    ForgeFlow's own pipeline state. They are deliberately separate fields.
    """

    model_config = ConfigDict(extra="forbid")

    repo_full_name: str
    number: int = Field(ge=1)
    title: str = Field(min_length=1)
    body: str = ""
    state: str = "open"
    labels: list[str] = Field(default_factory=list)
    url: str = ""
    comments_count: int = Field(default=0, ge=0)
    assignees: list[str] = Field(default_factory=list)
    is_pull_request: bool = False
    linked_open_prs: list[int] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    task_state: TaskState = TaskState.DISCOVERED

    @field_validator("repo_full_name")
    @classmethod
    def _check_full_name(cls, value: str) -> str:
        return _validate_repo_full_name(value)

    @field_validator("state")
    @classmethod
    def _check_state(cls, value: str) -> str:
        if value not in {"open", "closed"}:
            raise ValueError(f"state must be 'open' or 'closed', got {value!r}")
        return value


# Default weights from SPEC section 5. They must sum to 1.0.
DEFAULT_WEIGHTS: dict[str, float] = {
    "skill_relevance": 0.25,
    "issue_clarity": 0.20,
    "feasible_scope": 0.20,
    "testability": 0.15,
    "repository_health": 0.10,
    "duplicate_conflict_risk": 0.10,
}

SCORE_FACTORS: tuple[str, ...] = tuple(DEFAULT_WEIGHTS)


class IssueScore(BaseModel):
    """Per-factor scores for an issue, each in 0.0 to 1.0, plus reasons.

    This model only holds and combines the numbers. The heuristics that produce
    the factor values belong to ``ranking.py`` and are not implemented yet.
    """

    model_config = ConfigDict(extra="forbid")

    repo_full_name: str
    issue_number: int = Field(ge=1)
    skill_relevance: float = Field(ge=0.0, le=1.0)
    issue_clarity: float = Field(ge=0.0, le=1.0)
    feasible_scope: float = Field(ge=0.0, le=1.0)
    testability: float = Field(ge=0.0, le=1.0)
    repository_health: float = Field(ge=0.0, le=1.0)
    duplicate_conflict_risk: float = Field(ge=0.0, le=1.0)
    reasons: dict[str, str] = Field(default_factory=dict)

    @field_validator("repo_full_name")
    @classmethod
    def _check_full_name(cls, value: str) -> str:
        return _validate_repo_full_name(value)

    def factor_values(self) -> dict[str, float]:
        """Return the six factor sub-scores keyed by factor name."""
        return {factor: float(getattr(self, factor)) for factor in SCORE_FACTORS}

    def compute_total(self, weights: dict[str, float] | None = None) -> float:
        """Return the weighted total on a 0 to 100 scale."""
        weights = DEFAULT_WEIGHTS if weights is None else weights
        missing = set(SCORE_FACTORS) - set(weights)
        if missing:
            raise ValueError(f"weights missing factors: {sorted(missing)}")
        total = sum(weights[factor] * value for factor, value in self.factor_values().items())
        return round(total * 100.0, 4)


class RankedIssue(BaseModel):
    """An issue together with its score and position in the ranked list."""

    model_config = ConfigDict(extra="forbid")

    issue: Issue
    score: IssueScore
    rank: int = Field(ge=1)

    @model_validator(mode="after")
    def _check_score_matches_issue(self) -> RankedIssue:
        if self.score.repo_full_name != self.issue.repo_full_name:
            raise ValueError("score repo_full_name does not match issue")
        if self.score.issue_number != self.issue.number:
            raise ValueError("score issue_number does not match issue number")
        return self


# --------------------------------------------------------------------------
# Phase 2: untrusted text handling
# --------------------------------------------------------------------------


class TrustLevel(StrEnum):
    """Provenance of a piece of text.

    Only one level exists today: repository and issue text is always untrusted.
    The enum is explicit so that a prompt can be built by *trust level* rather
    than by accident, and so that adding a trusted level later is a visible
    change rather than a silent default.
    """

    UNTRUSTED = "untrusted"


class UntrustedText(BaseModel):
    """A block of text that must never be interpreted as instructions.

    SPEC section 16 and the Phase 2 threat model require that repository and
    issue content is treated strictly as data. Wrapping it in this model keeps
    the provenance attached to the text everywhere it travels, and
    :meth:`to_prompt_block` renders it inside a fence that names its origin.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    source: str
    trust: TrustLevel = TrustLevel.UNTRUSTED
    truncated: bool = False
    original_length: int = 0

    def to_prompt_block(self) -> str:
        """Render the text inside an explicit untrusted-data fence."""
        note = " (truncated)" if self.truncated else ""
        return (
            f"<<<BEGIN UNTRUSTED DATA: {self.source}{note}>>>\n"
            f"{self.text}\n"
            f"<<<END UNTRUSTED DATA: {self.source}>>>"
        )


class AIPolicy(StrEnum):
    """What a repository's contribution rules say about AI-generated work."""

    PROHIBITED = "prohibited"
    RESTRICTED = "restricted"
    ALLOWED = "allowed"
    UNKNOWN = "unknown"


class ContributionRules(BaseModel):
    """Rules detected from a repository's contribution guidance.

    Detection is keyword based and deliberately conservative: every non-unknown
    verdict carries the verbatim line that produced it, so a human can check it.
    """

    ai_policy: AIPolicy = AIPolicy.UNKNOWN
    ai_policy_evidence: list[str] = Field(default_factory=list)
    requires_tests: bool = False
    requires_signoff: bool = False
    requires_cla: bool = False
    sources: list[str] = Field(default_factory=list)


class FileExcerpt(BaseModel):
    """One repository file, size-limited and marked untrusted."""

    path: str
    size_bytes: int
    content: UntrustedText


class RepoContext(BaseModel):
    """Everything the analyzer needs about a shortlisted issue (SPEC 2D).

    Every free-text field is an :class:`UntrustedText`; there is no plain string
    field carrying repository or issue prose.
    """

    repo_full_name: str
    issue_number: int
    workspace_path: str
    issue: UntrustedText
    readme: UntrustedText | None = None
    contributing: list[UntrustedText] = Field(default_factory=list)
    test_layout: list[str] = Field(default_factory=list)
    relevant_files: list[FileExcerpt] = Field(default_factory=list)
    contribution_rules: ContributionRules = Field(default_factory=ContributionRules)
    total_bytes: int = 0
    truncated: bool = False
    cloned_at: datetime | None = None


# --------------------------------------------------------------------------
# Phase 2: LLM analysis and cost tracking
# --------------------------------------------------------------------------


class TriageResult(BaseModel):
    """Validated output of the cheap triage call."""

    model_config = ConfigDict(extra="forbid")

    actionable: bool
    problem_summary: str = ""
    clarity: float = Field(default=0.0, ge=0.0, le=1.0)
    scope_hint: str = "unclear"
    missing_information: list[str] = Field(default_factory=list)
    injection_flagged: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class AnalysisResult(BaseModel):
    """Validated output of the strong analysis call (SPEC section 2E).

    Every field the spec requires is present and non-optional, so a model that
    omits the test plan or the risks fails validation instead of silently
    producing a half-filled report.
    """

    model_config = ConfigDict(extra="forbid")

    problem_explanation: str = Field(min_length=1)
    minimal_fix_proposal: str = Field(min_length=1)
    risks: list[str] = Field(default_factory=list)
    test_plan: list[str] = Field(default_factory=list)
    files_likely_involved: list[str] = Field(default_factory=list)
    unverified_assumptions: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    triage: TriageResult | None = None


class ModelPricing(BaseModel):
    """Per-million-token prices for one model, in US dollars.

    Held in configuration rather than hard-coded, because prices change and a
    wrong constant would silently corrupt every cost figure.
    """

    model_config = ConfigDict(extra="forbid")

    input_per_million: float = Field(ge=0.0)
    output_per_million: float = Field(ge=0.0)


class LLMUsage(BaseModel):
    """One recorded model call, for cost tracking and auditing."""

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    created_at: datetime | None = None
    repo_full_name: str
    issue_number: int
    purpose: str
    model: str
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    success: bool = True
    error: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def compute_cost_usd(
    prompt_tokens: int,
    completion_tokens: int,
    pricing: ModelPricing,
) -> float:
    """Return the cost of one call, rounded to six decimal places."""
    cost = (
        prompt_tokens * pricing.input_per_million + completion_tokens * pricing.output_per_million
    ) / 1_000_000
    return round(cost, 6)


class PatchRecord(BaseModel):
    """A captured patch and its content hash (SPEC sections 2H and 2I).

    The hash exists so approval can be bound to an exact change: if the patch
    changes, the hash changes, and any earlier approval no longer applies.
    """

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    created_at: datetime | None = None
    repo_full_name: str
    issue_number: int
    provider: str
    session_id: str | None = None
    patch_sha256: str = Field(min_length=64, max_length=64)
    diff: str = ""
    files_changed: int = Field(default=0, ge=0)
    additions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)
    isolated: bool = True
    notes: str = ""


class AnalysisRecord(BaseModel):
    """A persisted analysis, so a review can be regenerated later.

    The payload is stored as JSON text rather than as columns. The analysis
    schema will evolve faster than a table should, and the report only needs to
    read the whole object back, so normalising it would add migration work
    without buying anything.
    """

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    created_at: datetime | None = None
    repo_full_name: str
    issue_number: int
    model: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    payload_json: str
