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
from forgeflow.publish_token import read_publish_token


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
    # (name, status, detail) where status is PASS, FAIL, or UNVERIFIED. The
    # third value matters: a dry run with no client cannot confirm the target
    # branch or the token scope, and showing those as PASS would tell a human
    # reviewer something ForgeFlow does not know.
    checks: list[tuple[str, str, str]] = field(default_factory=list)
    refused: bool = False
    refusal_reason: str = ""
    unverified: list[str] = field(default_factory=list)

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
        for name, status, detail in self.checks:
            lines.append(f"  [{status}] {name}: {detail}")
        if self.unverified:
            lines.append("")
            lines.append(
                "WARNING: these checks could not be verified in this run and are "
                f"NOT a pass: {', '.join(self.unverified)}"
            )
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


# Every branch ForgeFlow creates must start with this prefix. Enforced by
# require_creatable_branch, not by convention.
BRANCH_PREFIX = "forgeflow/"

# Branches ForgeFlow must never write to, whatever the caller asks.
PROTECTED_BRANCHES: frozenset[str] = frozenset({"main", "master", "develop", "trunk"})


class BranchPolicyError(PublisherError):
    """Raised when a branch name is not permitted. Never a token leak."""


def head_branch_for(issue_number: int, patch_sha256: str) -> str:
    """A deterministic head branch name, so a retry targets the same branch."""
    return f"{BRANCH_PREFIX}issue-{issue_number}-{patch_sha256[:12]}"


