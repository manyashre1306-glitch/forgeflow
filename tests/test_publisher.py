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
    """An in-memory GitHub that records requests.

    A POST is a write, so tests assert on the POST count. It models just enough
    of the object API for a real branch push: branches, file contents, blobs,
    trees, commits, and refs.
    """

    def __init__(
        self,
        *,
        branch_sha: str = "a" * 40,
        write_scope: bool = True,
        pr_response: dict | None = None,
        base_files: dict[str, str] | None = None,
        existing_branches: list[str] | None = None,
    ) -> None:
        self.branch_sha = branch_sha
        self.write_scope = write_scope
        self.pr_response = pr_response or {
            "number": 42,
            "html_url": f"https://github.com/{SANDBOX}/pull/42",
            "draft": True,
        }
        # Files on the base branch, readable through the contents endpoint.
        self.base_files = base_files if base_files is not None else {"p.py": "def f():\n"}
        self.existing_branches = existing_branches if existing_branches is not None else [BRANCH]
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict]] = []
        self.created_refs: list[str] = []
        self.created_commits: list[dict] = []

    def get(self, url: str, headers: dict | None = None, **kwargs: object) -> FakeResponse:
        self.gets.append(url)
        if "/contents/" in url:
            path = url.split("/contents/")[1].split("?")[0]
            if path not in self.base_files:
                return FakeResponse(404)
            import base64

            encoded = base64.b64encode(self.base_files[path].encode()).decode()
            return FakeResponse(200, {"content": encoded, "encoding": "base64"})
        if "/git/ref/heads/" in url:
            return FakeResponse(200, {"object": {"sha": self.branch_sha}})
        if url.endswith("/branches?per_page=100") or "/branches?" in url:
            return FakeResponse(200, [{"name": name} for name in self.existing_branches])
        if "/branches/" in url:
            return FakeResponse(200, {"commit": {"sha": self.branch_sha}})
        # The repository endpoint, used for the permission-scope check.
        return FakeResponse(200, {"permissions": {"push": self.write_scope}})

    def post(
        self, url: str, json: dict | None = None, headers: dict | None = None, **kwargs: object
    ) -> FakeResponse:
        payload = json or {}
        self.posts.append((url, payload))
        if url.endswith("/git/blobs"):
            return FakeResponse(201, {"sha": "b" * 40})
        if url.endswith("/git/trees"):
            return FakeResponse(201, {"sha": "c" * 40})
        if url.endswith("/git/commits"):
            self.created_commits.append(payload)
            return FakeResponse(201, {"sha": "d" * 40})
        if url.endswith("/git/refs"):
            self.created_refs.append(payload.get("ref", ""))
            return FakeResponse(201, {"ref": payload.get("ref", "")})
        return FakeResponse(201, self.pr_response)

    def close(self) -> None:
        pass

    @property
    def writes(self) -> list[tuple[str, dict]]:
        """Only the requests that change GitHub state."""
        return [
            (url, payload)
            for url, payload in self.posts
            if url.endswith("/pulls")
            or url.endswith("/git/blobs")
            or url.endswith("/git/trees")
            or url.endswith("/git/commits")
            or url.endswith("/git/refs")
        ]


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


def pr_count(http: FakeHTTP) -> int:
    """How many pull requests were created. Branch writes are counted separately."""
    return sum(1 for url, _ in http.posts if url.endswith("/pulls"))


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
    assert pr_count(http) == 1

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
    assert pr_count(http) == 1, "the second attempt must not have written"


