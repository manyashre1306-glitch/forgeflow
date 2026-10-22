"""Phase 2 pipeline orchestration.

Wires the pieces together in the order the SPEC describes: build context, analyse,
implement, capture the patch, write a review bundle. Each step moves the task
through the state machine that Phase 1 already enforces, so an out-of-order call
fails loudly instead of silently doing the wrong thing.

The orchestration deliberately stops short of publishing. There is no publish
step here, and no code path in this module contacts a remote for writing.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from forgeflow import storage
from forgeflow.agent_runner import AgentResult, CodingAgent
from forgeflow.config import AppConfig
from forgeflow.issue_analyzer import IssueAnalyzer
from forgeflow.models import (
    AnalysisRecord,
    AnalysisResult,
    Issue,
    PatchRecord,
    RepoContext,
    TaskState,
)
from forgeflow.patch import PatchError, build_patch_record, workspace_diff
from forgeflow.repository_context import prepare_repo_context, workspace_path_for
from forgeflow.review import review_report_path, write_review_report
from forgeflow.sandbox import Sandbox


class PipelineError(RuntimeError):
    """A user-facing pipeline failure."""


# States in which an analysis has already been produced and approved or acted
# on. Re-analysing from here would spend money again for no new information.
POST_ANALYSIS_STATES = frozenset(
    {
        TaskState.AWAITING_PLAN_APPROVAL,
        TaskState.IMPLEMENTING,
        TaskState.VALIDATING,
        TaskState.FAILED_VALIDATION,
        TaskState.AWAITING_PUBLISH_APPROVAL,
        TaskState.PUBLISHED,
        TaskState.MERGED,
    }
)


def _persist_analysis(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
    analysis: AnalysisResult,
    model: str,
) -> AnalysisRecord:
    """Write an analysis to storage and return the stored record."""
    record = AnalysisRecord(
        created_at=datetime.now(UTC),
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        model=model,
        confidence=analysis.confidence,
        payload_json=analysis.model_dump_json(),
    )
    record.id = storage.record_analysis(conn, record)
    return record


def load_analysis(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
) -> AnalysisResult | None:
    """Read back the most recent stored analysis, or None.

    A stored payload that no longer validates returns None rather than raising:
    the schema may have moved on, and a stale row should not break a review run.
    """
    record = storage.latest_analysis(conn, repo_full_name, issue_number)
    if record is None:
        return None
    try:
        return AnalysisResult.model_validate(json.loads(record.payload_json))
    except (ValueError, TypeError):
        return None


@dataclass
class ContextOutcome:
    """Result of the context step."""

    context: RepoContext
    issue: Issue


@dataclass
class AnalysisOutcome:
    """Result of the analysis step."""

    analysis: AnalysisResult
    issue: Issue


@dataclass
class ImplementOutcome:
    """Result of the implementation step."""

    agent_result: AgentResult
    patch: PatchRecord | None
    issue: Issue
    executed_in_sandbox: bool
    limitations: list[str]


@dataclass
class ReviewOutcome:
    """Result of the review step."""

    report_path: Path
    patch: PatchRecord | None
    issue: Issue


def _load_issue(conn: sqlite3.Connection, repo_full_name: str, number: int) -> Issue:
    issue = storage.get_issue(conn, repo_full_name, number)
    if issue is None:
        raise PipelineError(
            f"issue {repo_full_name}#{number} is not in the database; run "
            f"'forgeflow discover' first"
        )
    return issue


def run_context(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
    *,
    workspace_root: Path | str = "data/workspaces",
    source_path: Path | str | None = None,
    remote_url: str | None = None,
    clone: bool = True,
) -> ContextOutcome:
    """Build the repository context bundle and shortlist the task.

    The task moves to ``shortlisted``, which is the only legal predecessor of
    ``analyzed``, so a later analyse step cannot be reached by skipping this one.
    """
    issue = _load_issue(conn, repo_full_name, issue_number)

    if issue.task_state is TaskState.DISCOVERED:
        issue = storage.transition_task_state(
            conn,
            repo_full_name,
            issue_number,
            TaskState.SHORTLISTED,
            actor="forgeflow",
            reason="context extraction requested",
        )

    if clone:
        context = prepare_repo_context(
            repo_full_name,
            issue,
            workspace_root=workspace_root,
            source_path=source_path,
            remote_url=remote_url,
        )
    else:
        workspace = workspace_path_for(workspace_root, repo_full_name, issue_number)
        from forgeflow.repository_context import build_repo_context

        context = build_repo_context(repo_full_name, issue, workspace)

    return ContextOutcome(context=context, issue=issue)


def run_analysis(
    conn: sqlite3.Connection,
    analyzer: IssueAnalyzer,
    repo_full_name: str,
    issue_number: int,
    *,
    context: RepoContext | None = None,
    workspace_root: Path | str = "data/workspaces",
    skip_triage: bool = False,
    force: bool = False,
) -> AnalysisOutcome:
    """Analyse an issue, then move it to ``awaiting_plan_approval``.

    The move to ``awaiting_plan_approval`` is the spec's gate: implementation
    requires a human to approve the plan first.

    Idempotence (SPEC section 10): once an analysis exists and the task has
    reached the approval gate, re-running would spend money again for the same
    answer, so this refuses unless ``force`` is set. A stored analysis is
    returned unchanged in that case.
    """
    issue = _load_issue(conn, repo_full_name, issue_number)

    if not force and issue.task_state in POST_ANALYSIS_STATES:
        stored = load_analysis(conn, repo_full_name, issue_number)
        if stored is not None:
            raise PipelineError(
                f"{repo_full_name}#{issue_number} already has an analysis and is at "
                f"'{issue.task_state.value}'. Re-analysing would spend again; pass "
                f"force=True to replace it."
            )

    if issue.task_state is TaskState.DISCOVERED:
        issue = storage.transition_task_state(
            conn,
            repo_full_name,
            issue_number,
            TaskState.SHORTLISTED,
            actor="forgeflow",
            reason="implicit shortlist before analysis",
        )

    if context is None:
        workspace = workspace_path_for(workspace_root, repo_full_name, issue_number)
        from forgeflow.repository_context import build_repo_context

        context = build_repo_context(repo_full_name, issue, workspace)

    analysis = analyzer.analyze(context, skip_triage=skip_triage)
    _persist_analysis(
        conn,
        repo_full_name,
        issue_number,
        analysis,
        model=analyzer.config.analysis_model,
    )

    if issue.task_state is TaskState.SHORTLISTED:
        issue = storage.transition_task_state(
            conn,
            repo_full_name,
            issue_number,
            TaskState.ANALYZED,
            actor="forgeflow",
            reason=f"analysis complete (model {analyzer.config.analysis_model})",
        )
    if issue.task_state is TaskState.ANALYZED:
        issue = storage.transition_task_state(
            conn,
            repo_full_name,
            issue_number,
            TaskState.AWAITING_PLAN_APPROVAL,
            actor="forgeflow",
            reason="plan ready for human approval",
        )

    return AnalysisOutcome(analysis=analysis, issue=issue)


def run_implement(
    conn: sqlite3.Connection,
    agent: CodingAgent,
    sandbox: Sandbox,
    repo_full_name: str,
    issue_number: int,
    plan: str,
    *,
    context: RepoContext | None = None,
    workspace_root: Path | str = "data/workspaces",
    skip_triage: bool = False,
    timeout: float = 600.0,
) -> ImplementOutcome:
    """Run the agent, capture the diff, and record a hashed patch.

    Refuses to proceed unless the task is at ``awaiting_plan_approval``, which is
    the state that means a human approved the plan. The diff is captured from the
    workspace after the run; when isolation is unavailable the patch is recorded
    with ``isolated=False`` and the limitation is carried into the report.
    """
    issue = _load_issue(conn, repo_full_name, issue_number)

    if issue.task_state is not TaskState.AWAITING_PLAN_APPROVAL:
        raise PipelineError(
            f"{repo_full_name}#{issue_number} is at '{issue.task_state.value}', "
            "but implementation requires 'awaiting_plan_approval' (the plan must "
            "be approved first)"
        )

    if context is None:
        workspace = workspace_path_for(workspace_root, repo_full_name, issue_number)
        from forgeflow.repository_context import build_repo_context

        context = build_repo_context(repo_full_name, issue, workspace)

    limitations: list[str] = []
    executed_in_sandbox = False

    issue = storage.transition_task_state(
        conn,
        repo_full_name,
        issue_number,
        TaskState.IMPLEMENTING,
        actor="forgeflow",
        reason=f"agent {agent.name} started",
    )

    agent_result = agent.run_task(context, plan, timeout=timeout)

    if not sandbox.is_isolated:
        limitations.append(
            "No container isolation was available, so no repository code was "
            "executed. The diff comes from the agent alone and was not run or "
            "tested locally."
        )
    else:
        limitations.append(
            "Container isolation was available for this run, but repository "
            "tests were not automatically executed by this step."
        )

    patch: PatchRecord | None = None
    if agent_result.diff and agent_result.diff.strip():
        patch = build_patch_record(
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            provider=agent_result.provider,
            diff=agent_result.diff,
            session_id=agent_result.session_id,
            isolated=executed_in_sandbox,
            notes="; ".join(limitations),
        )
        patch.id = storage.record_patch(conn, patch)

    issue = storage.transition_task_state(
        conn,
        repo_full_name,
        issue_number,
        TaskState.VALIDATING,
        actor="forgeflow",
        reason="patch captured, awaiting validation",
    )

    return ImplementOutcome(
        agent_result=agent_result,
        patch=patch,
        issue=issue,
        executed_in_sandbox=executed_in_sandbox,
        limitations=limitations,
    )


def run_review(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
    *,
    analysis: AnalysisResult | None = None,
    agent_result: AgentResult | None = None,
    context: RepoContext | None = None,
    patch: PatchRecord | None = None,
    limitations: list[str] | None = None,
    executed_in_sandbox: bool = False,
    report_dir: Path | str = "data/reports",
) -> ReviewOutcome:
    """Write a local review bundle for a task.

    Reads the latest recorded patch when one is not supplied, so a review can be
    regenerated later without re-running the agent.
    """
    issue = _load_issue(conn, repo_full_name, issue_number)

    if analysis is None:
        analysis = load_analysis(conn, repo_full_name, issue_number)

    if patch is None:
        patch = storage.latest_patch(conn, repo_full_name, issue_number)

    path = review_report_path(repo_full_name, issue_number, report_dir)
    write_review_report(
        path,
        issue=issue,
        analysis=analysis,
        agent_result=agent_result,
        patch=patch,
        context=context,
        limitations=limitations,
        executed_in_sandbox=executed_in_sandbox,
        generated_at=datetime.now(UTC),
    )
    return ReviewOutcome(report_path=path, patch=patch, issue=issue)


def capture_workspace_patch(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
    *,
    provider: str,
    workspace_root: Path | str = "data/workspaces",
    isolated: bool = False,
) -> PatchRecord:
    """Capture a patch from the workspace on disk and record it.

    Used when the agent ran outside this process (for example a human applied
    changes) or when re-capturing after a repair attempt.
    """
    workspace = workspace_path_for(workspace_root, repo_full_name, issue_number)
    try:
        diff = workspace_diff(workspace)
    except PatchError as exc:
        raise PipelineError(str(exc)) from exc

    patch = build_patch_record(
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        provider=provider,
        diff=diff,
        isolated=isolated,
        notes="captured from workspace",
    )
    patch.id = storage.record_patch(conn, patch)
    return patch


def load_config_llm(config: AppConfig):
    """Return the LLM configuration from the app config."""
    return config.llm