def require_creatable_branch(branch: str, *, existing: list[str] | None = None) -> None:
    """Raise unless ``branch`` is a new branch ForgeFlow is allowed to create.

    Four rules, all mandatory:

    * the name must start with ``forgeflow/``, so ForgeFlow never creates a
      branch that looks like a maintainer's;
    * it must not be a protected branch such as ``main``;
    * it must not already exist, because ForgeFlow only ever creates new
      branches and never updates an existing one;
    * it must be non-empty after the prefix.

    ``existing`` is the list of branch names already on the remote. When it is
    None the existence rule cannot be checked and the caller must verify
    separately; the create path always passes a real list.
    """
    if not branch:
        raise BranchPolicyError("branch name is empty")
    if not branch.startswith(BRANCH_PREFIX):
        raise BranchPolicyError(
            f"refusing to create branch {branch!r}: ForgeFlow may only create branches "
            f"starting with {BRANCH_PREFIX!r}"
        )
    if branch.strip() == BRANCH_PREFIX:
        raise BranchPolicyError(f"refusing to create branch {branch!r}: no name after the prefix")
    if branch in PROTECTED_BRANCHES:
        raise BranchPolicyError(
            f"refusing to write to protected branch {branch!r}: ForgeFlow never writes "
            "to an existing branch"
        )
    if existing is not None and branch in existing:
        raise BranchPolicyError(
            f"refusing to create branch {branch!r}: it already exists. ForgeFlow only "
            "creates new branches and never updates an existing one."
        )


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
    # Check 1, first: the repository must be on the hard-coded allowlist. This
    # runs before any database lookup so a third-party target is refused before
    # ForgeFlow does any work at all.
    if not is_repo_allowed(repo_full_name):
        plan = PublishPlan(
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            patch_sha256="",
            target_branch=target_branch,
            head_branch="",
            title="",
            body="",
            mode=mode,
        )
        plan.checks.append(
            ("allowlist", "FAIL", f"{repo_full_name} is NOT on the publish allowlist")
        )
        plan.refused = True
        plan.refusal_reason = (
            f"{repo_full_name} is not on the publish allowlist. ForgeFlow only writes "
            f"to repositories listed in PUBLISH_ALLOWLIST. Adding one is a code change."
        )
        return plan

    issue = storage.get_issue(conn, repo_full_name, issue_number)
    if issue is None:
        raise PublisherError(f"{repo_full_name}#{issue_number} is not in the database")

    patch = storage.latest_patch(conn, repo_full_name, issue_number)
    if patch is None:
        raise PublisherError(
            f"no patch recorded for {repo_full_name}#{issue_number}; nothing to publish"
        )

    # Idempotence is checked early, before the task-state check, because a retry
    # of an already-published patch is a duplicate no matter what state the task
    # moved to afterwards. Reporting it as a state error would mislead.
    existing_publication = storage.find_publication_for_patch(
        conn, repo_full_name, issue_number, patch.patch_sha256
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
        plan.checks.append((name, "PASS" if ok else "FAIL", detail))
        return ok

    def unverified(name: str, detail: str) -> None:
        plan.checks.append((name, "UNVERIFIED", detail))
        plan.unverified.append(name)

    check("allowlist", True, f"{repo_full_name} is on the publish allowlist")

    # Idempotence, before any state or approval check. Publishing the same patch
    # twice must be reported as a duplicate rather than as a stale-state error,
    # because after a successful publish the task has legitimately moved on.
    if not check(
        "idempotence",
        existing_publication is None,
        f"already published as {existing_publication.pr_url}"
        if existing_publication
        else "no prior publication",
    ):
        plan.refused = True
        plan.refusal_reason = (
            f"this exact patch was already published as {existing_publication.pr_url}. "
            "Publishing again would create a duplicate PR."
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
        unverified("target branch", f"{target_branch} could not be checked (dry run, no client)")
        unverified("permission scope", "token scope could not be checked (dry run, no client)")

    plan.body = build_pr_body(
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        patch_sha256=patch.patch_sha256,
        diff=patch.diff,
        analysis_summary=_analysis_summary(conn, repo_full_name, issue_number),
        test_summary=_test_summary(conn, repo_full_name, issue_number, patch.patch_sha256),
        limitations=_limitations(conn, repo_full_name, issue_number, patch.patch_sha256),
    )
    return plan


def _analysis_summary(conn: sqlite3.Connection, repo_full_name: str, issue_number: int) -> str:
    """The stored analysis summary, or empty when none was recorded."""
    record = storage.latest_analysis(conn, repo_full_name, issue_number)
    if record is None:
        return ""
    for attribute in ("summary", "approach", "plan"):
        value = getattr(record, attribute, "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _test_summary(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
    patch_sha256: str,
) -> str:
    """A factual summary of the validation checks recorded for this patch.

    Reports exactly what ran and what it returned. A skipped check is reported
    as skipped, never as a pass, because the PR body is read by a maintainer who
    will trust it.
    """
    rows = storage.list_validation_results(
        conn, repo_full_name, issue_number, patch_sha256=patch_sha256
    )
    if not rows:
        return ""
    lines: list[str] = []
    for row in rows:
        name = str(row.get("name") or row.get("kind") or "check")
        status = str(row.get("status") or "unknown")
        exit_code = row.get("exit_code")
        suffix = f" (exit {exit_code})" if exit_code is not None else ""
        lines.append(f"- `{name}`: {status}{suffix}")
    return "\n".join(lines)


def _limitations(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
    patch_sha256: str,
) -> list[str]:
    """Environment limitations and skipped checks recorded for this patch."""
    rows = storage.list_validation_results(
        conn, repo_full_name, issue_number, patch_sha256=patch_sha256
    )
    found: list[str] = []
    for row in rows:
        reason = row.get("skip_reason")
        if isinstance(reason, str) and reason.strip():
            found.append(reason.strip())
    return found


def _commit_message(issue_number: int, title: str) -> str:
    """The commit message for the created branch."""
    return f"{title}\n\nFixes #{issue_number}\n\nCreated by ForgeFlow."


def _files_for_patch(
    client: GitHubWriteClient,
    repo_full_name: str,
    base_branch: str,
    diff: str,
) -> dict[str, str]:
    """Turn a stored diff into the full file contents to commit.

    Reads each touched file from the base branch and applies the diff strictly.
    Raises if the diff does not apply, so a branch is never created carrying
    content that does not match the approved patch.
    """
    from forgeflow.diff_apply import DiffApplyError, apply_diff, parse_unified_diff

    try:
        changes = parse_unified_diff(diff)
    except DiffApplyError as exc:
        raise PublisherError(f"the stored patch could not be parsed: {exc}") from exc
    if not changes:
        raise PublisherError("the stored patch contains no file changes")

    base_files: dict[str, str] = {}
    for change in changes:
        if change.is_new:
            continue
        try:
            base_files[change.old_path] = client.get_file_content(
                repo_full_name, change.old_path, base_branch
            )
        except PublisherError as exc:
            raise PublisherError(
                f"the patch modifies {change.old_path!r}, which could not be read "
                f"from {base_branch}: {exc}"
            ) from exc

    try:
        return apply_diff(diff, base_files)
    except DiffApplyError as exc:
        raise PublisherError(
            f"the stored patch does not apply cleanly to {base_branch}: {exc}. "
            "Refusing to create a branch with content that does not match the "
            "approved patch."
        ) from exc


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

    # Real write, step 1 of 2: create the head branch carrying the patch. The
    # branch policy is re-checked here against the repository's real branch
    # list, so an existing branch is refused even if the database forgot.
    stored_patch = storage.latest_patch(conn, repo_full_name, issue_number)
    if stored_patch is None:
        raise PublisherError("the patch disappeared between planning and publishing")
    files = _files_for_patch(client, repo_full_name, target_branch, stored_patch.diff)
    existing = client.list_branches(repo_full_name)
    client.push_patch_branch(
        repo_full_name=repo_full_name,
        branch=plan.head_branch,
        base_branch=target_branch,
        files=files,
        commit_message=_commit_message(issue_number, plan.title),
        existing_branches=existing,
    )
    audit.record(
        conn,
        "publish.branch_created",
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        actor=actor,
        detail=f"created branch {plan.head_branch}",
        payload={"branch": plan.head_branch, "files": sorted(files)},
    )

    # Real write, step 2 of 2: open the DRAFT PR from that branch.
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

    def get_ref_sha(self, repo_full_name: str, branch: str) -> str:
        """Read the commit SHA a branch points at, or raise if it is absent."""
        payload = self._get(f"/repos/{repo_full_name}/git/ref/heads/{branch}")
        obj = payload.get("object") if isinstance(payload, dict) else None
        if not isinstance(obj, dict) or not obj.get("sha"):
            raise PublisherError(f"branch {branch!r} has no commit")
        return str(obj["sha"])

    def list_branches(self, repo_full_name: str) -> list[str]:
        """Every branch name on the repository. Used by the existence check."""
        payload = self._get(f"/repos/{repo_full_name}/branches?per_page=100")
        if not isinstance(payload, list):
            raise PublisherError("GitHub returned an unexpected branches response")
        return [str(item.get("name", "")) for item in payload if isinstance(item, dict)]

    def get_file_content(self, repo_full_name: str, path: str, ref: str) -> str:
        """Read one file's text at a ref. Raises if the file is absent."""
        payload = self._get(f"/repos/{repo_full_name}/contents/{path}?ref={ref}")
        if not isinstance(payload, dict):
            raise PublisherError(f"unexpected content response for {path!r}")
        import base64

        content = payload.get("content")
        if not isinstance(content, str):
            raise PublisherError(f"{path!r} has no readable content")
        try:
            decoded = base64.b64decode(content, validate=False)
        except Exception as exc:
            raise PublisherError(f"{path!r} content is not valid base64") from exc
        return decoded.decode("utf-8", errors="replace")

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self._base_url}{path}"
        response = self._client.post(url, json=payload, headers=self._headers())  # type: ignore[attr-defined]
        if response.status_code >= 400:
            raise PublisherError(
                f"GitHub returned {response.status_code} for {path}: {_safe_message(response)}"
            )
        result = response.json()
        if not isinstance(result, dict):
            raise PublisherError("GitHub returned an unexpected response body")
        return result

    def push_patch_branch(
        self,
        *,
        repo_full_name: str,
        branch: str,
        base_branch: str,
        files: dict[str, str],
        commit_message: str,
        existing_branches: list[str],
    ) -> dict:
        """Create a NEW branch carrying ``files``, via the GitHub object API.

        Refuses in five cases before any write, matching the approved limits:

        * the branch name is not permitted (prefix, protected, or empty);
        * the branch already exists;
        * the base branch does not exist;
        * there are no files to write.

        Uses blob, tree, commit, then ref, which creates a branch. There is no
        update path: this method can only create. No local git runs, and a force
        update is impossible because a new ref cannot be force-updated.
        """
        require_creatable_branch(branch, existing=existing_branches)
        if not files:
            raise PublisherError("no files to write to the branch")
        if branch == base_branch:
            raise PublisherError(f"refusing to write the head branch {branch!r} onto itself")

        base_sha = self.get_ref_sha(repo_full_name, base_branch)

        tree_items: list[dict] = []
        for path, content in files.items():
            blob = self._post(
                f"/repos/{repo_full_name}/git/blobs",
                {"content": content, "encoding": "utf-8"},
            )
            blob_sha = blob.get("sha")
            if not blob_sha:
                raise PublisherError(f"GitHub did not return a blob sha for {path!r}")
            tree_items.append({"path": path, "mode": "100644", "type": "blob", "sha": blob_sha})

        tree = self._post(
            f"/repos/{repo_full_name}/git/trees",
            {"base_tree": base_sha, "tree": tree_items},
        )
        tree_sha = tree.get("sha")
        if not tree_sha:
            raise PublisherError("GitHub did not return a tree sha")

        commit = self._post(
            f"/repos/{repo_full_name}/git/commits",
            {"message": commit_message, "tree": tree_sha, "parents": [base_sha]},
        )
        commit_sha = commit.get("sha")
        if not commit_sha:
            raise PublisherError("GitHub did not return a commit sha")

        ref = self._post(
            f"/repos/{repo_full_name}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": commit_sha},
        )
        return ref

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
