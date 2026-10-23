"""Controlled publishing of a reviewed patch as a draft PR (SPEC sections 8 and 10).

This is the only module in ForgeFlow that performs an external write. Everything
about it is built to make an unintended write hard:

* The publishing credential is read through :mod:`forgeflow.publish_token`, which
  no discovery, analysis, or coding module may import.
* Dry-run is the default. ``PublishMode.REAL`` must be requested explicitly.
* A real write needs six independent checks to pass, in order. Any mismatch
  refuses and the caller must obtain fresh approval.
* Only draft PRs are created. There is no code path that merges, force-pushes,
  comments on an issue, or edits an issue.
* The destination repository must be on a hard-coded allowlist that contains
  only the sandbox repository. Third-party repositories are refused in code.
* Publishing the same patch twice is refused by the idempotence check, not by
  hoping the caller does not retry.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from forgeflow import approval as approval_module
from forgeflow import audit, storage
from forgeflow.models import (
    PublicationRecord,
    PublishMode,
    TaskState,
)
from forgeflow.patch import compute_patch_sha256
from forgeflow.publish_token import PublishTokenError, publish_token_present, read_publish_token


def _safe_message(response: object) -> str:
    """Extract GitHub's error message without echoing anything sensitive."""
    try:
        payload = response.json()  # type: ignore[attr-defined]
    except Exception:
        return "unparseable response body"
    if isinstance(payload, dict) and "message" in payload:
        return str(payload["message"])
    return "no message field in response"


def _default_http_client(timeout: float) -> object:
    """Build the HTTP client lazily so importing this module needs no network."""
    import httpx

    return httpx.Client(timeout=timeout)


# Repositories where a real write is permitted. SPEC section 8 requires a
# hard-coded allowlist, so this is a literal in the source, not configuration a
# running process could rewrite. Adding a repository is a code change that a
# human reviews. The sandbox repository is the only entry.
PUBLISH_ALLOWLIST: frozenset[str] = frozenset(
    {
        "manyashre1306-glitch/forgeflow-sandbox",
    }
)

GITHUB_API = "https://api.github.com"
DEFAULT_TIMEOUT_SECONDS = 30.0


class PublisherError(RuntimeError):
    """Raised when a publish cannot proceed. Never contains a token."""


class RefusedByPolicyError(PublisherError):
    """Raised when a check refuses the publish and fresh approval is needed."""


@dataclass
class PublishPlan:
    """Exactly what a publish would write, for review before it happens."""

    repo_full_name: str
    issue_number: int
    patch_sha256: str
    target_branch: str
    head_branch: str
    title: str
    body: str
    draft: bool = True
    mode: PublishMode = PublishMode.DRY_RUN
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    refused: bool = False
    refusal_reason: str = ""

    @property
    def allowed(self) -> bool:
        return not self.refused

    def render(self) -> str:
        """A human-readable summary of the plan. Safe to print."""
        lines = [
            f"mode:          {self.mode.value}",
            f"repository:    {self.repo_full_name}",
            f"issue:         #{self.issue_number}",
            f"patch sha256:  {self.patch_sha256}",
            f"target branch: {self.target_branch}",
            f"head branch:   {self.head_branch}",
            f"draft:         {self.draft}",
            f"title:         {self.title}",
            "",
            "checks:",
        ]
        for name, ok, detail in self.checks:
            mark = "PASS" if ok else "FAIL"
            lines.append(f"  [{mark}] {name}: {detail}")
        if self.refused:
            lines.append("")
            lines.append(f"REFUSED: {self.refusal_reason}")
        lines.append("")
        lines.append("body:")
        lines.append(self.body)
        return "\n".join(lines)


def is_repo_allowed(repo_full_name: str) -> bool:
    """Whether this repository may receive a real write."""
    return repo_full_name.strip().lower() in {r.lower() for r in PUBLISH_ALLOWLIST}


def head_branch_for(issue_number: int, patch_sha256: str) -> str:
    """A deterministic head branch name, so a retry targets the same branch."""
    return f"forgeflow/issue-{issue_number}-{patch_sha256[:12]}"


