"""Local-mode-only capabilities: the database, the sandbox, and publishing.

Every function here starts with :func:`web.modes.require_local`, so a hosted
deployment cannot reach any of them even if a page calls one directly. This
module is the *only* place in ``web/`` that imports ``forgeflow.publisher`` or
``forgeflow.publish_token``, and it is imported lazily by the pages, so a hosted
process never loads the publisher at all.

Nothing here re-implements a guard. The database, the sandbox, the approval
boundary, and the publisher are the real modules; this file only wires them to
the session and refuses when the mode is hosted.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from forgeflow.models import TaskState
from forgeflow.repository_context import workspace_path_for
from web.modes import ModeInfo, require_local

#: The operator's database, relative to the process working directory. Only read
#: in local mode.
DEFAULT_DB_PATH = "data/forgeflow.db"
DEFAULT_WORKSPACE_ROOT = "data/workspaces"


def open_local_db(mode: ModeInfo, db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open the real ForgeFlow database. Local mode only.

    Raises :class:`web.modes.HostedModeRefused` in hosted mode before any file is
    touched, so the database is never even opened there.
    """
    require_local(mode, "reading the local database")

    from forgeflow import storage

    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} does not exist. Run 'forgeflow discover' locally first, or use "
            "the pages that do not need the database."
        )
    conn = storage.connect(path)
    storage.init_db(conn)
    return conn


def sandbox_status(mode: ModeInfo) -> tuple[bool, str]:
    """Report whether a container runtime is available. Local mode only."""
    require_local(mode, "detecting a container runtime")

    from forgeflow.sandbox import detect_backend

    status = detect_backend()
    return status.isolated, status.reason


def run_validation(
    mode: ModeInfo,
    repo_full_name: str,
    issue_number: int,
    *,
    workspace_root: str | Path = DEFAULT_WORKSPACE_ROOT,
    timeout: float = 900.0,
):
    """Run the repository's tests and lint through the sandbox. Local mode only.

    The sandbox refuses when no container runtime is available; that refusal is
    returned as a report whose checks are ``SKIPPED``, never as a pass. Nothing
    runs on the host.
    """
    require_local(mode, "executing repository code")

    from forgeflow import storage
    from forgeflow.validation import build_validator

    conn = open_local_db(mode)
    try:
        patch = storage.latest_patch(conn, repo_full_name, issue_number)
        if patch is None:
            raise ValueError(f"no patch recorded for {repo_full_name}#{issue_number}")
        validator = build_validator(timeout=timeout)
        workspace = workspace_path_for(workspace_root, repo_full_name, issue_number)
        return validator.validate(
            repo_full_name,
            issue_number,
            patch.patch_sha256,
            patch.diff,
            workspace,
        )
    finally:
        conn.close()


@dataclass
class PublishContext:
    """Everything a publish needs, built once for the page to show."""

    db_path: str
    repo_full_name: str
    issue_number: int
    target_branch: str
    patch_sha256: str
    issue_title: str
    task_state: str


def publish_context(
    mode: ModeInfo,
    repo_full_name: str,
    issue_number: int,
    *,
    target_branch: str = "main",
    db_path: str | Path = DEFAULT_DB_PATH,
) -> PublishContext:
    """Load the state the publish page needs. Local mode only."""
    require_local(mode, "reading the local database")

    from forgeflow import storage

    conn = open_local_db(mode, db_path)
    try:
        issue = storage.get_issue(conn, repo_full_name, issue_number)
        if issue is None:
            raise ValueError(f"{repo_full_name}#{issue_number} is not in the database")
        patch = storage.latest_patch(conn, repo_full_name, issue_number)
        if patch is None:
            raise ValueError(f"no patch recorded for {repo_full_name}#{issue_number}")
        return PublishContext(
            db_path=str(db_path),
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            target_branch=target_branch,
            patch_sha256=patch.patch_sha256,
            issue_title=issue.title,
            task_state=issue.task_state.value,
        )
    finally:
        conn.close()


def apply_approval(
    mode: ModeInfo,
    repo_full_name: str,
    issue_number: int,
    action: str,
    *,
    reason: str = "",
    target_branch: str = "",
    db_path: str | Path = DEFAULT_DB_PATH,
) -> tuple[str, str]:
    """Record an approve / request-changes / reject / discard decision.

    Local mode only. Delegates to :func:`forgeflow.approval.apply_action`, so the
    patch-hash binding and the validation gate are the real ones.
    """
    require_local(mode, "recording an approval")

    from forgeflow import approval
    from forgeflow.approval import ApprovalAction

    conn = open_local_db(mode, db_path)
    try:
        decision, state = approval.apply_action(
            conn,
            repo_full_name,
            issue_number,
            ApprovalAction(action),
            actor="web-operator",
            reason=reason,
            target_branch=target_branch,
        )
        return decision.action.value, state.value
    finally:
        conn.close()


