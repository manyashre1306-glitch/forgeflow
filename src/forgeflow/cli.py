"""ForgeFlow command-line interface.

Phase 1 scope: discover public GitHub issues from an allowlist, deduplicate,
rank with deterministic heuristics, persist to SQLite, and write Markdown
reports. ForgeFlow never writes to an external repository in Phase 1.

The orchestration functions (``run_discover``, ``run_rank``, ``run_report``,
``run_shortlist``) are plain functions rather than Typer callbacks so tests can
call them directly with an injected fake client. No test touches the network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from forgeflow import approval, audit, killswitch
from forgeflow import report as report_module
from forgeflow.agent_runner import AgentUnavailableError, get_agent
from forgeflow.approval import ApprovalError, storage
from forgeflow.config import (
    AppConfig,
    ConfigError,
    KillSwitchEngagedError,
    Settings,
    load_config,
    require_kill_switch_clear,
)
from forgeflow.dedupe import DroppedIssue, deduplicate, filter_issues
from forgeflow.discovery import DiscoveryResult, collect_linked_open_prs, discover_issues
from forgeflow.github_client import GitHubClient, GitHubError, GitHubReader, MissingTokenError
from forgeflow.issue_analyzer import (
    AnalyzerError,
    BudgetExceededError,
    ModelUnavailableError,
    build_analyzer,
)
from forgeflow.models import (
    ApprovalAction,
    IllegalTransitionError,
    Issue,
    TaskState,
)
from forgeflow.pipeline import (
    PipelineError,
    run_analysis,
    run_context,
    run_implement,
    run_review,
    run_validation,
)
from forgeflow.ranking import rank_issues, resolve_weights, total_for
from forgeflow.repository_context import (
    RepositoryContextError,
    list_workspaces,
    prune_workspaces,
    remove_workspace,
    workspace_size_bytes,
)
from forgeflow.sandbox import IsolationError, build_sandbox
from forgeflow.validation import build_validator

DEFAULT_CONFIG_PATH = Path("config/repositories.yaml")
DEFAULT_DB_PATH = Path("data/forgeflow.db")

app = typer.Typer(
    name="forgeflow",
    help="ForgeFlow: AI-powered open-source contribution automation (Phase 1).",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


class ForgeFlowError(RuntimeError):
    """A user-facing failure. The CLI prints the message and exits non-zero."""


# --- shared helpers ---------------------------------------------------------


def load_app_config(config_path: Path) -> AppConfig:
    """Load the allowlist config, converting config problems into ForgeFlowError."""
    try:
        return load_config(config_path)
    except ConfigError as exc:
        raise ForgeFlowError(str(exc)) from exc


def build_client() -> GitHubClient:
    """Create the GitHub client from the environment.

    Raises ``ForgeFlowError`` with a clear message when the token is missing, so
    the CLI exits non-zero instead of attempting an unauthenticated request.
    """
    settings = Settings()
    token = settings.github_token.get_secret_value() if settings.github_token else None
    try:
        return GitHubClient(token)
    except MissingTokenError as exc:
        raise ForgeFlowError(str(exc)) from exc


def open_db(db_path: Path):
    """Open and initialize the database, creating parent directories."""
    conn = storage.connect(db_path)
    storage.init_db(conn)
    return conn


def parse_issue_reference(reference: str) -> tuple[str, int]:
    """Parse ``owner/repo#123`` into ``(owner/repo, 123)``."""
    if "#" not in reference:
        raise ForgeFlowError(
            f"invalid issue reference {reference!r}; expected the form owner/repo#123"
        )
    repo_part, _, number_part = reference.rpartition("#")
    repo_part = repo_part.strip()
    if not repo_part or "/" not in repo_part:
        raise ForgeFlowError(
            f"invalid issue reference {reference!r}; expected the form owner/repo#123"
        )
    try:
        number = int(number_part)
    except ValueError as exc:
        raise ForgeFlowError(
            f"invalid issue number in {reference!r}; expected a positive integer"
        ) from exc
    if number < 1:
        raise ForgeFlowError(f"invalid issue number in {reference!r}; must be 1 or greater")
    return repo_part, number


