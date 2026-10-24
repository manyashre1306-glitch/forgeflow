"""Tests for patch capture, review reports, and the Phase 2 pipeline.

No network. The pipeline tests drive the real state machine and storage against a
temporary SQLite database, with a local fixture repository for context.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from forgeflow import storage
from forgeflow.agent_runner import AgentCommand, AgentResult, MockAgent
from forgeflow.config import LLMConfig
from forgeflow.issue_analyzer import (
    FakeBackend,
    IssueAnalyzer,
    make_budget_query,
    make_usage_recorder,
)
from forgeflow.models import (
    AnalysisResult,
    Issue,
    ModelPricing,
    Repository,
    TaskState,
    TriageResult,
)
from forgeflow.patch import (
    PatchError,
    analyse_diff,
    build_patch_record,
    changed_files,
    compute_patch_sha256,
    verify_patch_hash,
    workspace_diff,
)
from forgeflow.pipeline import (
    PipelineError,
    run_analysis,
    run_context,
    run_implement,
    run_review,
)
from forgeflow.review import render_review_report, review_report_path
from forgeflow.sandbox import IsolationKind, IsolationStatus, Sandbox

REPO = "acme/widget"
UNAVAILABLE = IsolationStatus(IsolationKind.NONE, False, "no container runtime")

SAMPLE_DIFF = """diff --git a/widget/parser.py b/widget/parser.py
index 1111111..2222222 100644
--- a/widget/parser.py
+++ b/widget/parser.py
@@ -1,3 +1,6 @@
 def parse(value):