def build_pr_body(
    *,
    repo_full_name: str,
    issue_number: int,
    patch_sha256: str,
    diff: str,
    analysis_summary: str = "",
    test_summary: str = "",
    limitations: list[str] | None = None,
) -> str:
    """Compose the PR body from recorded facts.

    Every line here comes from stored data. Nothing is invented: if a test
    summary is absent, the body says so rather than claiming tests passed.
    """
    parts = [
        f"Fixes #{issue_number}",
        "",
        "## Summary",
        "",
        analysis_summary.strip() or "_No analysis summary was recorded for this task._",
        "",
        "## Validation",
        "",
        test_summary.strip() or "_No test evidence was recorded for this patch._",
        "",
        "## Patch",
        "",
        f"- Patch SHA-256: `{patch_sha256}`",
    ]
    if diff.strip():
        additions = sum(
            1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")
        )
        deletions = sum(
            1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---")
        )
        parts.append(f"- Diff: +{additions} / -{deletions}")
    parts.append("")
    parts.append("## Limitations")
    parts.append("")
    if limitations:
        parts.extend(f"- {item}" for item in limitations)
    else:
        parts.append("- None recorded.")
    parts.extend(
        [
            "",
            "---",
            "",
            "_This draft PR was opened by ForgeFlow and is not auto-merged. "
            "It awaits human review._",
        ]
    )
    return "\n".join(parts)


def _git_state(
    client: GitHubWriteClient,
    repo_full_name: str,
    target_branch: str,
) -> tuple[str, bool]:
    """Return (current target branch head sha, whether the repo is writable).

    Read-only lookups. Used to prove the target branch exists and to record
    which commit the patch was based on.
    """
    branch = client.get_branch(repo_full_name, target_branch)
    sha = str(branch.get("commit", {}).get("sha", ""))
    if not sha:
        raise PublisherError(
            f"target branch {target_branch!r} in {repo_full_name} has no commit; "
            "cannot determine the base state"
        )
    return sha, True