# --- orchestration ----------------------------------------------------------


def run_discover(
    client: GitHubReader,
    config: AppConfig,
    db_path: Path,
    *,
    per_repo_limit: int = 100,
    detect_tests: bool = True,
    fetch_linked_prs: bool = True,
) -> DiscoveryResult:
    """Discover, deduplicate, filter, and store issues.

    Idempotent: running this twice leaves the row count unchanged, because every
    write is an upsert keyed on (repo_full_name, issue_number).
    """
    result = discover_issues(
        client, config, per_repo_limit=per_repo_limit, detect_tests=detect_tests
    )
    conn = open_db(db_path)
    try:
        run_id = storage.start_run(conn, "discover")
        for repo in result.repositories:
            storage.upsert_repository(conn, repo)

        unique, duplicates = deduplicate(result.issues)

        if fetch_linked_prs:
            enriched: list[Issue] = []
            for issue in unique:
                linked = collect_linked_open_prs(client, issue)
                enriched.append(
                    issue.model_copy(update={"linked_open_prs": linked}) if linked else issue
                )
            unique = enriched

        filtered = filter_issues(
            unique,
            exclude_labels=config.exclude_labels,
            opt_out_labels=config.opt_out_labels,
        )

        for issue in filtered.kept:
            storage.upsert_issue(conn, issue)
        for dropped in [*duplicates, *filtered.dropped]:
            storage.upsert_excluded_issue(
                conn,
                repo_full_name=dropped.issue.repo_full_name,
                number=dropped.issue.number,
                reason=dropped.reason.value,
                detail=dropped.detail,
                title=dropped.issue.title,
                url=dropped.issue.url,
            )

        storage.finish_run(conn, run_id, status="completed", issues_seen=len(filtered.kept))
    finally:
        conn.close()
    return result


def run_rank(db_path: Path, config: AppConfig | None = None) -> list:
    """Rank stored issues, persist the scores, and return the ranked list."""
    conn = open_db(db_path)
    try:
        issues = storage.list_issues(conn)
        repositories = {repo.full_name: repo for repo in storage.list_repositories(conn)}
        ranked = rank_issues(issues, config, repositories)
        for entry in ranked:
            storage.upsert_issue_score(conn, entry.score)
        return ranked
    finally:
        conn.close()


def run_report(
    db_path: Path,
    config: AppConfig | None = None,
    *,
    output: Path | None = None,
) -> Path:
    """Write a Markdown report of ranked and excluded issues. Returns the path."""
    conn = open_db(db_path)
    try:
        issues = storage.list_issues(conn)
        repositories = {repo.full_name: repo for repo in storage.list_repositories(conn)}
        ranked = rank_issues(issues, config, repositories)
        for entry in ranked:
            storage.upsert_issue_score(conn, entry.score)
        weights = resolve_weights(config)
        dropped = _stored_exclusions(conn)
    finally:
        conn.close()

    target = output or report_module.default_report_path()
    return report_module.write_report(target, ranked, dropped, weights)


def _stored_exclusions(conn) -> list[DroppedIssue]:
    """Rebuild DroppedIssue objects from the exclusion table for reporting."""
    dropped: list[DroppedIssue] = []
    for row in storage.list_excluded_issues(conn):
        issue = Issue(
            repo_full_name=row["repo_full_name"],
            number=row["number"],
            title=row["title"] or "(title not recorded)",
            url=row["url"],
        )
        dropped.append(DroppedIssue(issue=issue, reason=row["reason"], detail=row["detail"]))
    return dropped


def run_shortlist(
    db_path: Path,
    reference: str,
    *,
    reason: str = "",
) -> Issue:
    """Move a stored issue to the ``shortlisted`` state.

    Enforces the SPEC section 10 state machine: only a legal transition from the
    issue's current state is accepted.
    """
    repo_full_name, number = parse_issue_reference(reference)
    conn = open_db(db_path)
    try:
        try:
            return storage.transition_task_state(
                conn,
                repo_full_name,
                number,
                TaskState.SHORTLISTED,
                actor="cli",
                reason=reason or "shortlisted by user",
            )
        except ValueError as exc:
            raise ForgeFlowError(str(exc)) from exc
    finally:
        conn.close()