def test_idempotence_is_per_patch_not_per_issue(conn) -> None:
    """A new patch for the same issue is a new publish, not a duplicate."""
    seed_ready(conn)
    http = FakeHTTP()
    publisher.publish(
        conn, SANDBOX, 7, target_branch=BRANCH, mode=PublishMode.REAL, client=make_client(http)
    )
    assert pr_count(http) == 1

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
    assert pr_count(http) == 2


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

    # The PR is the last write, after the branch, tree, and commit.
    pr_posts = [(url, payload) for url, payload in http.posts if url.endswith("/pulls")]
    assert len(pr_posts) == 1
    url, payload = pr_posts[0]
    assert url.endswith(f"/repos/{SANDBOX}/pulls")
    assert payload["draft"] is True
    assert payload["base"] == BRANCH
    assert payload["head"] == record.head_branch


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
    reads = {
        "close",
        "get_branch",
        "get_file_content",
        "get_ref_sha",
        "has_write_scope",
        "list_branches",
    }
    writes = [
        name for name in dir(GitHubWriteClient) if not name.startswith("_") and name not in reads
    ]
    assert sorted(writes) == ["create_draft_pull_request", "push_patch_branch"], (
        f"GitHubWriteClient exposes unexpected write methods: {writes}"
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
    assert pr_count(http) == 1, "the unapproved revision must not have been published"


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


# ==========================================================================
# Branch creation guardrails (the approved limits for push_patch_branch)
# ==========================================================================


def client_with(http: FakeHTTP) -> GitHubWriteClient:
    return make_client(http)


def test_push_branch_is_refused_without_approval(conn) -> None:
    """The branch push sits behind the same checks, so no approval means no write."""
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
            client=client_with(http),
        )
    assert http.writes == [], "an unapproved publish must not create a branch"


def test_push_branch_is_refused_for_third_party_repo(conn) -> None:
    """The allowlist still gates the branch push."""
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
            client=client_with(http),
        )
    assert http.writes == []


def test_push_branch_is_refused_on_duplicate(conn) -> None:
    """A second publish of the same patch must not create a second branch."""
    seed_ready(conn)
    http = FakeHTTP()
    publisher.publish(
        conn, SANDBOX, 7, target_branch=BRANCH, mode=PublishMode.REAL, client=client_with(http)
    )
    refs_after_first = len(http.created_refs)
    assert refs_after_first == 1

    with pytest.raises(RefusedByPolicyError, match="already published"):
        publisher.publish(
            conn, SANDBOX, 7, target_branch=BRANCH, mode=PublishMode.REAL, client=client_with(http)
        )
    assert len(http.created_refs) == refs_after_first, "no second branch may be created"


def test_push_branch_is_refused_when_branch_already_exists(conn) -> None:
    """If the head branch already exists on the remote, refuse rather than update."""
    seed_ready(conn)
    head = publisher.head_branch_for(7, HASH)
    http = FakeHTTP(existing_branches=[BRANCH, head])
    with pytest.raises((publisher.BranchPolicyError, PublisherError), match="already exists"):
        publisher.publish(
            conn, SANDBOX, 7, target_branch=BRANCH, mode=PublishMode.REAL, client=client_with(http)
        )
    assert http.created_refs == [], "no ref may be created for an existing branch"


def test_push_branch_refuses_main_as_a_head_branch() -> None:
    """ForgeFlow may never create or write to main."""
    for name in ("main", "master", "develop", "trunk"):
        with pytest.raises(publisher.BranchPolicyError):
            publisher.require_creatable_branch(name)


def test_push_branch_requires_the_forgeflow_prefix() -> None:
    """A branch without the prefix must be refused."""
    for name in ("fix-thing", "release-1.0", "forgeflowx/issue-1"):
        with pytest.raises(publisher.BranchPolicyError):
            publisher.require_creatable_branch(name, existing=[])


def test_push_branch_is_refused_if_the_patch_hash_changed(conn) -> None:
    """A changed patch invalidates the approval, so no branch may be created."""
    seed_ready(conn)
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
            conn, SANDBOX, 7, target_branch=BRANCH, mode=PublishMode.REAL, client=client_with(http)
        )
    assert http.writes == [], "no branch may be created for an unapproved patch"


def test_push_branch_is_never_reachable_in_a_dry_run(conn) -> None:
    """The dry run must stop before the branch push."""
    seed_ready(conn)
    http = FakeHTTP()
    publisher.publish(conn, SANDBOX, 7, target_branch=BRANCH, client=client_with(http))
    assert http.writes == [], "dry run must not create a branch or a PR"
    assert http.created_refs == []


