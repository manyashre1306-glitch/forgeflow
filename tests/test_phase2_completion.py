"""Tests for the Phase 2 completion items: kill switch, persisted analyses,
workspace lifecycle, and analyze idempotence.

No network. The kill switch is exercised through both sources (config file and
environment) because either alone must be enough to stop the system.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from forgeflow import storage
from forgeflow.config import (
    KILL_SWITCH_ENV,
    AppConfig,
    KillSwitchEngagedError,
    LLMConfig,
    Settings,
    kill_switch_engaged,
    require_kill_switch_clear,
)
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
)
from forgeflow.pipeline import (
    PipelineError,
    load_analysis,
    run_analysis,
    run_review,
)
from forgeflow.repository_context import (
    cleanup_workspace,
    list_workspaces,
    prune_workspaces,
    remove_workspace,
    workspace_exists,
    workspace_path_for,
    workspace_size_bytes,
)

REPO = "acme/widget"

ANALYSIS_PAYLOAD = {
    "problem_explanation": "int() raises on non-numeric input.",
    "minimal_fix_proposal": "Catch ValueError and re-raise with context.",
    "risks": ["exception type changes"],
    "test_plan": ["test parse('x') raises ValueError"],
    "files_likely_involved": ["widget/parser.py"],
    "unverified_assumptions": [],
    "confidence": 0.7,
}


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_config(**overrides) -> LLMConfig:
    data = {
        "triage_model": "cheap",
        "analysis_model": "strong",
        "pricing": {
            "cheap": ModelPricing(input_per_million=1.0, output_per_million=2.0),
            "strong": ModelPricing(input_per_million=10.0, output_per_million=20.0),
        },
    }
    data.update(overrides)
    return LLMConfig(**data)


@pytest.fixture
def conn(tmp_path):
    connection = storage.connect(tmp_path / "t.db")
    storage.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def clear_kill_switch_env(monkeypatch):
    """Keep the ambient environment from leaking into the kill-switch tests."""
    monkeypatch.delenv(KILL_SWITCH_ENV, raising=False)


def seed_issue(conn, state: TaskState = TaskState.DISCOVERED) -> Issue:
    storage.upsert_repository(conn, Repository(full_name=REPO, stars=10))
    issue = Issue(repo_full_name=REPO, number=7, title="Bug: parser fails", body="x")
    storage.upsert_issue(conn, issue)
    if state is TaskState.DISCOVERED:
        return issue
    for target in (TaskState.SHORTLISTED, TaskState.ANALYZED, TaskState.AWAITING_PLAN_APPROVAL):
        storage.transition_task_state(conn, REPO, 7, target)
        if target is state:
            break
    return issue


def make_analyzer(conn) -> IssueAnalyzer:
    """An analyzer whose fake backend replays a valid triage then analysis reply.

    Both replies are needed because run_analysis calls triage first; replies are
    consumed in order.
    """
    triage = json.dumps({"actionable": True, "problem_summary": "clear", "clarity": 0.9})
    analysis = json.dumps(ANALYSIS_PAYLOAD)
    return IssueAnalyzer(
        FakeBackend([triage, analysis]),
        make_config(),
        budget_query=make_budget_query(conn),
        usage_recorder=make_usage_recorder(conn),
    )


def make_workspace_root(tmp_path) -> Path:
    root = tmp_path / "wsroot"
    target = root / "acme__widget__issue-7"
    write(target, "README.md", "# Widget\n")
    return root


# --------------------------------------------------------------------------
# Kill switch (SPEC section 9)
# --------------------------------------------------------------------------


def test_kill_switch_defaults_to_off() -> None:
    engaged, reason = kill_switch_engaged(AppConfig(repositories=["a/b"]))
    assert engaged is False
    assert reason == ""


def test_kill_switch_engages_from_config() -> None:
    config = AppConfig(repositories=["a/b"], kill_switch=True)
    engaged, reason = kill_switch_engaged(config)
    assert engaged is True
    assert "configuration file" in reason


def test_kill_switch_engages_from_environment(monkeypatch) -> None:
    monkeypatch.setenv(KILL_SWITCH_ENV, "1")
    engaged, reason = kill_switch_engaged(None, Settings())
    assert engaged is True
    assert KILL_SWITCH_ENV in reason


def test_environment_alone_is_enough() -> None:
    """No config object should be needed to stop the system."""
    engaged, _ = kill_switch_engaged(None)
    assert engaged is False  # not set in this test's environment


def test_require_kill_switch_clear_raises_when_engaged() -> None:
    config = AppConfig(repositories=["a/b"], kill_switch=True)
    with pytest.raises(KillSwitchEngagedError, match="kill switch engaged"):
        require_kill_switch_clear(config)


def test_require_kill_switch_clear_is_a_noop_when_off() -> None:
    require_kill_switch_clear(AppConfig(repositories=["a/b"]))


def test_kill_switch_error_names_the_remedy() -> None:
    config = AppConfig(repositories=["a/b"], kill_switch=True)
    with pytest.raises(KillSwitchEngagedError, match="Unset FORGEFLOW_KILL_SWITCH"):
        require_kill_switch_clear(config)


def test_kill_switch_blocks_model_calls_before_they_happen(conn) -> None:
    """A model call must not be attempted once the switch is engaged."""
    backend = FakeBackend([json.dumps(ANALYSIS_PAYLOAD)])
    analyzer = IssueAnalyzer(
        backend,
        make_config(),
        budget_query=make_budget_query(conn),
        usage_recorder=make_usage_recorder(conn),
        kill_switch_check=lambda: (_ for _ in ()).throw(KillSwitchEngagedError("stop")),
    )
    context = _make_context()
    with pytest.raises(KillSwitchEngagedError):
        analyzer.analyze(context, skip_triage=True)
    assert backend.calls == []


def test_kill_switch_blocks_sandbox_execution(tmp_path) -> None:
    from forgeflow.sandbox import IsolationKind, IsolationStatus, Sandbox

    sandbox = Sandbox(
        status=IsolationStatus(IsolationKind.DOCKER, True, "ok"),
        kill_switch_check=lambda: (_ for _ in ()).throw(KillSwitchEngagedError("stop")),
    )
    with pytest.raises(KillSwitchEngagedError):
        sandbox.run(["echo", "hi"], workspace=tmp_path)


def test_kill_switch_is_checked_before_isolation_refusal(tmp_path) -> None:
    """The switch must fire even when the sandbox would have refused anyway."""
    from forgeflow.sandbox import IsolationKind, IsolationStatus, Sandbox

    sandbox = Sandbox(
        status=IsolationStatus(IsolationKind.NONE, False, "no runtime"),
        kill_switch_check=lambda: (_ for _ in ()).throw(KillSwitchEngagedError("stop")),
    )
    with pytest.raises(KillSwitchEngagedError):
        sandbox.run(["echo"], workspace=tmp_path)


def _make_context():
    from forgeflow.models import RepoContext, UntrustedText

    return RepoContext(
        repo_full_name=REPO,
        issue_number=7,
        workspace_path="ws",
        issue=UntrustedText(text="Bug", source="issue"),
    )


# --------------------------------------------------------------------------
# Persisted analyses
# --------------------------------------------------------------------------


def test_analysis_round_trips_through_sqlite(conn, tmp_path) -> None:
    seed_issue(conn)
    root = make_workspace_root(tmp_path)
    outcome = run_analysis(conn, make_analyzer(conn), REPO, 7, workspace_root=root)

    loaded = load_analysis(conn, REPO, 7)
    assert loaded is not None
    assert loaded.problem_explanation == outcome.analysis.problem_explanation
    assert loaded.minimal_fix_proposal == outcome.analysis.minimal_fix_proposal
    assert loaded.test_plan == outcome.analysis.test_plan


def test_analysis_record_stores_the_model_and_confidence(conn, tmp_path) -> None:
    seed_issue(conn)
    root = make_workspace_root(tmp_path)
    run_analysis(conn, make_analyzer(conn), REPO, 7, workspace_root=root)

    record = storage.latest_analysis(conn, REPO, 7)
    assert record is not None
    assert record.model == "strong"
    assert record.confidence == pytest.approx(0.7)


def test_load_analysis_returns_none_when_absent(conn) -> None:
    seed_issue(conn)
    assert load_analysis(conn, REPO, 7) is None


def test_load_analysis_returns_none_for_stale_payload(conn) -> None:
    """A row written by an older schema must not break a review run."""
    from forgeflow.models import AnalysisRecord

    seed_issue(conn)
    storage.record_analysis(
        conn,
        AnalysisRecord(
            created_at=datetime.now(UTC),
            repo_full_name=REPO,
            issue_number=7,
            model="old",
            confidence=0.1,
            payload_json=json.dumps({"no_longer": "valid"}),
        ),
    )
    assert load_analysis(conn, REPO, 7) is None


def test_review_loads_the_stored_analysis(conn, tmp_path) -> None:
    """The gap that previously made a standalone review show no analysis."""
    seed_issue(conn)
    root = make_workspace_root(tmp_path)
    run_analysis(conn, make_analyzer(conn), REPO, 7, workspace_root=root)

    outcome = run_review(conn, REPO, 7, report_dir=tmp_path / "reports")
    text = outcome.report_path.read_text(encoding="utf-8")
    assert "int() raises on non-numeric input." in text
    assert "Catch ValueError and re-raise with context." in text
    assert "_No analysis was produced for this run._" not in text


def test_review_still_works_with_no_stored_analysis(conn, tmp_path) -> None:
    seed_issue(conn)
    outcome = run_review(conn, REPO, 7, report_dir=tmp_path / "reports")
    text = outcome.report_path.read_text(encoding="utf-8")
    assert "_No analysis was produced for this run._" in text


def test_explicit_analysis_overrides_the_stored_one(conn, tmp_path) -> None:
    seed_issue(conn)
    root = make_workspace_root(tmp_path)
    run_analysis(conn, make_analyzer(conn), REPO, 7, workspace_root=root)

    explicit = AnalysisResult(
        problem_explanation="passed in explicitly",
        minimal_fix_proposal="do the other thing",
    )
    outcome = run_review(conn, REPO, 7, analysis=explicit, report_dir=tmp_path / "reports")
    text = outcome.report_path.read_text(encoding="utf-8")
    assert "passed in explicitly" in text
    assert "int() raises on non-numeric input." not in text


def test_analyses_are_append_only(conn, tmp_path) -> None:
    """Each analysis is a new row, so history is preserved."""
    seed_issue(conn)
    root = make_workspace_root(tmp_path)
    run_analysis(conn, make_analyzer(conn), REPO, 7, workspace_root=root)
    run_analysis(conn, make_analyzer(conn), REPO, 7, workspace_root=root, force=True)

    assert len(storage.list_analyses(conn, REPO, 7)) == 2


# --------------------------------------------------------------------------
# Analyze idempotence (SPEC section 10)
# --------------------------------------------------------------------------


def test_reanalyzing_an_approved_task_refuses(conn, tmp_path) -> None:
    """SPEC 10: repeated runs must not re-spend on the same issue."""
    seed_issue(conn)
    root = make_workspace_root(tmp_path)
    run_analysis(conn, make_analyzer(conn), REPO, 7, workspace_root=root)

    with pytest.raises(PipelineError, match="already has an analysis"):
        run_analysis(conn, make_analyzer(conn), REPO, 7, workspace_root=root)


def test_refused_reanalysis_makes_no_model_call(conn, tmp_path) -> None:
    seed_issue(conn)
    root = make_workspace_root(tmp_path)
    run_analysis(conn, make_analyzer(conn), REPO, 7, workspace_root=root)

    backend = FakeBackend([json.dumps(ANALYSIS_PAYLOAD)])
    analyzer = IssueAnalyzer(
        backend,
        make_config(),
        budget_query=make_budget_query(conn),
        usage_recorder=make_usage_recorder(conn),
    )
    with pytest.raises(PipelineError):
        run_analysis(conn, analyzer, REPO, 7, workspace_root=root)
    assert backend.calls == []


def test_force_allows_reanalysis(conn, tmp_path) -> None:
    seed_issue(conn)
    root = make_workspace_root(tmp_path)
    run_analysis(conn, make_analyzer(conn), REPO, 7, workspace_root=root)
    outcome = run_analysis(conn, make_analyzer(conn), REPO, 7, workspace_root=root, force=True)
    assert outcome.analysis.minimal_fix_proposal


def test_force_does_not_change_the_state(conn, tmp_path) -> None:
    seed_issue(conn)
    root = make_workspace_root(tmp_path)
    run_analysis(conn, make_analyzer(conn), REPO, 7, workspace_root=root)
    outcome = run_analysis(conn, make_analyzer(conn), REPO, 7, workspace_root=root, force=True)
    assert outcome.issue.task_state is TaskState.AWAITING_PLAN_APPROVAL


def test_first_analysis_is_not_blocked(conn, tmp_path) -> None:
    seed_issue(conn)
    root = make_workspace_root(tmp_path)
    outcome = run_analysis(conn, make_analyzer(conn), REPO, 7, workspace_root=root)
    assert outcome.issue.task_state is TaskState.AWAITING_PLAN_APPROVAL


def test_refusal_names_the_force_flag(conn, tmp_path) -> None:
    seed_issue(conn)
    root = make_workspace_root(tmp_path)
    run_analysis(conn, make_analyzer(conn), REPO, 7, workspace_root=root)
    with pytest.raises(PipelineError, match="force=True"):
        run_analysis(conn, make_analyzer(conn), REPO, 7, workspace_root=root)


# --------------------------------------------------------------------------
# Workspace lifecycle
# --------------------------------------------------------------------------


def test_workspace_exists(tmp_path) -> None:
    root = make_workspace_root(tmp_path)
    assert workspace_exists(root, REPO, 7) is True
    assert workspace_exists(root, REPO, 999) is False


def test_remove_workspace_deletes_it(tmp_path) -> None:
    root = make_workspace_root(tmp_path)
    assert remove_workspace(root, REPO, 7) is True
    assert workspace_exists(root, REPO, 7) is False


def test_remove_workspace_reports_when_absent(tmp_path) -> None:
    assert remove_workspace(tmp_path / "nothing", REPO, 7) is False


def test_remove_workspace_cannot_escape_the_root(tmp_path) -> None:
    """The path is rebuilt internally, so a crafted name cannot escape."""
    root = tmp_path / "wsroot"
    root.mkdir()
    outside = tmp_path / "important"
    outside.mkdir()
    write(outside, "keep.txt", "do not delete")

    # A traversal-shaped name must resolve inside root, not to tmp_path.
    remove_workspace(root, "../../important", 7)
    assert outside.is_dir()
    assert (outside / "keep.txt").is_file()


def test_cleanup_workspace_matches_remove(tmp_path) -> None:
    root = make_workspace_root(tmp_path)
    assert cleanup_workspace(root, REPO, 7) is True
    assert workspace_exists(root, REPO, 7) is False


def test_list_workspaces_is_empty_for_missing_root(tmp_path) -> None:
    assert list_workspaces(tmp_path / "nope") == []


def test_list_workspaces_returns_only_directories(tmp_path) -> None:
    root = tmp_path / "wsroot"
    root.mkdir()
    (root / "a").mkdir()
    (root / "b").mkdir()
    write(root, "stray.txt", "not a workspace")
    names = [path.name for path in list_workspaces(root)]
    assert names == ["a", "b"]


def test_workspace_size_is_positive(tmp_path) -> None:
    root = make_workspace_root(tmp_path)
    assert workspace_size_bytes(root / "acme__widget__issue-7") > 0


def test_workspace_size_zero_for_missing(tmp_path) -> None:
    assert workspace_size_bytes(tmp_path / "nope") == 0


def test_prune_keeps_the_newest(tmp_path) -> None:
    root = tmp_path / "wsroot"
    root.mkdir()
    import os
    import time

    for index in range(5):
        entry = root / f"ws{index}"
        entry.mkdir()
        write(entry, "f.txt", "x")
        # Space the mtimes so "newest" is unambiguous.
        stamp = time.time() - (10 - index) * 100
        os.utime(entry, (stamp, stamp))

    removed = prune_workspaces(root, keep=2)
    assert len(removed) == 3
    remaining = sorted(path.name for path in list_workspaces(root))
    assert remaining == ["ws3", "ws4"]


def test_prune_is_a_noop_when_under_the_limit(tmp_path) -> None:
    root = tmp_path / "wsroot"
    root.mkdir()
    for index in range(2):
        (root / f"ws{index}").mkdir()
    assert prune_workspaces(root, keep=5) == []


def test_prune_with_keep_zero_removes_everything(tmp_path) -> None:
    root = tmp_path / "wsroot"
    root.mkdir()
    for index in range(3):
        (root / f"ws{index}").mkdir()
    assert len(prune_workspaces(root, keep=0)) == 3
    assert list_workspaces(root) == []


def test_prune_rejects_a_negative_keep(tmp_path) -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        prune_workspaces(tmp_path, keep=-1)


def test_workspace_path_is_deterministic() -> None:
    first = workspace_path_for("data/workspaces", REPO, 7)
    second = workspace_path_for("data/workspaces", REPO, 7)
    assert first == second
    assert first.name == "acme__widget__issue-7"


def test_pruned_workspace_is_really_gone_from_disk(tmp_path) -> None:
    root = make_workspace_root(tmp_path)
    extra = root / "acme__widget__issue-8"
    write(extra, "README.md", "# Other\n")
    import os
    import time

    stamp = time.time() - 10_000
    os.utime(extra, (stamp, stamp))

    removed = prune_workspaces(root, keep=1)
    assert removed == [extra]
    assert not extra.exists()
    assert workspace_exists(root, REPO, 7)
