"""Publisher tests against a mocked GitHub (SPEC sections 8 and 10).

Every test here uses a fake HTTP client that records each request, so the
assertions can prove not just that publishing failed but that *no write was
attempted*. That distinction is the whole point of a dry-run default.

No test in this file touches the network, and no test performs a real write.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from forgeflow import approval, audit, publisher, storage
from forgeflow.models import (
    ApprovalAction,
    CheckKind,
    CheckStatus,
    Issue,
    PatchRecord,
    PublishMode,
    Repository,
    TaskState,
    ValidationCheck,
    ValidationReport,
)
from forgeflow.patch import compute_patch_sha256
from forgeflow.publisher import (
    GitHubWriteClient,
    PublisherError,
    RefusedByPolicyError,
    is_repo_allowed,
)

# The only repository a real write is permitted to target.
SANDBOX = "manyashre1306-glitch/forgeflow-sandbox"
# A repository that must be refused by the allowlist.
THIRD_PARTY = "some-org/some-repo"
BRANCH = "main"

DIFF = "diff --git a/p.py b/p.py\n--- a/p.py\n+++ b/p.py\n@@ -1 +1,2 @@\n def f():\n+    pass\n"
HASH = compute_patch_sha256(DIFF)


# --------------------------------------------------------------------------
# A fake GitHub that records every request
# --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class FakeHTTP:
    """Records requests. A POST is a write, so tests assert on the POST count."""

    def __init__(
        self,
        *,
        branch_sha: str = "a" * 40,
        write_scope: bool = True,
        pr_response: dict | None = None,
    ) -> None:
        self.branch_sha = branch_sha
        self.write_scope = write_scope
        self.pr_response = pr_response or {
            "number": 42,
            "html_url": f"https://github.com/{SANDBOX}/pull/42",
            "draft": True,
        }
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict]] = []

    def get(self, url: str, headers: dict | None = None, **kwargs: object) -> FakeResponse:
        self.gets.append(url)
        if "/branches/" in url:
            return FakeResponse(200, {"commit": {"sha": self.branch_sha}})
        # The repository endpoint, used for the permission-scope check.
        return FakeResponse(200, {"permissions": {"push": self.write_scope}})

    def post(
        self, url: str, json: dict | None = None, headers: dict | None = None, **kwargs: object
    ) -> FakeResponse:
        self.posts.append((url, json or {}))
        return FakeResponse(201, self.pr_response)

    def close(self) -> None:
        pass


@pytest.fixture
def conn(tmp_path):
    connection = storage.connect(tmp_path / "ff.db")
    storage.init_db(connection)
    yield connection
    connection.close()


def validation_report(repo: str, number: int, digest: str) -> ValidationReport:
    return ValidationReport(
        repo_full_name=repo,
        issue_number=number,
        patch_sha256=digest,
        isolated=True,
        checks=[
            ValidationCheck(
                kind=CheckKind.TESTS,
                name="pytest",
                command=["python", "-m", "pytest"],
                status=CheckStatus.PASSED,
                exit_code=0,
            )
        ],
    )


def seed_to_validating(conn, repo: str = SANDBOX, number: int = 7) -> None:
    storage.upsert_repository(conn, Repository(full_name=repo, stars=10))
    storage.upsert_issue(
        conn, Issue(repo_full_name=repo, number=number, title="Fix a bug", body="b")
    )
    for target in (
        TaskState.SHORTLISTED,
        TaskState.ANALYZED,
        TaskState.AWAITING_PLAN_APPROVAL,
        TaskState.IMPLEMENTING,
        TaskState.VALIDATING,
    ):
        storage.transition_task_state(conn, repo, number, target)


def seed_ready(conn, repo: str = SANDBOX, number: int = 7, diff: str = DIFF) -> str:
    """A task fully approved and ready to publish."""
    digest = compute_patch_sha256(diff)
    seed_to_validating(conn, repo, number)
    storage.record_patch(
        conn,
        PatchRecord(
            repo_full_name=repo,
            issue_number=number,
            provider="mock",
            patch_sha256=digest,
            diff=diff,
        ),
    )
    storage.record_validation_report(conn, validation_report(repo, number, digest))
    approval.apply_action(
        conn, repo, number, ApprovalAction.APPROVE, actor="human", target_branch=BRANCH
    )
    return digest


def make_client(http: FakeHTTP, token: str = "publish-token-value") -> GitHubWriteClient:
    return GitHubWriteClient(token=token, client=http)


# ==========================================================================
# The allowlist
# ==========================================================================


def test_allowlist_contains_only_the_sandbox_repo() -> None:
    assert frozenset({SANDBOX}) == publisher.PUBLISH_ALLOWLIST


def test_sandbox_repo_is_allowed() -> None:
    assert is_repo_allowed(SANDBOX) is True


def test_third_party_repo_is_not_allowed() -> None:
    assert is_repo_allowed(THIRD_PARTY) is False


def test_third_party_repo_is_refused_and_no_write_is_attempted(conn) -> None:
    """A third-party target must be refused by code, not by configuration."""
    digest = compute_patch_sha256(DIFF)
    seed_to_validating(conn, repo=THIRD_PARTY)
    storage.record_patch(
        conn,
        PatchRecord(
            repo_full_name=THIRD_PARTY,
            issue_number=7,
            provider="mock",
            patch_sha256=digest,
            diff=DIFF,
        ),
    )
    storage.record_validation_report(conn, validation_report(THIRD_PARTY, 7, digest))
    approval.apply_action(conn, THIRD_PARTY, 7, ApprovalAction.APPROVE, target_branch=BRANCH)

    http = FakeHTTP()
    with pytest.raises(RefusedByPolicyError, match="allowlist"):
        publisher.publish(
            conn,
            THIRD_PARTY,
            7,
            target_branch=BRANCH,
            mode=PublishMode.REAL,
            client=make_client(http),
        )
    assert http.posts == [], "a refused publish must not reach the network"


def test_allowlist_check_precedes_every_other_check(conn) -> None:
    """A third-party repo is refused even when the task is not otherwise ready."""
    seed_to_validating(conn, repo=THIRD_PARTY)
    http = FakeHTTP()
    with pytest.raises(RefusedByPolicyError, match="allowlist"):
        publisher.publish(
            conn,
            THIRD_PARTY,
            7,
            target_branch=BRANCH,
            mode=PublishMode.REAL,
            client=make_client(http),
        )
    assert http.posts == []


# ==========================================================================
# Dry-run is the default
# ==========================================================================


def test_default_mode_is_dry_run(conn) -> None:
    seed_ready(conn)
    http = FakeHTTP()
    record = publisher.publish(conn, SANDBOX, 7, target_branch=BRANCH, client=make_client(http))
    assert record.mode is PublishMode.DRY_RUN


def test_dry_run_makes_no_write(conn) -> None:
    """The central guarantee: a dry run must not POST anything."""
    seed_ready(conn)
    http = FakeHTTP()
    publisher.publish(conn, SANDBOX, 7, target_branch=BRANCH, client=make_client(http))
    assert http.posts == [], "dry run must not perform any write request"


def test_dry_run_records_the_attempt(conn) -> None:
    seed_ready(conn)
    http = FakeHTTP()
    publisher.publish(conn, SANDBOX, 7, target_branch=BRANCH, client=make_client(http))
    attempts = storage.list_publications(conn, SANDBOX, 7)
    assert len(attempts) == 1
    assert attempts[0].published is False
    assert attempts[0].pr_url == ""


def test_dry_run_does_not_move_the_task_to_published(conn) -> None:
    seed_ready(conn)
    http = FakeHTTP()
    publisher.publish(conn, SANDBOX, 7, target_branch=BRANCH, client=make_client(http))
    assert storage.get_issue(conn, SANDBOX, 7).task_state is TaskState.AWAITING_PUBLISH_APPROVAL


def test_dry_run_writes_an_audit_event(conn) -> None:
    seed_ready(conn)
    http = FakeHTTP()
    publisher.publish(conn, SANDBOX, 7, target_branch=BRANCH, client=make_client(http))
    events = audit.list_events(conn, event="publish.dry_run")
    assert len(events) == 1


def test_plan_render_shows_what_would_be_written(conn) -> None:
    """A human must be able to read the plan before approving a real write."""
    digest = seed_ready(conn)
    plan = publisher.plan_publish(conn, SANDBOX, 7, target_branch=BRANCH)
    rendered = plan.render()
    assert digest in rendered
    assert SANDBOX in rendered
    assert BRANCH in rendered
    assert "draft:         True" in rendered
    # A check that could not be verified must never render as PASS.
    assert "[UNVERIFIED] target branch" in rendered
    assert "[PASS] target branch" not in rendered
    assert "NOT a pass" in rendered


# ==========================================================================
# Every blocking condition
# ==========================================================================


def test_missing_approval_blocks(conn) -> None:
    """A task with no approval must not publish."""
    seed_to_validating(conn)
    storage.transition_task_state(conn, SANDBOX, 7, TaskState.FAILED_VALIDATION)
    storage.record_patch(
        conn,
        PatchRecord(
            repo_full_name=SANDBOX,
            issue_number=7,
            provider="mock",
            patch_sha256=HASH,
            diff=DIFF,
        ),
    )
    http = FakeHTTP()
    with pytest.raises(RefusedByPolicyError):
        publisher.publish(
            conn,
            SANDBOX,
            7,
            target_branch=BRANCH,
            mode=PublishMode.REAL,
            client=make_client(http),
        )
    assert http.posts == []


def test_modified_patch_blocks(conn) -> None:
    """Approval binds to a patch hash; changing the patch invalidates it."""
    digest = seed_ready(conn)
    assert digest == HASH

    # The patch changes after approval.
    changed = DIFF + "+    return None\n"
    storage.record_patch(
        conn,
        PatchRecord(
            repo_full_name=SANDBOX,
            issue_number=7,
            provider="mock",
            patch_sha256=compute_patch_sha256(changed),
            diff=changed,
        ),
    )
    http = FakeHTTP()
    with pytest.raises(RefusedByPolicyError, match="approval"):
        publisher.publish(
            conn,
            SANDBOX,
            7,
            target_branch=BRANCH,
            mode=PublishMode.REAL,
            client=make_client(http),
        )
    assert http.posts == []


def test_wrong_branch_blocks(conn) -> None:
    """An approval for one branch must not authorize another."""
    seed_ready(conn)  # approved for BRANCH
    http = FakeHTTP()
    with pytest.raises(RefusedByPolicyError, match="approval"):
        publisher.publish(
            conn,
            SANDBOX,
            7,
            target_branch="release-2.0",
            mode=PublishMode.REAL,
            client=make_client(http),
        )
    assert http.posts == []


def test_failed_validation_blocks(conn) -> None:
    """A task at failed_validation has no live approval, so it cannot publish."""
    seed_to_validating(conn)
    storage.transition_task_state(conn, SANDBOX, 7, TaskState.FAILED_VALIDATION)
    storage.record_patch(
        conn,
        PatchRecord(
            repo_full_name=SANDBOX,
            issue_number=7,
            provider="mock",
            patch_sha256=HASH,
            diff=DIFF,
        ),
    )
    http = FakeHTTP()
    with pytest.raises(RefusedByPolicyError):
        publisher.publish(
            conn,
            SANDBOX,
            7,
            target_branch=BRANCH,
            mode=PublishMode.REAL,
            client=make_client(http),
        )
    assert http.posts == []


def test_wrong_task_state_blocks(conn) -> None:
    """Even with an approval row, a task not awaiting publish approval blocks."""
    seed_ready(conn)
    # Move back to implementing; the approval is now stale for this state.
    approval.apply_action(conn, SANDBOX, 7, ApprovalAction.REQUEST_CHANGES)
    http = FakeHTTP()
    with pytest.raises(RefusedByPolicyError, match="task"):
        publisher.publish(
            conn,
            SANDBOX,
            7,
            target_branch=BRANCH,
            mode=PublishMode.REAL,
            client=make_client(http),
        )
    assert http.posts == []


def test_missing_write_scope_blocks(conn) -> None:
    """A token without push permission must not be used to publish."""
    seed_ready(conn)
    http = FakeHTTP(write_scope=False)
    with pytest.raises(RefusedByPolicyError, match="write permission"):
        publisher.publish(
            conn,
            SANDBOX,
            7,
            target_branch=BRANCH,
            mode=PublishMode.REAL,
            client=make_client(http),
        )
    assert http.posts == []


def test_missing_target_branch_blocks(conn) -> None:
    """A target branch that does not exist must be refused before writing."""

    class NoBranchHTTP(FakeHTTP):
        def get(self, url, headers=None, **kwargs):
            if "/branches/" in url:
                return FakeResponse(404)
            return super().get(url, headers=headers, **kwargs)

    seed_ready(conn)
    http = NoBranchHTTP()
    with pytest.raises(RefusedByPolicyError):
        publisher.publish(
            conn,
            SANDBOX,
            7,
            target_branch="does-not-exist",
            mode=PublishMode.REAL,
            client=make_client(http),
        )
    assert http.posts == []


# ==========================================================================
# Idempotence
# ==========================================================================


def test_duplicate_publish_is_prevented(conn) -> None:
    """Publishing the same patch twice must not create a second PR."""
    seed_ready(conn)
    http = FakeHTTP()
    first = publisher.publish(
        conn,
        SANDBOX,
        7,
        target_branch=BRANCH,
        mode=PublishMode.REAL,
        client=make_client(http),
    )
    assert first.published is True
    assert len(http.posts) == 1

    # A second attempt with the same patch must be refused.
    with pytest.raises(RefusedByPolicyError, match="already published"):
        publisher.publish(
            conn,
            SANDBOX,
            7,
            target_branch=BRANCH,
            mode=PublishMode.REAL,
            client=make_client(http),
        )
    assert len(http.posts) == 1, "the second attempt must not have written"


def test_idempotence_is_per_patch_not_per_issue(conn) -> None:
    """A new patch for the same issue is a new publish, not a duplicate."""
    seed_ready(conn)
    http = FakeHTTP()
    publisher.publish(
        conn, SANDBOX, 7, target_branch=BRANCH, mode=PublishMode.REAL, client=make_client(http)
    )
    assert len(http.posts) == 1

    # A different patch, freshly approved, is allowed to publish.
    changed = DIFF + "+    return None\n"
    new_hash = compute_patch_sha256(changed)
    storage.record_patch(
        conn,
        PatchRecord(
            repo_full_name=SANDBOX,
            issue_number=7,
            provider="mock",
            patch_sha256=new_hash,
            diff=changed,
        ),
    )
    # A maintainer requests changes, so the task returns to implementation.
    storage.transition_task_state(conn, SANDBOX, 7, TaskState.IMPLEMENTING)
    storage.transition_task_state(conn, SANDBOX, 7, TaskState.VALIDATING)
    storage.record_validation_report(conn, validation_report(SANDBOX, 7, new_hash))
    approval.apply_action(conn, SANDBOX, 7, ApprovalAction.APPROVE, target_branch=BRANCH)

    second = publisher.publish(
        conn, SANDBOX, 7, target_branch=BRANCH, mode=PublishMode.REAL, client=make_client(http)
    )
    assert second.published is True
    assert len(http.posts) == 2


def test_head_branch_is_deterministic(conn) -> None:
    """The head branch is derived from the patch hash, so a retry is stable."""
    first = publisher.head_branch_for(7, HASH)
    second = publisher.head_branch_for(7, HASH)
    assert first == second
    assert HASH[:12] in first


# ==========================================================================
# The one write method
# ==========================================================================


def test_real_publish_creates_a_draft_pr(conn) -> None:
    seed_ready(conn)
    http = FakeHTTP()
    record = publisher.publish(
        conn, SANDBOX, 7, target_branch=BRANCH, mode=PublishMode.REAL, client=make_client(http)
    )
    assert record.published is True
    assert record.pr_number == 42
    assert record.pr_url == f"https://github.com/{SANDBOX}/pull/42"

    url, payload = http.posts[0]
    assert url.endswith(f"/repos/{SANDBOX}/pulls")
    assert payload["draft"] is True
    assert payload["base"] == BRANCH


def test_real_publish_moves_the_task_to_published(conn) -> None:
    seed_ready(conn)
    http = FakeHTTP()
    publisher.publish(
        conn, SANDBOX, 7, target_branch=BRANCH, mode=PublishMode.REAL, client=make_client(http)
    )
    assert storage.get_issue(conn, SANDBOX, 7).task_state is TaskState.PUBLISHED


def test_real_publish_writes_an_audit_event(conn) -> None:
    seed_ready(conn)
    http = FakeHTTP()
    publisher.publish(
        conn, SANDBOX, 7, target_branch=BRANCH, mode=PublishMode.REAL, client=make_client(http)
    )
    events = audit.list_events(conn, event="publish.created")
    assert len(events) == 1
    assert "pull/42" in events[0].detail


def test_write_client_exposes_exactly_one_write_method() -> None:
    """The class must offer no capability beyond creating a draft PR.

    ``close`` is excluded on purpose: it closes the HTTP client and performs no
    GitHub action. The check is on the public methods that could reach GitHub.
    """
    # Methods that only read or manage the local HTTP client.
    lifecycle = {"close", "get_branch", "has_write_scope"}
    write_like = [
        name
        for name in dir(GitHubWriteClient)
        if not name.startswith("_") and name not in lifecycle
    ]
    assert write_like == ["create_draft_pull_request"], (
        f"GitHubWriteClient exposes unexpected public methods: {write_like}"
    )


def test_write_client_has_no_forbidden_github_action() -> None:
    """No method name may suggest merge, force push, or commenting."""
    forbidden = ("merge", "force", "comment", "delete", "update_issue", "review")
    methods = [name for name in dir(GitHubWriteClient) if not name.startswith("_")]
    for name in methods:
        assert not any(word in name.lower() for word in forbidden), (
            f"GitHubWriteClient exposes {name!r}, which suggests a capability "
            "beyond creating a draft PR"
        )


def test_draft_flag_cannot_be_turned_off() -> None:
    """create_draft_pull_request must not accept a draft=False parameter."""
    signature = inspect.signature(GitHubWriteClient.create_draft_pull_request)
    assert "draft" not in signature.parameters


def test_publish_refuses_a_real_write_without_a_client(conn) -> None:
    """A real write with no client must fail rather than silently dry-run."""
    seed_ready(conn)
    with pytest.raises(PublisherError, match="write client"):
        publisher.publish(conn, SANDBOX, 7, target_branch=BRANCH, mode=PublishMode.REAL)


# ==========================================================================
# The token never leaks
# ==========================================================================


def test_token_never_appears_in_the_recorded_publication(conn) -> None:
    seed_ready(conn)
    http = FakeHTTP()
    secret = "ghp_" + "Q" * 36
    publisher.publish(
        conn,
        SANDBOX,
        7,
        target_branch=BRANCH,
        mode=PublishMode.REAL,
        client=make_client(http, token=secret),
    )
    for attempt in storage.list_publications(conn, SANDBOX, 7):
        assert secret not in attempt.model_dump_json()


def test_token_never_appears_in_the_audit_log(conn) -> None:
    seed_ready(conn)
    http = FakeHTTP()
    secret = "ghp_" + "Q" * 36
    publisher.publish(
        conn,
        SANDBOX,
        7,
        target_branch=BRANCH,
        mode=PublishMode.REAL,
        client=make_client(http, token=secret),
    )
    for event in audit.list_events(conn):
        assert secret not in event.model_dump_json()


def test_token_never_appears_in_the_plan_render(conn) -> None:
    seed_ready(conn)
    http = FakeHTTP()
    secret = "ghp_" + "Q" * 36
    plan = publisher.plan_publish(
        conn, SANDBOX, 7, target_branch=BRANCH, client=make_client(http, token=secret)
    )
    assert secret not in plan.render()


def test_token_never_appears_in_the_database_file(conn, tmp_path) -> None:
    """The strongest form: read the raw SQLite bytes and search for the token."""
    seed_ready(conn)
    secret = "ghp_" + "Q" * 36
    http = FakeHTTP()
    publisher.publish(
        conn,
        SANDBOX,
        7,
        target_branch=BRANCH,
        mode=PublishMode.REAL,
        client=make_client(http, token=secret),
    )
    conn.close()

    raw = (tmp_path / "ff.db").read_bytes()
    assert secret.encode() not in raw, "the publishing token was written to the database file"


def test_refusal_message_never_contains_the_token(conn) -> None:
    seed_ready(conn)
    secret = "ghp_" + "Q" * 36
    http = FakeHTTP(write_scope=False)
    with pytest.raises(RefusedByPolicyError) as excinfo:
        publisher.publish(
            conn,
            SANDBOX,
            7,
            target_branch=BRANCH,
            mode=PublishMode.REAL,
            client=make_client(http, token=secret),
        )
    assert secret not in str(excinfo.value)


def test_no_source_file_writes_the_token_to_disk() -> None:
    """The token must never be written to a file by the publisher."""
    text = Path(publisher.__file__).read_text(encoding="utf-8")
    assert "write_text(token" not in text
    assert "write_text(self._token" not in text


def test_a_revision_cannot_reuse_the_previous_approval(conn) -> None:
    """After publishing, a new patch still needs its own approval.

    The revision path (published -> implementing) must not become a way to
    publish without fresh human review.
    """
    seed_ready(conn)
    http = FakeHTTP()
    publisher.publish(
        conn, SANDBOX, 7, target_branch=BRANCH, mode=PublishMode.REAL, client=make_client(http)
    )

    changed = DIFF + "+    return None\n"
    storage.record_patch(
        conn,
        PatchRecord(
            repo_full_name=SANDBOX,
            issue_number=7,
            provider="mock",
            patch_sha256=compute_patch_sha256(changed),
            diff=changed,
        ),
    )
    storage.transition_task_state(conn, SANDBOX, 7, TaskState.IMPLEMENTING)
    storage.transition_task_state(conn, SANDBOX, 7, TaskState.VALIDATING)
    # Deliberately no new approval: the old one bound the old patch hash.
    with pytest.raises(RefusedByPolicyError):
        publisher.publish(
            conn,
            SANDBOX,
            7,
            target_branch=BRANCH,
            mode=PublishMode.REAL,
            client=make_client(http),
        )
    assert len(http.posts) == 1, "the unapproved revision must not have been published"


def test_published_state_allows_a_revision(conn) -> None:
    """A published task can return to implementation for a revision."""
    seed_ready(conn)
    http = FakeHTTP()
    publisher.publish(
        conn, SANDBOX, 7, target_branch=BRANCH, mode=PublishMode.REAL, client=make_client(http)
    )
    storage.transition_task_state(conn, SANDBOX, 7, TaskState.IMPLEMENTING)
    assert storage.get_issue(conn, SANDBOX, 7).task_state is TaskState.IMPLEMENTING


# ==========================================================================
# The CLI command
# ==========================================================================


def test_cli_publish_defaults_to_dry_run(tmp_path) -> None:
    """Running 'forgeflow publish' with no flag must not write anything."""
    from typer.testing import CliRunner

    from forgeflow.cli import app

    db_path = tmp_path / "ff.db"
    conn = storage.connect(db_path)
    storage.init_db(conn)
    seed_ready(conn)
    conn.close()

    config = tmp_path / "repos.yaml"
    config.write_text(f"repositories:\n  - {SANDBOX}\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "publish",
            f"{SANDBOX}#7",
            "--db",
            str(db_path),
            "--config",
            str(config),
            "--target-branch",
            BRANCH,
        ],
    )
    assert result.exit_code == 0, result.output
    assert "dry run" in result.output.lower()
    assert "nothing was written" in result.output.lower()


def test_cli_publish_dry_run_shows_the_plan(tmp_path) -> None:
    from typer.testing import CliRunner

    from forgeflow.cli import app

    db_path = tmp_path / "ff.db"
    conn = storage.connect(db_path)
    storage.init_db(conn)
    digest = seed_ready(conn)
    conn.close()

    config = tmp_path / "repos.yaml"
    config.write_text(f"repositories:\n  - {SANDBOX}\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "publish",
            f"{SANDBOX}#7",
            "--db",
            str(db_path),
            "--config",
            str(config),
            "--target-branch",
            BRANCH,
        ],
    )
    assert digest in result.output
    assert "draft:         True" in result.output
    assert f"repository:    {SANDBOX}" in result.output
    assert "[UNVERIFIED] target branch" in result.output


def test_cli_publish_refuses_a_third_party_repo(tmp_path) -> None:
    from typer.testing import CliRunner

    from forgeflow.cli import app

    db_path = tmp_path / "ff.db"
    conn = storage.connect(db_path)
    storage.init_db(conn)
    seed_ready(conn, repo=THIRD_PARTY)
    conn.close()

    config = tmp_path / "repos.yaml"
    config.write_text(f"repositories:\n  - {THIRD_PARTY}\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "publish",
            f"{THIRD_PARTY}#7",
            "--db",
            str(db_path),
            "--config",
            str(config),
            "--target-branch",
            BRANCH,
        ],
    )
    assert result.exit_code == 1
    assert "allowlist" in result.output