def dry_run_publish(
    mode: ModeInfo,
    repo_full_name: str,
    issue_number: int,
    *,
    target_branch: str = "main",
    db_path: str | Path = DEFAULT_DB_PATH,
) -> str:
    """Build the full publish plan without writing. Local mode only.

    Returns the rendered plan. A dry run is always safe: it records the attempt
    and sends nothing.
    """
    require_local(mode, "planning a publish")

    from forgeflow.models import PublishMode
    from forgeflow.publisher import plan_publish

    conn = open_local_db(mode, db_path)
    try:
        plan = plan_publish(
            conn,
            repo_full_name,
            issue_number,
            target_branch=target_branch,
            mode=PublishMode.DRY_RUN,
        )
        return plan.render()
    finally:
        conn.close()


def real_publish(
    mode: ModeInfo,
    repo_full_name: str,
    issue_number: int,
    *,
    target_branch: str = "main",
    expected_patch_sha256: str,
    confirmation: str,
    db_path: str | Path = DEFAULT_DB_PATH,
):
    """Publish a draft PR for real. Local mode only.

    Three conditions beyond the publisher's own checks, all enforced here:

    * the mode must be local;
    * the visitor must type the exact confirmation string, which names the repo,
      branch, and patch hash, so a real write cannot be a stray click;
    * the recorded patch hash must still match what was confirmed, so a patch
      that changed since the dry run is refused.

    The publisher still runs every pre-publish check, the allowlist, the AI-policy
    block, and opens a DRAFT PR only. This function never merges and never force
    pushes.
    """
    require_local(mode, "publishing")

    from forgeflow import storage
    from forgeflow.models import PublishMode
    from forgeflow.publish_token import read_publish_token
    from forgeflow.publisher import GitHubWriteClient, publish

    expected = confirmation_string(repo_full_name, target_branch, expected_patch_sha256)
    if confirmation.strip() != expected:
        raise ValueError("the typed confirmation does not match. Type it exactly:\n" + expected)

    conn = open_local_db(mode, db_path)
    try:
        patch = storage.latest_patch(conn, repo_full_name, issue_number)
        if patch is None:
            raise ValueError(f"no patch recorded for {repo_full_name}#{issue_number}")
        if patch.patch_sha256 != expected_patch_sha256:
            raise ValueError(
                "the recorded patch changed since you confirmed it; a fresh "
                "approval and confirmation are required."
            )

        write_token = read_publish_token()
        read_token = _read_token()
        write_client = GitHubWriteClient(write_token)
        read_client = _read_github_client(read_token)
        try:
            return publish(
                conn,
                repo_full_name,
                issue_number,
                target_branch=target_branch,
                mode=PublishMode.REAL,
                client=write_client,
                read_client=read_client,
            )
        finally:
            write_client.close()
            if read_client is not None:
                read_client.close()
    finally:
        conn.close()


def confirmation_string(repo_full_name: str, target_branch: str, patch_sha256: str) -> str:
    """The exact text a visitor must type to authorize a real publish."""
    return f"PUBLISH {repo_full_name} {target_branch} {patch_sha256[:12]}"


def _read_token() -> str | None:
    from forgeflow.config import load_settings

    settings = load_settings()
    token = settings.github_token
    return token.get_secret_value() if token is not None else None


def _read_github_client(token: str | None):
    if not token:
        return None
    from forgeflow.github_client import GitHubClient

    return GitHubClient(token)


def list_tasks(
    mode: ModeInfo,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    limit: int = 50,
) -> list[dict[str, str]]:
    """List stored tasks for the approval page. Local mode only."""
    require_local(mode, "reading the local database")

    from forgeflow import storage

    conn = open_local_db(mode, db_path)
    try:
        rows: list[dict[str, str]] = []
        for issue in storage.list_issues(conn)[:limit]:
            rows.append(
                {
                    "repo_full_name": issue.repo_full_name,
                    "number": str(issue.number),
                    "title": issue.title,
                    "task_state": issue.task_state.value,
                }
            )
        return rows
    finally:
        conn.close()


def task_state_for(
    mode: ModeInfo,
    repo_full_name: str,
    issue_number: int,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> TaskState | None:
    """Return a task's current state, or None. Local mode only."""
    require_local(mode, "reading the local database")

    from forgeflow import storage

    conn = open_local_db(mode, db_path)
    try:
        issue = storage.get_issue(conn, repo_full_name, issue_number)
        return issue.task_state if issue is not None else None
    finally:
        conn.close()