# --- commands ---------------------------------------------------------------


@app.command()
def discover(
    config_path: Annotated[
        Path, typer.Option("--config", "-c", help="Path to the allowlist YAML.")
    ] = DEFAULT_CONFIG_PATH,
    db_path: Annotated[
        Path, typer.Option("--db", help="Path to the SQLite database.")
    ] = DEFAULT_DB_PATH,
    limit: Annotated[
        int, typer.Option("--limit", "-n", help="Maximum issues to fetch per repository.")
    ] = 100,
    detect_tests: Annotated[
        bool, typer.Option("--detect-tests/--no-detect-tests", help="Probe for a test suite.")
    ] = True,
    linked_prs: Annotated[
        bool,
        typer.Option("--linked-prs/--no-linked-prs", help="Check timelines for linked open PRs."),
    ] = True,
) -> None:
    """Discover open issues from the allowlist and store them."""
    try:
        config = load_app_config(config_path)
        with build_client() as client:
            result = run_discover(
                client,
                config,
                db_path,
                per_repo_limit=limit,
                detect_tests=detect_tests,
                fetch_linked_prs=linked_prs,
            )
    except ForgeFlowError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except GitHubError as exc:
        console.print(f"[red]GitHub error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    conn = open_db(db_path)
    try:
        kept = storage.count_issues(conn)
        excluded = storage.count_excluded_issues(conn)
    finally:
        conn.close()

    console.print(
        f"Discovered {len(result.issues)} issue payload(s) across "
        f"{len(result.repositories)} repository/ies."
    )
    console.print(f"Stored {kept} eligible issue(s); recorded {excluded} exclusion(s).")
    for error in result.errors:
        console.print(f"[yellow]warning:[/yellow] {error}")


@app.command()
def rank(
    config_path: Annotated[Path, typer.Option("--config", "-c")] = DEFAULT_CONFIG_PATH,
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB_PATH,
    top: Annotated[int, typer.Option("--top", "-n", help="How many to display.")] = 10,
) -> None:
    """Rank stored issues with the six weighted factors."""
    try:
        config = load_app_config(config_path)
    except ForgeFlowError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    ranked = run_rank(db_path, config)
    weights = resolve_weights(config)

    if not ranked:
        console.print("No issues to rank. Run 'discover' first.")
        return

    table = Table(title="Ranked issues", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("Issue")
    table.add_column("Total", justify="right")
    table.add_column("Title", overflow="fold")
    for entry in ranked[:top]:
        table.add_row(
            str(entry.rank),
            f"{entry.issue.repo_full_name}#{entry.issue.number}",
            f"{total_for(entry.score, weights):.2f}",
            entry.issue.title,
        )
    console.print(table)
    console.print("[dim]Scores are initial heuristics, not predictors of PR acceptance.[/dim]")


@app.command()
def report(
    config_path: Annotated[Path, typer.Option("--config", "-c")] = DEFAULT_CONFIG_PATH,
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB_PATH,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Report path. Defaults to data/reports/<timestamp>.md"),
    ] = None,
) -> None:
    """Write a Markdown report of ranked and excluded issues."""
    try:
        config = load_app_config(config_path)
    except ForgeFlowError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    path = run_report(db_path, config, output=output)
    console.print(f"Wrote report to {path}")


@app.command()
def shortlist(
    issue: Annotated[str, typer.Argument(help="Issue reference, e.g. pydantic/pydantic#1234.")],
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB_PATH,
    reason: Annotated[str, typer.Option("--reason", help="Why it was shortlisted.")] = "",
) -> None:
    """Shortlist a stored issue, moving it to the 'shortlisted' state."""
    try:
        updated = run_shortlist(db_path, issue, reason=reason)
    except ForgeFlowError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"{updated.repo_full_name}#{updated.number} is now "
        f"[green]{updated.task_state.value}[/green]"
    )