-    return int(value)
+    try:
+        return int(value)
+    except ValueError as exc:
+        raise ValueError(f"bad: {value!r}") from exc
"""


def make_issue(number: int = 7) -> Issue:
    return Issue(
        repo_full_name=REPO,
        number=number,
        title="Bug: parser fails on nested input",
        body="Steps: pass a non-numeric string.\nExpected ValueError, got int().\nSee parser.py.",
        labels=["bug"],
        url=f"https://github.com/{REPO}/issues/{number}",
    )


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def conn(tmp_path):
    connection = storage.connect(tmp_path / "t.db")
    storage.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def workspace(tmp_path) -> Path:
    """A git workspace with one commit, so diffs are meaningful."""
    root = tmp_path / "ws"
    write(root, "README.md", "# Widget\n")
    write(root, "CONTRIBUTING.md", "Tests are required for every fix.\n")
    write(root, "widget/parser.py", "def parse(value):\n    return int(value)\n")
    write(root, "tests/test_parser.py", "def test_ok():\n    assert True\n")
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    for command in (
        ["git", "init", "--quiet", "-b", "main"],
        ["git", "add", "-A"],
        ["git", "commit", "--quiet", "-m", "init"],
    ):
        subprocess.run(command, cwd=root, env=env, check=True, capture_output=True)
    return root


def seed_issue(conn, state: TaskState = TaskState.DISCOVERED) -> Issue:
    """Insert the repository and issue, optionally advancing to a later state.

    The issues table has a foreign key to repositories, so the repository row
    must exist first. Later states are reached by walking the legal path, so the
    fixture cannot set up a state the state machine would refuse.
    """
    storage.upsert_repository(conn, Repository(full_name=REPO, stars=10))
    issue = make_issue()
    storage.upsert_issue(conn, issue)
    if state is TaskState.DISCOVERED:
        return issue

    path = [
        TaskState.SHORTLISTED,
        TaskState.ANALYZED,
        TaskState.AWAITING_PLAN_APPROVAL,
    ]
    for target in path:
        storage.transition_task_state(conn, REPO, 7, target)
        if target is state:
            break
    return issue


def make_analysis() -> AnalysisResult:
    return AnalysisResult(
        problem_explanation="int() raises on non-numeric input.",
        minimal_fix_proposal="Catch ValueError and re-raise with context.",
        risks=["exception type changes"],
        test_plan=["test parse('x') raises ValueError"],
        files_likely_involved=["widget/parser.py"],
        unverified_assumptions=["no caller depends on the raw error"],
        confidence=0.7,
        triage=TriageResult(actionable=True, problem_summary="clear", clarity=0.9),
    )


def make_analyzer(conn, *, skip_triage: bool = False) -> IssueAnalyzer:
    config = LLMConfig(
        triage_model="cheap",
        analysis_model="strong",
        pricing={
            "cheap": ModelPricing(input_per_million=1.0, output_per_million=2.0),
            "strong": ModelPricing(input_per_million=10.0, output_per_million=20.0),
        },
    )
    triage = json.dumps({"actionable": True, "problem_summary": "clear", "clarity": 0.9})
    analysis = json.dumps(
        {
            "problem_explanation": "int() raises on non-numeric input.",
            "minimal_fix_proposal": "Catch ValueError and re-raise with context.",
            "risks": ["exception type changes"],
            "test_plan": ["test parse('x') raises ValueError"],
            "files_likely_involved": ["widget/parser.py"],
            "unverified_assumptions": [],
            "confidence": 0.7,
        }
    )
    # Replies are consumed in order, so when triage is skipped the analysis
    # reply must come first.
    replies = [analysis] if skip_triage else [triage, analysis]
    return IssueAnalyzer(
        FakeBackend(replies),
        config,
        budget_query=make_budget_query(conn),
        usage_recorder=make_usage_recorder(conn),
    )


# --- patch hashing ----------------------------------------------------------


def test_hash_is_64_hex_characters() -> None:
    digest = compute_patch_sha256(SAMPLE_DIFF)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_hash_is_stable_across_calls() -> None:
    assert compute_patch_sha256(SAMPLE_DIFF) == compute_patch_sha256(SAMPLE_DIFF)


def test_hash_changes_when_one_byte_changes() -> None:
    other = SAMPLE_DIFF.replace("bad:", "bad2:")
    assert compute_patch_sha256(SAMPLE_DIFF) != compute_patch_sha256(other)


def test_hash_is_sensitive_to_trailing_whitespace() -> None:
    assert compute_patch_sha256(SAMPLE_DIFF) != compute_patch_sha256(SAMPLE_DIFF + " ")


def test_verify_patch_hash_accepts_the_same_diff() -> None:
    assert verify_patch_hash(SAMPLE_DIFF, compute_patch_sha256(SAMPLE_DIFF)) is True


def test_verify_patch_hash_rejects_a_modified_diff() -> None:
    """SPEC 2I: a changed patch invalidates an earlier approval."""
    approved = compute_patch_sha256(SAMPLE_DIFF)
    assert verify_patch_hash(SAMPLE_DIFF + "\n# extra", approved) is False


def test_known_sha256_vector() -> None:
    """Pin the hash to a value computed independently of the implementation."""
    import hashlib

    assert compute_patch_sha256("hello") == hashlib.sha256(b"hello").hexdigest()


# --- diff analysis ----------------------------------------------------------


def test_analyse_diff_counts_files_and_lines() -> None:
    stats = analyse_diff(SAMPLE_DIFF)
    assert stats.files_changed == 1
    assert stats.additions == 4
    assert stats.deletions == 1
    assert stats.files == ["widget/parser.py"]


def test_analyse_diff_ignores_file_headers() -> None:
    """The '+++' header must not be counted as an added line."""
    stats = analyse_diff(SAMPLE_DIFF)
    # 3 added body lines plus the '+++' line would be 5 if the header counted.
    assert stats.additions == 4


def test_analyse_diff_handles_multiple_files() -> None:
    diff = SAMPLE_DIFF + SAMPLE_DIFF.replace("parser.py", "other.py")
    stats = analyse_diff(diff)
    assert stats.files_changed == 2


def test_analyse_diff_empty() -> None:
    stats = analyse_diff("")
    assert stats.is_empty is True


def test_analyse_diff_skips_cache_directories() -> None:
    diff = """diff --git a/.pytest_cache/x b/.pytest_cache/x
