"""Local review report for ForgeFlow Phase 2 (SPEC section 2H).

A review bundle must contain the issue URL, the analysis, the proposed fix, the
complete diff, the commands actually executed, the limitations, and a proposed
PR description. This module renders exactly those sections to Markdown under
``data/reports/``, which is gitignored.

Two things the report refuses to do:

* It never presents a diff as validated. When no sandbox ran the code, the
  report says so in its own section rather than implying the patch was tested.
* It never fabricates a test result. If no command was executed, the commands
  section says that explicitly instead of showing an empty table that could be
  mistaken for "all tests passed".
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from forgeflow.agent_runner import AgentCommand, AgentResult
from forgeflow.models import AnalysisResult, Issue, PatchRecord, RepoContext

DEFAULT_REPORT_DIR = Path("data/reports")


def review_report_path(
    repo_full_name: str,
    issue_number: int,
    directory: str | Path = DEFAULT_REPORT_DIR,
) -> Path:
    """Return a deterministic, human-readable path for one review report."""
    slug = repo_full_name.replace("/", "__")
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return Path(directory) / f"review-{slug}-{issue_number}-{stamp}.md"


def _section(title: str, body: str) -> str:
    return f"## {title}\n\n{body.strip()}\n"


def _render_commands(commands: list[AgentCommand]) -> str:
    if not commands:
        return (
            "_No commands were executed. Nothing in this report was validated by "
            "running code, and no test result is claimed._"
        )
    lines = ["| Command | Exit | Output |", "| --- | ---: | --- |"]
    for command in commands:
        exit_text = "-" if command.exit_code is None else str(command.exit_code)
        output = command.output.strip().replace("|", "\\|").replace("\n", " ")
        if len(output) > 300:
            output = output[:300] + " … (truncated)"
        lines.append(f"| `{command.command}` | {exit_text} | {output} |")
    return "\n".join(lines)


def _render_list(items: list[str], empty: str) -> str:
    if not items:
        return f"_{empty}_"
    return "\n".join(f"- {item}" for item in items)


def _render_context_sources(context: RepoContext | None) -> str:
    if context is None:
        return "_No repository context was extracted for this run._"
    lines = [
        f"- Workspace: `{context.workspace_path}`",
        f"- Relevant files extracted: {len(context.relevant_files)}",
        f"- Total context bytes: {context.total_bytes}",
        f"- Context truncated: {context.truncated}",
    ]
    if context.test_layout:
        lines.append(f"- Test layout entries: {len(context.test_layout)}")
    rules = context.contribution_rules
    lines.append(f"- Detected AI policy: **{rules.ai_policy.value}**")
    for evidence in rules.ai_policy_evidence:
        lines.append(f"  - evidence: {evidence}")
    lines.append(f"- Requires tests: {rules.requires_tests}")
    lines.append(f"- Requires sign-off: {rules.requires_signoff}")
    lines.append(f"- Requires CLA: {rules.requires_cla}")
    if rules.ai_policy.value == "unknown":
        lines.append(
            "- **Note:** no AI policy was found in the repository guidance. "
            "Unknown is not permission; check the repository yourself before "
            "publishing anything."
        )
    return "\n".join(lines)


def _proposed_pr_description(
    issue: Issue,
    analysis: AnalysisResult | None,
    patch: PatchRecord | None,
) -> str:
    fix = analysis.minimal_fix_proposal if analysis else "See the analysis section."
    body = [
        f"Fixes #{issue.number}",
        "",
        "## What this changes",
        "",
        fix.strip(),
    ]
    if analysis and analysis.problem_explanation:
        body += ["", "## Why", "", analysis.problem_explanation.strip()]
    if analysis and analysis.test_plan:
        body += ["", "## Testing", ""]
        body += [f"- {item}" for item in analysis.test_plan]
    if analysis and analysis.unverified_assumptions:
        body += ["", "## Not verified", ""]
        body += [f"- {item}" for item in analysis.unverified_assumptions]
    if patch is not None:
        body += [
            "",
            "---",
            "",
            f"Patch SHA-256: `{patch.patch_sha256}`",
            "",
            "This patch was prepared by an automated tool and reviewed by a human "
            "before submission.",
        ]
    return "\n".join(body)


def render_review_report(
    *,
    issue: Issue,
    analysis: AnalysisResult | None,
    agent_result: AgentResult | None,
    patch: PatchRecord | None,
    context: RepoContext | None = None,
    limitations: list[str] | None = None,
    generated_at: datetime | None = None,
    executed_in_sandbox: bool = False,
) -> str:
    """Render the full review bundle as Markdown."""
    moment = generated_at or datetime.now(UTC)
    parts: list[str] = []

    parts.append(f"# Review bundle: {issue.repo_full_name}#{issue.number}\n")
    parts.append(
        f"Generated: {moment.isoformat()}\n\n"
        f"- **Issue:** {issue.title}\n"
        f"- **URL:** {issue.url or f'https://github.com/{issue.repo_full_name}/issues/{issue.number}'}\n"
        f"- **Task state:** {issue.task_state.value}\n"
        f"- **Agent provider:** {agent_result.provider if agent_result else 'not run'}\n"
        f"- **Patch hash:** {patch.patch_sha256 if patch else 'no patch captured'}\n"
    )

    if not executed_in_sandbox:
        parts.append(
            "> **This patch was NOT executed or tested.** No isolated execution "
            "environment was available, so no repository code was run and no test "
            "result is claimed. Treat the diff as a proposal to review by hand, "
            "not as a verified fix.\n"
        )

    analysis_body = "_No analysis was produced for this run._"
    if analysis is not None:
        analysis_lines = [
            f"**Problem:** {analysis.problem_explanation.strip()}",
            "",
            f"**Minimal fix proposal:** {analysis.minimal_fix_proposal.strip()}",
            "",
            f"**Confidence:** {analysis.confidence:.2f}",
        ]
        if analysis.triage is not None:
            analysis_lines += [
                "",
                f"**Triage:** actionable={analysis.triage.actionable}, "
                f"scope={analysis.triage.scope_hint}, "
                f"clarity={analysis.triage.clarity:.2f}, "
                f"injection_flagged={analysis.triage.injection_flagged}",
            ]
        analysis_lines += ["", "**Risks:**", _render_list(analysis.risks, "none reported")]
        analysis_lines += [
            "",
            "**Test plan:**",
            _render_list(analysis.test_plan, "none proposed"),
        ]
        analysis_lines += [
            "",
            "**Files likely involved:**",
            _render_list(analysis.files_likely_involved, "not stated"),
        ]
        analysis_lines += [
            "",
            "**Unverified assumptions:**",
            _render_list(analysis.unverified_assumptions, "none stated"),
        ]
        analysis_body = "\n".join(analysis_lines)
    parts.append(_section("Analysis", analysis_body))

    parts.append(_section("Repository context", _render_context_sources(context)))

    commands = list(agent_result.commands) if agent_result else []
    parts.append(_section("Commands executed", _render_commands(commands)))

    if patch is not None:
        diff_body = (
            f"Files changed: {patch.files_changed} | "
            f"additions: {patch.additions} | deletions: {patch.deletions}\n\n"
            f"SHA-256: `{patch.patch_sha256}`\n\n"
            "```diff\n"
            f"{patch.diff.rstrip()}\n"
            "```"
        )
    else:
        diff_body = "_No patch was captured._"
    parts.append(_section("Proposed fix (full diff)", diff_body))

    default_limits = [
        "No isolated execution environment was available, so no repository code "
        "was run and no test result is claimed.",
        "Cost figures come from configured per-model prices; verify vendor "
        "pricing before relying on them.",
        "Analysis is model output and may be wrong; the human review is the control that matters.",
    ]
    parts.append(
        _section(
            "Limitations",
            _render_list(
                limitations if limitations is not None else default_limits, "none recorded"
            ),
        )
    )

    parts.append(
        _section(
            "Proposed PR description",
            "```markdown\n" + _proposed_pr_description(issue, analysis, patch) + "\n```",
        )
    )

    parts.append(
        _section(
            "Approval",
            "**Not approved and not published.** SPEC section 2I requires explicit "
            "human approval bound to the exact patch hash above. If the patch "
            "changes, this approval is void and a fresh one is required.",
        )
    )

    return "\n".join(parts)


def write_review_report(
    path: str | Path,
    **kwargs: object,
) -> Path:
    """Render a review report and write it to ``path``. Returns the path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = render_review_report(**kwargs)  # type: ignore[arg-type]
    target.write_text(content, encoding="utf-8")
    return target