# --------------------------------------------------------------------------
# Phase 2 commands: context, analyze, implement, review
# --------------------------------------------------------------------------


@app.command()
def context(
    issue: Annotated[str, typer.Argument(help="Issue reference, e.g. pydantic/pydantic#1234.")],
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB_PATH,
    workspace_root: Annotated[Path, typer.Option("--workspace-root")] = Path("data/workspaces"),
    local_path: Annotated[
        Path | None,
        typer.Option("--local-path", help="Clone from a local path instead of GitHub."),
    ] = None,
) -> None:
    """Extract a repository context bundle for an issue (read-only)."""
    repo_full_name, number = parse_issue_reference(issue)
    try:
        conn = open_db(db_path)
    except ForgeFlowError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    try:
        outcome = run_context(
            conn,
            repo_full_name,
            number,
            workspace_root=workspace_root,
            source_path=local_path,
            clone=True,
        )
    except (PipelineError, RepositoryContextError, GitHubError) as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    ctx = outcome.context
    rules = ctx.contribution_rules
    console.print(f"Context built for [bold]{repo_full_name}#{number}[/bold]")
    console.print(f"  workspace:        {ctx.workspace_path}")
    console.print(f"  relevant files:   {len(ctx.relevant_files)}")
    console.print(f"  test entries:     {len(ctx.test_layout)}")
    console.print(f"  total bytes:      {ctx.total_bytes}")
    console.print(f"  truncated:        {ctx.truncated}")
    console.print(f"  AI policy:        [bold]{rules.ai_policy.value}[/bold]")
    console.print(f"  requires tests:   {rules.requires_tests}")
    console.print(f"  requires signoff: {rules.requires_signoff}")
    console.print(f"  task state:       {outcome.issue.task_state.value}")
    if rules.ai_policy.value == "unknown":
        console.print(
            "[yellow]note:[/yellow] no AI policy found in repository guidance; "
            "unknown is not permission."
        )