def test_push_branch_creates_a_new_ref_with_the_forgeflow_prefix(conn) -> None:
    """The successful path creates exactly one ref, under forgeflow/."""
    seed_ready(conn)
    http = FakeHTTP()
    publisher.publish(
        conn, SANDBOX, 7, target_branch=BRANCH, mode=PublishMode.REAL, client=client_with(http)
    )
    assert len(http.created_refs) == 1
    assert http.created_refs[0].startswith("refs/heads/forgeflow/")
    # The commit's parent is the base branch tip, so it is not a rewrite.
    assert len(http.created_commits) == 1
    assert http.created_commits[0]["parents"] == ["a" * 40]


def test_push_branch_never_updates_an_existing_ref(conn) -> None:
    """Every ref POST must be a create, expressed as refs/heads/<new name>."""
    seed_ready(conn)
    http = FakeHTTP()
    publisher.publish(
        conn, SANDBOX, 7, target_branch=BRANCH, mode=PublishMode.REAL, client=client_with(http)
    )
    ref_posts = [(u, p) for u, p in http.posts if u.endswith("/git/refs")]
    for _url, payload in ref_posts:
        assert payload["ref"].startswith("refs/heads/forgeflow/")
        assert "force" not in payload


def test_push_branch_refuses_a_diff_that_does_not_apply(conn) -> None:
    """A patch that does not apply to the base tree must not create a branch."""
    seed_ready(conn)
    # The base branch no longer contains the context the diff expects.
    http = FakeHTTP(base_files={"p.py": "def totally_different():\n"})
    with pytest.raises(PublisherError, match="does not apply"):
        publisher.publish(
            conn, SANDBOX, 7, target_branch=BRANCH, mode=PublishMode.REAL, client=client_with(http)
        )
    assert http.created_refs == [], "no branch may be created from a patch that does not apply"


def test_push_branch_refuses_a_missing_base_file(conn) -> None:
    """If the base tree lacks a file the patch modifies, refuse."""
    seed_ready(conn)
    http = FakeHTTP(base_files={})
    with pytest.raises(PublisherError):
        publisher.publish(
            conn, SANDBOX, 7, target_branch=BRANCH, mode=PublishMode.REAL, client=client_with(http)
        )
    assert http.created_refs == []


def test_pushed_file_content_matches_the_patch(conn) -> None:
    """The blob written must be the patched content, not the original."""
    seed_ready(conn)
    http = FakeHTTP(base_files={"p.py": "def f():\n"})
    publisher.publish(
        conn, SANDBOX, 7, target_branch=BRANCH, mode=PublishMode.REAL, client=client_with(http)
    )
    blob_posts = [p for u, p in http.posts if u.endswith("/git/blobs")]
    assert len(blob_posts) == 1
    assert blob_posts[0]["content"] == "def f():\n    pass\n"


# ==========================================================================
# The PR body reports only recorded facts
# ==========================================================================


def test_pr_body_includes_recorded_test_evidence(conn) -> None:
    """A passing check must appear in the body, not a 'no evidence' placeholder."""
    seed_ready(conn)
    plan = publisher.plan_publish(conn, SANDBOX, 7, target_branch=BRANCH)
    assert "`pytest`: passed" in plan.body
    assert "No test evidence was recorded" not in plan.body


def test_pr_body_says_so_when_no_evidence_exists() -> None:
    """With no validation passed in, the body must say so rather than imply a pass.

    Called directly because the approval gate means a task cannot reach
    awaiting_publish_approval without a passing check, so this branch of the body
    builder is only reachable in its own right.
    """
    body = publisher.build_pr_body(
        repo_full_name=SANDBOX,
        issue_number=9,
        patch_sha256=HASH,
        diff=DIFF,
    )
    assert "No test evidence was recorded" in body
    assert "No analysis summary was recorded" in body
    assert "passed" not in body, "the body must not imply a check passed"


def test_pr_body_includes_the_patch_hash(conn) -> None:
    """The body must carry the hash that binds it to the approved patch."""
    digest = seed_ready(conn)
    plan = publisher.plan_publish(conn, SANDBOX, 7, target_branch=BRANCH)
    assert digest in plan.body


def test_pr_body_states_it_is_not_auto_merged(conn) -> None:
    seed_ready(conn)
    plan = publisher.plan_publish(conn, SANDBOX, 7, target_branch=BRANCH)
    assert "not auto-merged" in plan.body
