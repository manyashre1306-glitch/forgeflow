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

from forgeflow import report as report_module
from forgeflow import storage
from forgeflow.config import AppConfig, ConfigError, Settings, load_config
from forgeflow.dedupe import DroppedIssue, deduplicate, filter_issues
from forgeflow.discovery import DiscoveryResult, collect_linked_open_prs, discover_issues
from forgeflow.github_client import GitHubClient, GitHubError, GitHubReader, MissingTokenError
from forgeflow.models import Issue, TaskState
from forgeflow.ranking import rank_issues, resolve_weights, total_for

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


if __name__ == "__main__":
    app()