@app.command()
def analyze(
    issue: Annotated[str, typer.Argument(help="Issue reference, e.g. pydantic/pydantic#1234.")],
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB_PATH,
    config_path: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG_PATH,
    workspace_root: Annotated[Path, typer.Option("--workspace-root")] = Path("data/workspaces"),
    skip_triage: Annotated[
        bool, typer.Option("--skip-triage", help="Analyse without the cheap triage call.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Re-analyse even if one is already stored.")
    ] = False,
) -> None:
    """Analyse an issue with the configured models, then await plan approval."""
    repo_full_name, number = parse_issue_reference(issue)
    try:
        config = load_app_config(config_path)
        conn = open_db(db_path)
        # Check all three sources: the config file, the environment, and the
        # database flag. Checking only the config would let a database stop be
        # bypassed by this command.
        require_kill_switch_clear(config, None, conn)
    except KillSwitchEngagedError as exc:
        console.print(f"[red]kill switch:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except ForgeFlowError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    try:
        analyzer = build_analyzer(config.llm, conn=conn, app_config=config)
        outcome = run_analysis(
            conn,
            analyzer,
            repo_full_name,
            number,
            workspace_root=workspace_root,
            skip_triage=skip_triage,
            force=force,
        )
    except BudgetExceededError as exc:
        console.print(f"[red]budget:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except KillSwitchEngagedError as exc:
        console.print(f"[red]kill switch:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except ModelUnavailableError as exc:
        console.print(f"[red]model unavailable:[/red] {exc}")
        console.print(
            "[yellow]note:[/yellow] set GEMINI_API_KEY in the environment to enable analysis."
        )
        raise typer.Exit(code=1) from exc
    except (AnalyzerError, PipelineError, RepositoryContextError) as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    result = outcome.analysis
    console.print(f"Analysis for [bold]{repo_full_name}#{number}[/bold]")
    console.print(f"  problem:  {result.problem_explanation.strip()}")
    console.print(f"  fix:      {result.minimal_fix_proposal.strip()}")
    console.print(f"  risks:    {len(result.risks)}")
    console.print(f"  tests:    {len(result.test_plan)}")
    console.print(f"  state:    [green]{outcome.issue.task_state.value}[/green]")


@app.command()
def implement(
    issue: Annotated[str, typer.Argument(help="Issue reference, e.g. pydantic/pydantic#1234.")],
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB_PATH,
    config_path: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG_PATH,
    workspace_root: Annotated[Path, typer.Option("--workspace-root")] = Path("data/workspaces"),
    provider: Annotated[
        str, typer.Option("--provider", help="Coding agent provider: mock or jules.")
    ] = "mock",
    plan: Annotated[
        str, typer.Option("--plan", help="Approved plan text handed to the agent.")
    ] = "",
    allow_external: Annotated[
        bool,
        typer.Option("--allow-external", help="Acknowledge that the provider is a remote service."),
    ] = False,
) -> None:
    """Run a coding agent on an approved plan and record a hashed patch.

    The task must be at 'awaiting_plan_approval'. With no container isolation the
    patch is recorded as unverified, and the review report says so.
    """
    repo_full_name, number = parse_issue_reference(issue)
    if not plan:
        console.print("[red]error:[/red] --plan is required (the approved plan text)")
        raise typer.Exit(code=1)

    try:
        config = load_app_config(config_path)
        conn = open_db(db_path)
        require_kill_switch_clear(config, None, conn)
    except KillSwitchEngagedError as exc:
        console.print(f"[red]kill switch:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except ForgeFlowError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    sandbox = build_sandbox(app_config=config)
    if not sandbox.is_isolated:
        console.print("[yellow]warning:[/yellow] no container isolation available.")
        console.print(
            "  Repository code will NOT be executed. Only the agent's own diff will be captured."
        )

    try:
        agent = get_agent(provider, allow_external=allow_external)
        outcome = run_implement(
            conn,
            agent,
            sandbox,
            repo_full_name,
            number,
            plan,
            workspace_root=workspace_root,
        )
    except KillSwitchEngagedError as exc:
        console.print(f"[red]kill switch:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except (AgentUnavailableError, IsolationError, PipelineError, RepositoryContextError) as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    patch = outcome.patch
    console.print(f"Implementation for [bold]{repo_full_name}#{number}[/bold]")
    console.print(f"  provider:   {outcome.agent_result.provider}")
    if patch is not None:
        console.print(f"  files:      {patch.files_changed}")
        console.print(f"  +{patch.additions} -{patch.deletions}")
        console.print(f"  sha256:     {patch.patch_sha256}")
    else:
        console.print("  patch:      none produced")
    console.print(f"  state:      {outcome.issue.task_state.value}")


@app.command()
def review(
    issue: Annotated[str, typer.Argument(help="Issue reference, e.g. pydantic/pydantic#1234.")],
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB_PATH,
    report_dir: Annotated[Path, typer.Option("--report-dir")] = Path("data/reports"),
) -> None:
    """Write a local Markdown review bundle for an issue (read-only)."""
    repo_full_name, number = parse_issue_reference(issue)
    try:
        conn = open_db(db_path)
    except ForgeFlowError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    try:
        outcome = run_review(conn, repo_full_name, number, report_dir=report_dir)
    except PipelineError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    console.print(f"Review bundle written to [bold]{outcome.report_path}[/bold]")
    if outcome.patch is None:
        console.print("  no patch recorded for this issue")
    else:
        console.print(f"  patch sha256: {outcome.patch.patch_sha256}")


@app.command()
def workspace(
    action: Annotated[str, typer.Argument(help="One of: list, prune, remove.")] = "list",
    issue: Annotated[
        str | None,
        typer.Option("--issue", help="Issue reference, required for remove."),
    ] = None,
    workspace_root: Annotated[Path, typer.Option("--workspace-root")] = Path("data/workspaces"),
    keep: Annotated[int, typer.Option("--keep", help="How many workspaces prune should keep.")] = 5,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would be removed, remove nothing.")
    ] = False,
) -> None:
    """List, prune, or remove cloned workspaces under data/workspaces/.

    Clones are large and disposable. Nothing here touches the database or any
    remote, so it is always safe to run.
    """
    if action == "list":
        entries = list_workspaces(workspace_root)
        if not entries:
            console.print(f"no workspaces under {workspace_root}")
            return
        table = Table(title=f"workspaces under {workspace_root}")
        table.add_column("name")
        table.add_column("size", justify="right")
        for entry in entries:
            size = workspace_size_bytes(entry)
            table.add_row(entry.name, f"{size / 1024 / 1024:.1f} MiB")
        console.print(table)
        return

    if action == "prune":
        if dry_run:
            entries = list_workspaces(workspace_root)
            if len(entries) <= keep:
                console.print(f"nothing to prune ({len(entries)} present, keeping {keep})")
                return
            ordered = sorted(entries, key=lambda path: path.stat().st_mtime, reverse=True)
            for path in ordered[keep:]:
                console.print(f"would remove {path}")
            return
        try:
            removed = prune_workspaces(workspace_root, keep=keep)
        except ValueError as exc:
            console.print(f"[red]error:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        if not removed:
            console.print(f"nothing to prune (keeping {keep})")
            return
        console.print(f"removed {len(removed)} workspace(s), kept {keep}")
        for path in removed:
            console.print(f"  {path}")
        return

    if action == "remove":
        if issue is None:
            console.print("[red]error:[/red] --issue is required for remove")
            raise typer.Exit(code=1)
        repo_full_name, number = parse_issue_reference(issue)
        removed = remove_workspace(workspace_root, repo_full_name, number)
        if removed:
            console.print(f"removed workspace for {repo_full_name}#{number}")
        else:
            console.print(f"no workspace found for {repo_full_name}#{number}")
        return

    console.print(f"[red]error:[/red] unknown action {action!r}; use list, prune, or remove")
    raise typer.Exit(code=1)


@app.command("kill-switch")
def kill_switch(
    action: Annotated[str, typer.Argument(help="One of: status, on, off.")] = "status",
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB_PATH,
    reason: Annotated[str, typer.Option("--reason", help="Why the switch was flipped.")] = "",
    actor: Annotated[str, typer.Option("--actor", help="Who flipped it.")] = "operator",
) -> None:
    """Engage, disengage, or inspect the persistent kill switch.

    Engaging stops all model calls and sandbox execution. This is the database
    flag; the configuration file and FORGEFLOW_KILL_SWITCH do the same thing.
    Any one of the three is enough to stop a run.
    """
    if action not in {"status", "on", "off"}:
        console.print(f"[red]error:[/red] unknown action {action!r}; use status, on, or off")
        raise typer.Exit(code=1)

    conn = open_db(db_path)
    try:
        if action == "status":
            state = killswitch.read_state(conn)
            table = Table(title="kill switch")
            table.add_column("field")
            table.add_column("value")
            table.add_row("engaged", "yes" if state.engaged else "no")
            table.add_row("reason", state.reason or "-")
            table.add_row("changed by", state.changed_by or "-")
            table.add_row("changed at", state.changed_at.isoformat() if state.changed_at else "-")
            console.print(table)

            # Report the other two sources too, so the operator sees the whole
            # picture rather than only the database flag.
            from forgeflow.config import kill_switch_engaged

            engaged, why = kill_switch_engaged(None, None, conn)
            console.print(
                f"effective: {'ENGAGED' if engaged else 'clear'} ({why or 'no source engaged'})"
            )
            return

        engaged = action == "on"
        state = killswitch.set_state(conn, engaged, reason=reason, actor=actor)
        audit.record(
            conn,
            "kill_switch.engaged" if engaged else "kill_switch.disengaged",
            actor=actor,
            severity="critical" if engaged else "info",
            detail=reason or f"kill switch turned {action}",
        )
        console.print(f"kill switch {'ENGAGED' if state.engaged else 'DISENGAGED'}")
        if state.engaged:
            console.print("All model calls and sandbox execution will now be refused.")
    finally:
        conn.close()


@app.command()
def audit_log(
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB_PATH,
    issue: Annotated[
        str | None, typer.Option("--issue", help="Filter to one issue reference.")
    ] = None,
    event: Annotated[str | None, typer.Option("--event", help="Filter to one event name.")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 20,
    verify: Annotated[
        bool, typer.Option("--verify", help="Prove the append-only triggers are active.")
    ] = False,
) -> None:
    """Show the append-only audit log, newest first."""
    conn = open_db(db_path)
    try:
        if verify:
            ok, why = audit.verify_append_only(conn)
            if ok:
                console.print("append-only: verified (UPDATE and DELETE both refused)")
            else:
                console.print(f"[red]append-only: FAILED[/red] {why}")
                raise typer.Exit(code=1)

        repo_full_name = None
        issue_number = None
        if issue is not None:
            repo_full_name, issue_number = parse_issue_reference(issue)

        events = audit.list_events(
            conn,
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            event=event,
            limit=limit,
        )
        if not events:
            console.print("no audit events")
            return

        table = Table(title=f"audit log ({audit.count_events(conn)} total)")
        table.add_column("when")
        table.add_column("event")
        table.add_column("issue")
        table.add_column("severity")
        table.add_column("detail")
        for item in events:
            ref = (
                f"{item.repo_full_name}#{item.issue_number}"
                if item.repo_full_name and item.issue_number
                else "-"
            )
            table.add_row(
                item.created_at.strftime("%Y-%m-%d %H:%M:%S") if item.created_at else "-",
                item.event,
                ref,
                item.severity,
                item.detail[:60],
            )
        console.print(table)
    finally:
        conn.close()


def _approval_common(
    issue: str,
    db_path: Path,
    config_path: Path,
    target_branch: str,
    actor: str,
    reason: str,
    action: ApprovalAction,
) -> None:
    """Shared body for the four approval commands.

    Kept in one place so the four actions cannot drift apart in how they check
    the kill switch or report a failure.
    """
    repo_full_name, number = parse_issue_reference(issue)
    try:
        config = load_app_config(config_path)
        conn = open_db(db_path)
        require_kill_switch_clear(config, None, conn)
    except KillSwitchEngagedError as exc:
        console.print(f"[red]kill switch:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except ForgeFlowError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    try:
        decision, state = approval.apply_action(
            conn,
            repo_full_name,
            number,
            action,
            actor=actor,
            reason=reason,
            target_branch=target_branch,
        )
    except (ApprovalError, IllegalTransitionError) as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    console.print(f"{action.value}: {repo_full_name}#{number}")
    console.print(f"  patch sha256: {decision.patch_sha256}")
    console.print(f"  actor:        {decision.actor}")
    console.print(f"  state:        {state.value}")
    if action is ApprovalAction.APPROVE:
        console.print(
            "[yellow]note:[/yellow] this approval is bound to that exact patch hash. "
            "If the patch changes, approval must be given again."
        )


@app.command()
def approve(
    issue: Annotated[str, typer.Argument(help="Issue reference, e.g. owner/repo#123.")],
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB_PATH,
    config_path: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG_PATH,
    target_branch: Annotated[
        str, typer.Option("--target-branch", help="Branch the patch is approved for.")
    ] = "",
    actor: Annotated[str, typer.Option("--actor", help="Who is approving.")] = "operator",
    reason: Annotated[str, typer.Option("--reason")] = "",
) -> None:
    """Approve the latest patch for publishing, bound to its exact hash.

    This records consent only. Nothing is published: publishing is Phase 4.
    """
    _approval_common(
        issue, db_path, config_path, target_branch, actor, reason, ApprovalAction.APPROVE
    )


@app.command("request-changes")
def request_changes(
    issue: Annotated[str, typer.Argument(help="Issue reference, e.g. owner/repo#123.")],
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB_PATH,
    config_path: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG_PATH,
    target_branch: Annotated[str, typer.Option("--target-branch")] = "",
    actor: Annotated[str, typer.Option("--actor")] = "operator",
    reason: Annotated[str, typer.Option("--reason", help="What must change.")] = "",
) -> None:
    """Send the task back to implementing for another attempt."""
    _approval_common(
        issue, db_path, config_path, target_branch, actor, reason, ApprovalAction.REQUEST_CHANGES
    )


@app.command()
def reject(
    issue: Annotated[str, typer.Argument(help="Issue reference, e.g. owner/repo#123.")],
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB_PATH,
    config_path: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG_PATH,
    target_branch: Annotated[str, typer.Option("--target-branch")] = "",
    actor: Annotated[str, typer.Option("--actor")] = "operator",
    reason: Annotated[str, typer.Option("--reason")] = "",
) -> None:
    """Reject the patch. The task is discarded."""
    _approval_common(
        issue, db_path, config_path, target_branch, actor, reason, ApprovalAction.REJECT
    )


@app.command()
def discard(
    issue: Annotated[str, typer.Argument(help="Issue reference, e.g. owner/repo#123.")],
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB_PATH,
    config_path: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG_PATH,
    target_branch: Annotated[str, typer.Option("--target-branch")] = "",
    actor: Annotated[str, typer.Option("--actor")] = "operator",
    reason: Annotated[str, typer.Option("--reason")] = "",
) -> None:
    """Abandon the task without judging the patch."""
    _approval_common(
        issue, db_path, config_path, target_branch, actor, reason, ApprovalAction.DISCARD
    )


@app.command()
def validate(
    issue: Annotated[str, typer.Argument(help="Issue reference, e.g. owner/repo#123.")],
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB_PATH,
    config_path: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG_PATH,
    workspace_root: Annotated[Path, typer.Option("--workspace-root")] = Path("data/workspaces"),
    timeout: Annotated[
        float, typer.Option("--timeout", help="Per-check wall-clock limit in seconds.")
    ] = 900.0,
    repair_attempts: Annotated[
        int, typer.Option("--repair-attempts", help="Repairs already made (SPEC 7 caps this at 2).")
    ] = 0,
) -> None:
    """Run the target repository's tests and lint checks against the latest patch.

    Every command runs in the sandbox. With no container isolation the checks are
    recorded as skipped with the reason, and the task fails validation rather than
    advancing on unproven code.
    """
    repo_full_name, number = parse_issue_reference(issue)
    try:
        config = load_app_config(config_path)
        conn = open_db(db_path)
        require_kill_switch_clear(config, None, conn)
    except KillSwitchEngagedError as exc:
        console.print(f"[red]kill switch:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except ForgeFlowError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    try:
        validator = build_validator(
            sandbox=build_sandbox(app_config=config),
            timeout=timeout,
            app_config=config,
        )
        outcome = run_validation(
            conn,
            repo_full_name,
            number,
            validator=validator,
            workspace_root=workspace_root,
            repair_attempts=repair_attempts,
        )
    except PipelineError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except IsolationError as exc:
        console.print(f"[red]isolation:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    report = outcome.report
    table = Table(title=f"validation: {repo_full_name}#{number}")
    table.add_column("check")
    table.add_column("status")
    table.add_column("exit")
    table.add_column("detail")
    for check in report.checks:
        detail = check.skip_reason or check.limitation or ""
        table.add_row(
            check.name,
            check.status.value,
            str(check.exit_code) if check.exit_code is not None else "-",
            detail[:50],
        )
    console.print(table)

    if report.findings:
        console.print(f"[yellow]{len(report.findings)} diff finding(s):[/yellow]")
        for finding in report.findings:
            console.print(f"  [{finding.severity}] {finding.category}: {finding.detail}")

    for limitation in report.environment_limitations:
        console.print(f"[yellow]limitation:[/yellow] {limitation}")

    if outcome.passed:
        console.print(f"[green]validation passed[/green] - state: {outcome.issue.task_state.value}")
        return

    console.print(
        f"[red]validation FAILED[/red] - state: {outcome.issue.task_state.value}. "
        "Publishing is blocked."
    )
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