--- a/.pytest_cache/x
+++ b/.pytest_cache/x
@@ -0,0 +1 @@
+junk
"""
    assert analyse_diff(diff).files_changed == 0


def test_build_patch_record_refuses_empty_diff() -> None:
    with pytest.raises(PatchError, match="empty patch"):
        build_patch_record(repo_full_name=REPO, issue_number=7, provider="mock", diff="")


def test_build_patch_record_refuses_diff_without_files() -> None:
    with pytest.raises(PatchError, match="no file changes"):
        build_patch_record(
            repo_full_name=REPO,
            issue_number=7,
            provider="mock",
            diff="not a diff at all\n",
        )


def test_build_patch_record_hashes_the_diff() -> None:
    record = build_patch_record(
        repo_full_name=REPO, issue_number=7, provider="mock", diff=SAMPLE_DIFF
    )
    assert record.patch_sha256 == compute_patch_sha256(SAMPLE_DIFF)
    assert record.files_changed == 1


# --- workspace diff ---------------------------------------------------------


def test_workspace_diff_sees_tracked_changes(workspace) -> None:
    write(workspace, "widget/parser.py", "def parse(value):\n    return str(value)\n")
    diff = workspace_diff(workspace)
    assert "widget/parser.py" in diff


def test_workspace_diff_includes_new_files(workspace) -> None:
    """A file the agent created must appear in the review bundle."""
    write(workspace, "widget/helper.py", "VALUE = 1\n")
    diff = workspace_diff(workspace)
    assert "widget/helper.py" in diff


def test_workspace_diff_empty_when_clean(workspace) -> None:
    assert workspace_diff(workspace).strip() == ""


def test_workspace_diff_missing_directory_raises(tmp_path) -> None:
    with pytest.raises(PatchError, match="workspace directory not found"):
        workspace_diff(tmp_path / "nope")


def test_changed_files_lists_paths(workspace) -> None:
    write(workspace, "widget/parser.py", "x = 1\n")
    write(workspace, "widget/new.py", "y = 1\n")
    files = changed_files(workspace)
    assert "widget/parser.py" in files
    assert "widget/new.py" in files


def test_workspace_diff_does_not_modify_the_index(workspace) -> None:
    """Capturing a diff must be read-only."""
    write(workspace, "widget/new.py", "y = 1\n")
    workspace_diff(workspace)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    # The new file must still be untracked, not staged.
    assert "?? widget/new.py" in status


# --- patch persistence ------------------------------------------------------


def test_patch_round_trips_through_sqlite(conn) -> None:
    record = build_patch_record(
        repo_full_name=REPO, issue_number=7, provider="mock", diff=SAMPLE_DIFF
    )
    storage.record_patch(conn, record)
    loaded = storage.latest_patch(conn, REPO, 7)
    assert loaded is not None
    assert loaded.patch_sha256 == record.patch_sha256
    assert loaded.diff == SAMPLE_DIFF
    assert loaded.files_changed == 1


def test_latest_patch_returns_newest(conn) -> None:
    for index in range(3):
        record = build_patch_record(
            repo_full_name=REPO,
            issue_number=7,
            provider="mock",
            diff=SAMPLE_DIFF.replace("bad:", f"bad{index}:"),
        )
        storage.record_patch(conn, record)
    loaded = storage.latest_patch(conn, REPO, 7)
    assert loaded is not None
    assert loaded.patch_sha256 == compute_patch_sha256(SAMPLE_DIFF.replace("bad:", "bad2:"))


def test_latest_patch_none_when_absent(conn) -> None:
    assert storage.latest_patch(conn, REPO, 7) is None


def test_list_patches_filters_by_issue(conn) -> None:
    storage.record_patch(
        conn,
        build_patch_record(repo_full_name=REPO, issue_number=7, provider="m", diff=SAMPLE_DIFF),
    )
    storage.record_patch(
        conn,
        build_patch_record(
            repo_full_name=REPO,
            issue_number=8,
            provider="m",
            diff=SAMPLE_DIFF.replace("parser.py", "other.py"),
        ),
    )
    assert len(storage.list_patches(conn, REPO, 7)) == 1
    assert len(storage.list_patches(conn)) == 2


# --- review report ----------------------------------------------------------


def test_review_report_has_all_required_sections() -> None:
    text = render_review_report(
        issue=make_issue(),
        analysis=make_analysis(),
        agent_result=None,
        patch=build_patch_record(
            repo_full_name=REPO, issue_number=7, provider="mock", diff=SAMPLE_DIFF
        ),
        executed_in_sandbox=False,
    )
    for heading in (
        "## Analysis",
        "## Repository context",
        "## Commands executed",
        "## Proposed fix (full diff)",
        "## Limitations",
        "## Proposed PR description",
    ):
        assert heading in text


def test_review_report_includes_the_issue_url() -> None:
    text = render_review_report(
        issue=make_issue(),
        analysis=None,
        agent_result=None,
        patch=None,
    )
    assert f"https://github.com/{REPO}/issues/7" in text


def test_review_report_warns_when_not_executed() -> None:
    text = render_review_report(
        issue=make_issue(),
        analysis=make_analysis(),
        agent_result=None,
        patch=None,
        executed_in_sandbox=False,
    )
    assert "NOT executed or tested" in text


def test_review_report_says_so_when_no_commands_ran() -> None:
    """An empty commands table must not read as 'all tests passed'."""
    text = render_review_report(issue=make_issue(), analysis=None, agent_result=None, patch=None)
    assert "No commands were executed" in text
    assert "no test result is claimed" in text


def test_review_report_includes_command_output() -> None:
    result = AgentResult(
        provider="mock",
        session_id="s",
        diff=SAMPLE_DIFF,
        commands=[AgentCommand(command="pytest -q", output="3 passed", exit_code=0)],
    )
    text = render_review_report(issue=make_issue(), analysis=None, agent_result=result, patch=None)
    assert "pytest -q" in text
    assert "3 passed" in text


def test_review_report_contains_the_full_diff() -> None:
    patch = build_patch_record(
        repo_full_name=REPO, issue_number=7, provider="mock", diff=SAMPLE_DIFF
    )
    text = render_review_report(issue=make_issue(), analysis=None, agent_result=None, patch=patch)
    assert "```diff" in text
    assert 'raise ValueError(f"bad: {value!r}") from exc' in text


def test_review_report_states_not_approved() -> None:
    text = render_review_report(issue=make_issue(), analysis=None, agent_result=None, patch=None)
    assert "Not approved and not published" in text


def test_review_report_flags_unknown_ai_policy() -> None:
    from forgeflow.models import RepoContext, UntrustedText

    context = RepoContext(
        repo_full_name=REPO,
        issue_number=7,
        workspace_path="ws",
        issue=UntrustedText(text="x", source="issue"),
    )
    text = render_review_report(
        issue=make_issue(), analysis=None, agent_result=None, patch=None, context=context
    )
    assert "Unknown is not permission" in text


def test_review_report_proposed_pr_body_mentions_the_issue() -> None:
    text = render_review_report(
        issue=make_issue(),
        analysis=make_analysis(),
        agent_result=None,
        patch=build_patch_record(
            repo_full_name=REPO, issue_number=7, provider="mock", diff=SAMPLE_DIFF
        ),
    )
    assert "Fixes #7" in text


def test_review_report_path_is_deterministic_in_shape() -> None:
    path = review_report_path(REPO, 7, "data/reports")
    assert path.parent == Path("data/reports")
    assert path.name.startswith("review-acme__widget-7-")
    assert path.suffix == ".md"


# --- review bundle: AI policy restriction -----------------------------------


def _restricted_rules():
    from forgeflow.models import AIPolicy, ContributionRules

    return ContributionRules(
        ai_policy=AIPolicy.PROHIBITED,
        ai_policy_evidence=["CONTRIBUTING.md: Do not generate PRs using AI."],
        requires_ai_disclosure=True,
    )


def test_review_report_warns_on_ai_restriction() -> None:
    from forgeflow.models import RepoContext, UntrustedText

    context = RepoContext(
        repo_full_name=REPO,
        issue_number=7,
        workspace_path="ws",
        issue=UntrustedText(text="x", source="issue"),
        contribution_rules=_restricted_rules(),
    )
    text = render_review_report(
        issue=make_issue(), analysis=None, agent_result=None, patch=None, context=context
    )
    assert "WARNING: this repository restricts AI-generated contributions" in text
    assert "BLOCKED" in text


def test_review_report_shows_disclosure_requirement() -> None:
    from forgeflow.models import RepoContext, UntrustedText

    context = RepoContext(
        repo_full_name=REPO,
        issue_number=7,
        workspace_path="ws",
        issue=UntrustedText(text="x", source="issue"),
        contribution_rules=_restricted_rules(),
    )
    text = render_review_report(
        issue=make_issue(), analysis=None, agent_result=None, patch=None, context=context
    )
    assert "Requires AI disclosure: True" in text


def test_run_context_persists_the_detected_rules(conn, workspace, tmp_path) -> None:
    """The rules must survive so a later review can read them."""
    seed_issue(conn)
    run_context(conn, REPO, 7, workspace_root=tmp_path / "wsroot", source_path=workspace)
    stored = storage.latest_contribution_rules(conn, REPO, 7)
    assert stored is not None
    assert stored.requires_tests is True  # the fixture CONTRIBUTING says so


def test_standalone_review_reads_the_persisted_policy(conn, tmp_path) -> None:
    """A review with no context object still warns when a restriction is stored."""
    from forgeflow.models import AIPolicy, ContributionRules

    seed_issue(conn, state=TaskState.AWAITING_PLAN_APPROVAL)
    storage.record_contribution_rules(
        conn,
        REPO,
        7,
        ContributionRules(
            ai_policy=AIPolicy.PROHIBITED,
            ai_policy_evidence=["CONTRIBUTING.md: no AI-generated code"],
        ),
    )
    outcome = run_review(conn, REPO, 7, report_dir=tmp_path / "reports")
    text = outcome.report_path.read_text(encoding="utf-8")
    assert "restricts AI-generated contributions" in text


def test_latest_contribution_rules_none_when_absent(conn) -> None:
    assert storage.latest_contribution_rules(conn, REPO, 7) is None


# --- pipeline: context ------------------------------------------------------


def test_run_context_shortlists_and_builds(conn, workspace, tmp_path) -> None:
    seed_issue(conn)
    outcome = run_context(
        conn,
        REPO,
        7,
        workspace_root=tmp_path / "wsroot",
        source_path=workspace,
    )
    assert outcome.issue.task_state is TaskState.SHORTLISTED
    assert outcome.context.repo_full_name == REPO
    assert outcome.context.readme is not None


def test_run_context_without_clone_uses_existing_workspace(conn, tmp_path) -> None:
    seed_issue(conn)
    root = tmp_path / "wsroot"
    target = root / "acme__widget__issue-7"
    write(target, "README.md", "# Widget\n")
    outcome = run_context(conn, REPO, 7, workspace_root=root, clone=False)
    assert outcome.context.readme is not None


def test_run_context_missing_issue_raises(conn) -> None:
    with pytest.raises(PipelineError, match="not in the database"):
        run_context(conn, REPO, 999)


# --- pipeline: analysis -----------------------------------------------------


def test_run_analysis_moves_to_awaiting_plan_approval(conn, tmp_path) -> None:
    seed_issue(conn)
    root = tmp_path / "wsroot"
    write(root / "acme__widget__issue-7", "README.md", "# Widget\n")
    outcome = run_analysis(conn, make_analyzer(conn), REPO, 7, workspace_root=root)
    assert outcome.issue.task_state is TaskState.AWAITING_PLAN_APPROVAL
    assert outcome.analysis.minimal_fix_proposal


def test_run_analysis_records_cost(conn, tmp_path) -> None:
    seed_issue(conn)
    root = tmp_path / "wsroot"
    write(root / "acme__widget__issue-7", "README.md", "# Widget\n")
    run_analysis(conn, make_analyzer(conn), REPO, 7, workspace_root=root)
    assert len(storage.list_llm_usage(conn)) == 2
    assert storage.spent_in_month(conn) > 0.0


def test_run_analysis_skips_triage_when_asked(conn, tmp_path) -> None:
    seed_issue(conn)
    root = tmp_path / "wsroot"
    write(root / "acme__widget__issue-7", "README.md", "# Widget\n")
    outcome = run_analysis(
        conn,
        make_analyzer(conn, skip_triage=True),
        REPO,
        7,
        workspace_root=root,
        skip_triage=True,
    )
    assert outcome.analysis.triage is None
    assert len(storage.list_llm_usage(conn)) == 1


# --- pipeline: implement ----------------------------------------------------


def test_run_implement_requires_plan_approval(conn, tmp_path) -> None:
    """SPEC: implementation must not be reachable without approval."""
    seed_issue(conn, TaskState.SHORTLISTED)
    root = tmp_path / "wsroot"
    write(root / "acme__widget__issue-7", "README.md", "# Widget\n")
    with pytest.raises(PipelineError, match="awaiting_plan_approval"):
        run_implement(
            conn,
            MockAgent(),
            Sandbox(status=UNAVAILABLE),
            REPO,
            7,
            "plan",
            workspace_root=root,
        )


def test_run_implement_captures_a_hashed_patch(conn, tmp_path) -> None:
    seed_issue(conn, TaskState.AWAITING_PLAN_APPROVAL)
    root = tmp_path / "wsroot"
    write(root / "acme__widget__issue-7", "README.md", "# Widget\n")
    outcome = run_implement(
        conn,
        MockAgent(),
        Sandbox(status=UNAVAILABLE),
        REPO,
        7,
        "Change parser.py only.",
        workspace_root=root,
    )
    assert outcome.patch is not None
    assert outcome.patch.patch_sha256 == compute_patch_sha256(outcome.patch.diff)
    assert outcome.issue.task_state is TaskState.VALIDATING
    assert storage.latest_patch(conn, REPO, 7) is not None


def test_run_implement_marks_patch_unverified_without_isolation(conn, tmp_path) -> None:
    seed_issue(conn, TaskState.AWAITING_PLAN_APPROVAL)
    root = tmp_path / "wsroot"
    write(root / "acme__widget__issue-7", "README.md", "# Widget\n")
    outcome = run_implement(
        conn,
        MockAgent(),
        Sandbox(status=UNAVAILABLE),
        REPO,
        7,
        "plan",
        workspace_root=root,
    )
    assert outcome.executed_in_sandbox is False
    assert outcome.patch is not None
    assert outcome.patch.isolated is False
    assert any("no repository code was executed" in item.lower() for item in outcome.limitations)


def test_run_implement_never_publishes(conn, tmp_path) -> None:
    seed_issue(conn, TaskState.AWAITING_PLAN_APPROVAL)
    root = tmp_path / "wsroot"
    write(root / "acme__widget__issue-7", "README.md", "# Widget\n")
    outcome = run_implement(
        conn,
        MockAgent(),
        Sandbox(status=UNAVAILABLE),
        REPO,
        7,
        "plan",
        workspace_root=root,
    )
    assert outcome.agent_result.published is False


def test_run_implement_handles_agent_with_no_diff(conn, tmp_path) -> None:
    seed_issue(conn, TaskState.AWAITING_PLAN_APPROVAL)
    root = tmp_path / "wsroot"
    write(root / "acme__widget__issue-7", "README.md", "# Widget\n")
    outcome = run_implement(
        conn,
        MockAgent(diff=""),
        Sandbox(status=UNAVAILABLE),
        REPO,
        7,
        "plan",
        workspace_root=root,
    )
    assert outcome.patch is None
    assert storage.latest_patch(conn, REPO, 7) is None


# --- pipeline: review -------------------------------------------------------


def test_run_review_writes_a_file(conn, tmp_path) -> None:
    seed_issue(conn, TaskState.AWAITING_PLAN_APPROVAL)
    outcome = run_review(conn, REPO, 7, report_dir=tmp_path / "reports")
    assert outcome.report_path.is_file()
    text = outcome.report_path.read_text(encoding="utf-8")
    assert "Review bundle" in text


def test_run_review_picks_up_the_latest_patch(conn, tmp_path) -> None:
    seed_issue(conn, TaskState.AWAITING_PLAN_APPROVAL)
    storage.record_patch(
        conn,
        build_patch_record(repo_full_name=REPO, issue_number=7, provider="mock", diff=SAMPLE_DIFF),
    )
    outcome = run_review(conn, REPO, 7, report_dir=tmp_path / "reports")
    assert outcome.patch is not None
    text = outcome.report_path.read_text(encoding="utf-8")
    assert outcome.patch.patch_sha256 in text


def test_run_review_without_patch_still_writes(conn, tmp_path) -> None:
    seed_issue(conn)
    outcome = run_review(conn, REPO, 7, report_dir=tmp_path / "reports")
    assert outcome.patch is None
    assert "No patch was captured" in outcome.report_path.read_text(encoding="utf-8")


def test_full_phase2_flow(conn, workspace, tmp_path) -> None:
    """context -> analyze -> implement -> review, through the real state machine."""
    seed_issue(conn)
    ws_root = tmp_path / "wsroot"
    reports = tmp_path / "reports"

    ctx_outcome = run_context(conn, REPO, 7, workspace_root=ws_root, source_path=workspace)
    assert ctx_outcome.issue.task_state is TaskState.SHORTLISTED

    analysis_outcome = run_analysis(conn, make_analyzer(conn), REPO, 7, context=ctx_outcome.context)
    assert analysis_outcome.issue.task_state is TaskState.AWAITING_PLAN_APPROVAL

    impl_outcome = run_implement(
        conn,
        MockAgent(),
        Sandbox(status=UNAVAILABLE),
        REPO,
        7,
        analysis_outcome.analysis.minimal_fix_proposal,
        context=ctx_outcome.context,
    )
    assert impl_outcome.issue.task_state is TaskState.VALIDATING
    assert impl_outcome.patch is not None

    review_outcome = run_review(
        conn,
        REPO,
        7,
        analysis=analysis_outcome.analysis,
        agent_result=impl_outcome.agent_result,
        context=ctx_outcome.context,
        patch=impl_outcome.patch,
        limitations=impl_outcome.limitations,
        executed_in_sandbox=False,
        report_dir=reports,
    )
    text = review_outcome.report_path.read_text(encoding="utf-8")
    assert "Fixes #7" in text
    assert impl_outcome.patch.patch_sha256 in text
    assert "NOT executed or tested" in text
    assert "Not approved and not published" in text