def plan_publish(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
    *,
    target_branch: str,
    mode: PublishMode = PublishMode.DRY_RUN,
    client: GitHubWriteClient | None = None,
    actor: str = "operator",
) -> PublishPlan:
    """Run every pre-publish check and return what would be written.

    Performs no write. The six checks SPEC section 8 lists are evaluated in
    order and recorded on the plan, so a refusal names the exact check that
    failed. ``mode`` only labels the plan; the caller decides whether to act.
    """
    issue = storage.get_issue(conn, repo_full_name, issue_number)
    if issue is None:
        raise PublisherError(f"{repo_full_name}#{issue_number} is not in the database")

    patch = storage.latest_patch(conn, repo_full_name, issue_number)
    if patch is None:
        raise PublisherError(
            f"no patch recorded for {repo_full_name}#{issue_number}; nothing to publish"
        )

    head_branch = head_branch_for(issue_number, patch.patch_sha256)
    title = f"{issue.title[:60]}" if issue.title else f"Fix #{issue_number}"

    plan = PublishPlan(
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        patch_sha256=patch.patch_sha256,
        target_branch=target_branch,
        head_branch=head_branch,
        title=title,
        body="",
        mode=mode,
    )

    def check(name: str, ok: bool, detail: str) -> bool:
        plan.checks.append((name, ok, detail))
        return ok

    # Check 1: the repository is on the hard-coded allowlist.
    if not check(
        "allowlist",
        is_repo_allowed(repo_full_name),
        f"{repo_full_name} is "
        f"{'allowed' if is_repo_allowed(repo_full_name) else 'NOT on the publish allowlist'}",
    ):
        plan.refused = True
        plan.refusal_reason = (
            f"{repo_full_name} is not on the publish allowlist. ForgeFlow only "
            f"writes to repositories listed in PUBLISH_ALLOWLIST. Adding one is a "
            f"code change."
        )
        return plan

    # Check 2: the task is in the state a human approval leaves it in.
    if not check(
        "task state",
        issue.task_state is TaskState.AWAITING_PUBLISH_APPROVAL,
        f"task is at '{issue.task_state.value}'",
    ):
        plan.refused = True
        plan.refusal_reason = (
            f"task {repo_full_name}#{issue_number} is at '{issue.task_state.value}', "
            f"expected 'awaiting_publish_approval'. Approve the patch first."
        )
        return plan

    # Check 3: an explicit live approval exists.
    live = approval_module.live_approval(conn, repo_full_name, issue_number)
    if not check(
        "explicit approval",
        live is not None and live.action.value == "approve",
        f"live decision is '{live.action.value}'" if live else "no live decision",
    ):
        plan.refused = True
        plan.refusal_reason = (
            f"no live approval for {repo_full_name}#{issue_number}. "
            "Run 'forgeflow approve' after reviewing the bundle."
        )
        return plan

    # Check 4: the approval is bound to this exact patch, repository, and branch.
    ok, reason = approval_module.check_approval(
        conn, repo_full_name, issue_number, diff=patch.diff, target_branch=target_branch
    )
    if not check("approval binding", ok, reason or "approval matches the current patch"):
        plan.refused = True
        plan.refusal_reason = reason
        return plan

    # Check 5: the stored patch hash equals the hash of the current diff bytes.
    actual = compute_patch_sha256(patch.diff)
    if not check(
        "patch hash",
        actual == patch.patch_sha256,
        "stored hash matches the diff"
        if actual == patch.patch_sha256
        else "stored hash differs from the diff",
    ):
        plan.refused = True
        plan.refusal_reason = (
            "the stored patch hash does not match the current diff bytes; the patch "
            "changed after approval. Fresh approval is required."
        )
        return plan

    # Check 6: destination repository, target branch, and permission scope.
    if not is_repo_allowed(repo_full_name):
        plan.refused = True
        plan.refusal_reason = f"{repo_full_name} is not permitted"
        return plan

    if client is not None:
        try:
            base_sha, _ = _git_state(client, repo_full_name, target_branch)
        except PublisherError as exc:
            check("target branch", False, str(exc))
            plan.refused = True
            plan.refusal_reason = str(exc)
            return plan
        check("target branch", True, f"{target_branch} at {base_sha[:12]}")

        scope_ok = client.has_write_scope(repo_full_name)
        if not check(
            "permission scope",
            scope_ok,
            "token reports write permission" if scope_ok else "token lacks write permission",
        ):
            plan.refused = True
            plan.refusal_reason = (
                f"the publishing token does not have write permission on {repo_full_name}"
            )
            return plan
    else:
        check("target branch", True, f"{target_branch} (not verified: no client)")
        check("permission scope", True, "not verified: no client")

    # Idempotence: the same patch must not be published twice.
    existing = storage.find_publication_for_patch(
        conn, repo_full_name, issue_number, patch.patch_sha256
    )
    if not check(
        "idempotence",
        existing is None,
        f"already published as {existing.pr_url}" if existing else "no prior publication",
    ):
        plan.refused = True
        plan.refusal_reason = (
            f"this exact patch was already published as {existing.pr_url}. "
            "Publishing again would create a duplicate PR."
        )
        return plan

    plan.body = build_pr_body(
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        patch_sha256=patch.patch_sha256,
        diff=patch.diff,
        limitations=[],
    )
    return plan


def publish(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
    *,
    target_branch: str,
    mode: PublishMode = PublishMode.DRY_RUN,
    client: GitHubWriteClient | None = None,
    actor: str = "operator",
) -> PublicationRecord:
    """Publish the reviewed patch as a draft PR, or record a dry run.

    Dry-run is the default. A real write requires ``mode=PublishMode.REAL`` and
    every check in :func:`plan_publish` to pass. Returns the recorded attempt
    either way, so a dry run is visible in the log.
    """
    plan = plan_publish(
        conn,
        repo_full_name,
        issue_number,
        target_branch=target_branch,
        mode=mode,
        client=client,
        actor=actor,
    )

    record = PublicationRecord(
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        patch_sha256=plan.patch_sha256,
        target_branch=target_branch,
        head_branch=plan.head_branch,
        mode=mode,
        published=False,
        draft=True,
        title=plan.title,
        body=plan.body,
        actor=actor,
    )

    if plan.refused:
        record.refusal_reason = plan.refusal_reason
        storage.record_publication(conn, record)
        audit.record(
            conn,
            "publish.refused",
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            severity="warning",
            actor=actor,
            detail=plan.refusal_reason,
            payload={"patch_sha256": plan.patch_sha256, "mode": mode.value},
        )
        raise RefusedByPolicyError(plan.refusal_reason)

    if mode is PublishMode.DRY_RUN:
        record.refusal_reason = ""
        storage.record_publication(conn, record)
        audit.record(
            conn,
            "publish.dry_run",
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            actor=actor,
            detail=f"would open a draft PR to {target_branch}",
            payload={"patch_sha256": plan.patch_sha256, "head_branch": plan.head_branch},
        )
        return record

    # Real write from here on.
    if client is None:
        raise PublisherError("a real publish requires a GitHub write client; none was supplied")

    if not publish_token_present():
        raise PublishTokenError("FORGEFLOW_PUBLISH_TOKEN is not set; a real publish cannot proceed")

    result = client.create_draft_pull_request(
        repo_full_name=repo_full_name,
        head_branch=plan.head_branch,
        base_branch=target_branch,
        title=plan.title,
        body=plan.body,
        patch_sha256=plan.patch_sha256,
    )

    record.published = True
    record.pr_number = result.get("number")
    record.pr_url = str(result.get("html_url", ""))
    record.draft = bool(result.get("draft", True))
    storage.record_publication(conn, record)

    storage.transition_task_state(
        conn,
        repo_full_name,
        issue_number,
        TaskState.PUBLISHED,
        actor=actor,
        reason=f"draft PR {record.pr_url} opened",
    )
    audit.record(
        conn,
        "publish.created",
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        actor=actor,
        detail=f"draft PR opened: {record.pr_url}",
        payload={
            "patch_sha256": plan.patch_sha256,
            "pr_number": record.pr_number,
            "pr_url": record.pr_url,
        },
    )
    return record


class GitHubWriteClient:
    """The narrowest possible GitHub write surface (SPEC section 8).

    Exactly one write method exists: create a draft pull request. There is no
    merge, no force push, no issue comment, no issue edit, and no branch delete.
    That is deliberate: a capability that does not exist cannot be invoked by a
    bug, a prompt injection, or a misread configuration.

    Reads (branch state, permission scope) go through GET, so the preflight
    checks can verify reality rather than trusting the database alone.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        client: object = None,
        base_url: str = GITHUB_API,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        # The token is read here, through the isolated module, and never stored
        # in a printable form.
        self._token = token if token is not None else read_publish_token()
        self._owns_client = client is None
        self._client = client if client is not None else _default_http_client(timeout)
        self._base_url = base_url.rstrip("/")

    def close(self) -> None:
        if self._owns_client:
            close = getattr(self._client, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> GitHubWriteClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get(self, path: str) -> dict:
        url = f"{self._base_url}{path}"
        response = self._client.get(url, headers=self._headers())  # type: ignore[attr-defined]
        if response.status_code == 404:
            raise PublisherError(f"not found: {path}")
        if response.status_code >= 400:
            # The body may echo the request; the token is never in it.
            raise PublisherError(
                f"GitHub returned {response.status_code} for {path}: {_safe_message(response)}"
            )
        return response.json()

    def get_branch(self, repo_full_name: str, branch: str) -> dict:
        """Read one branch. Used to confirm the target branch exists."""
        return self._get(f"/repos/{repo_full_name}/branches/{branch}")

    def has_write_scope(self, repo_full_name: str) -> bool:
        """Whether the token reports push permission on this repository.

        Reads the ``permissions.push`` field the API returns for the
        authenticated user. Absent or false is treated as no permission, so a
        malformed response fails closed.
        """
        try:
            payload = self._get(f"/repos/{repo_full_name}")
        except PublisherError:
            return False
        permissions = payload.get("permissions") if isinstance(payload, dict) else None
        if not isinstance(permissions, dict):
            return False
        return bool(permissions.get("push"))

    def create_draft_pull_request(
        self,
        *,
        repo_full_name: str,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
        patch_sha256: str = "",
    ) -> dict:
        """Create a DRAFT pull request. The only write this class can perform.

        ``draft`` is hard-coded True. There is no parameter to change it, so a
        caller cannot accidentally open a ready-for-review PR.
        """
        url = f"{self._base_url}/repos/{repo_full_name}/pulls"
        payload = {
            "title": title,
            "body": body,
            "head": head_branch,
            "base": base_branch,
            "draft": True,
        }
        response = self._client.post(url, json=payload, headers=self._headers())  # type: ignore[attr-defined]
        if response.status_code >= 400:
            raise PublisherError(
                f"GitHub returned {response.status_code} creating the draft PR on "
                f"{repo_full_name}: {_safe_message(response)}"
            )
        result = response.json()
        if not isinstance(result, dict):
            raise PublisherError("GitHub returned an unexpected response body")
        return result
