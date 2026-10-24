"""The offline, dry-run end-to-end demonstration (SPEC section 15).

It runs the real ForgeFlow functions in sequence against a controlled fixture
workspace, so the whole chain is exercised for real rather than stubbed:

    discover -> rank -> context -> analyze -> implement -> validate ->
    exception -> approve -> dry-run publish

Two things are substituted, and both are stated in the output:

* the coding agent is :class:`~forgeflow.agent_runner.MockAgent`, which produces a
  fixed unified diff and never calls a model; and
* the GitHub clients are fakes that record every request, so the demo can *prove*
  that a dry run issues no write.

The publish step is a dry run. It builds the complete plan and the PR body that
*would* be sent and records the attempt as a dry run, but it sends nothing.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from forgeflow import approval, storage
from forgeflow.agent_runner import MockAgent
from forgeflow.approval import ApprovalAction
from forgeflow.config import AppConfig, LLMConfig, ModelPricing
from forgeflow.issue_analyzer import (
    FakeBackend,
    IssueAnalyzer,
    make_budget_query,
    make_usage_recorder,
)
from forgeflow.models import (
    Issue,
    PublishMode,
    Repository,
)
from forgeflow.pipeline import run_analysis, run_context, run_implement, run_validation
from forgeflow.publisher import GitHubWriteClient, plan_publish, publish
from forgeflow.ranking import rank_issues
from forgeflow.sandbox import IsolationKind, IsolationStatus, Sandbox
from forgeflow.validation import ValidationCommands, Validator

#: The demonstration targets the one allowlisted repository, so the dry-run
#: publish can show every pre-publish check. Nothing is written: the clients are
#: fakes and the mode is DRY_RUN.
DEMO_REPO = "manyashre1306-glitch/forgeflow-sandbox"
DEMO_ISSUE = 1
#: A repository that is deliberately not on the allowlist, to show the refusal.
THIRD_PARTY_REPO = "some-org/some-repo"
TARGET_BRANCH = "main"

NO_ISOLATION = IsolationStatus(
    IsolationKind.NONE,
    False,
    "no container runtime is available in this environment",
)

TRIAGE_JSON = json.dumps(
    {
        "actionable": True,
        "problem_summary": "README has no Notes section; the request is a small doc addition.",
        "clarity": 0.9,
        "scope_hint": "small",
        "missing_information": [],
        "injection_flagged": False,
        "confidence": 0.85,
    }
)

ANALYSIS_JSON = json.dumps(
    {
        "problem_explanation": "The README documents the project but has no Notes section.",
        "minimal_fix_proposal": "Append a short 'Notes' section to README.md.",
        "risks": ["none of substance for a docs-only change"],
        "test_plan": ["no automated test applies to a prose-only change"],
        "files_likely_involved": ["README.md"],
        "unverified_assumptions": ["the maintainer wants a Notes section"],
        "confidence": 0.8,
    }
)

#: The diff MockAgent produces for the demo. It matches the real Phase 4 patch.
MOCK_DIFF = (
    "diff --git a/README.md b/README.md\n"
    "--- a/README.md\n"
    "+++ b/README.md\n"
    "@@ -1 +1,4 @@\n"
    " # forgeflow-sandbox\n"
    "+## Notes\n"
    "+\n"
    "+- Synthetic sandbox test change.\n"
)

EXCEPTION_REASON = (
    "synthetic demonstration change; no container runtime is available, so the "
    "repository checks were not executed and are recorded as skipped, not passed"
)


@dataclass
class DemoReport:
    """Everything the demonstration did, in a form a test can assert on."""

    stages: list[tuple[str, str]] = field(default_factory=list)
    plan_render: str = ""
    body: str = ""
    writes_attempted: int = 0
    dry_run_recorded: bool = False
    third_party_refused_reason: str = ""
    task_state: str = ""

    def note(self, stage: str, detail: str) -> None:
        self.stages.append((stage, detail))

    def render(self) -> str:
        lines = ["ForgeFlow offline dry-run demonstration (SPEC section 15)", "=" * 60, ""]
        for stage, detail in self.stages:
            lines.append(f"[{stage}] {detail}")
        lines.append("")
        lines.append("-" * 60)
        lines.append("Pre-publish plan (dry run; nothing was written):")
        lines.append("")
        lines.append(self.plan_render)
        lines.append("")
        lines.append("-" * 60)
        lines.append(f"Write requests attempted: {self.writes_attempted}")
        lines.append(f"Dry run recorded in the log: {self.dry_run_recorded}")
        lines.append(f"Third-party repo refusal: {self.third_party_refused_reason}")
        lines.append(f"Final task state: {self.task_state}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# A fake GitHub that records every request, so a dry run can be proven
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class RecordingHTTP:
    """A minimal in-memory GitHub that counts writes.

    ``posts`` records every request that would change state. The demo asserts it
    stays empty, which is the whole point of a dry run.
    """

    def __init__(self, *, base_files: dict[str, str], existing_branches: list[str]) -> None:
        self.base_files = base_files
        self.existing_branches = existing_branches
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict]] = []

    def get(self, url: str, headers: dict | None = None, **kwargs: object) -> _FakeResponse:
        self.gets.append(url)
        if "/contents/" in url:
            path = url.split("/contents/")[1].split("?")[0]
            if path not in self.base_files:
                return _FakeResponse(404, {})
            encoded = base64.b64encode(self.base_files[path].encode()).decode()
            return _FakeResponse(200, {"content": encoded, "encoding": "base64"})
        if "/issues/" in url:
            return _FakeResponse(200, {"number": DEMO_ISSUE})
        if "/git/ref/heads/" in url:
            return _FakeResponse(200, {"object": {"sha": "a" * 40}})
        if "/branches?" in url:
            return _FakeResponse(200, [{"name": name} for name in self.existing_branches])
        if "/branches/" in url:
            return _FakeResponse(200, {"commit": {"sha": "a" * 40}})
        return _FakeResponse(200, {"permissions": {"push": True}})

    def post(self, url: str, json: dict | None = None, **kwargs: object) -> _FakeResponse:
        self.posts.append((url, json or {}))
        return _FakeResponse(201, {})

    def close(self) -> None:
        pass


class FakeReadClient:
    """A read-only client whose issue always exists. No network."""

    def __init__(self) -> None:
        self.reads: list[tuple[str, int]] = []

    def issue_exists(self, repo_full_name: str, issue_number: int) -> bool:
        self.reads.append((repo_full_name, issue_number))
        return True


# --------------------------------------------------------------------------
# The demonstration
# --------------------------------------------------------------------------


def _fixture_workspace(workspace_root: Path) -> Path:
    """Create the small repository tree the context step reads.

    A controlled directory, not a clone, so the demo needs no git and no network.
    The path matches ``workspace_path_for`` so the context step finds it.
    """
    from forgeflow.repository_context import workspace_path_for

    workspace = workspace_path_for(workspace_root, DEMO_REPO, DEMO_ISSUE)
    (workspace / "tests").mkdir(parents=True, exist_ok=True)
    (workspace / "README.md").write_text(
        "# forgeflow-sandbox\n\nA repository used to exercise ForgeFlow.\n", encoding="utf-8"
    )
    (workspace / "CONTRIBUTING.md").write_text(
        "# Contributing\n\nKeep changes small and focused.\n", encoding="utf-8"
    )
    (workspace / "tests" / "test_readme.py").write_text(
        "def test_readme_exists():\n    assert True\n", encoding="utf-8"
    )
    return workspace


def _demo_config() -> AppConfig:
    return AppConfig(
        repositories=[DEMO_REPO],
        llm=LLMConfig(
            triage_model="cheap-model",
            analysis_model="strong-model",
            pricing={
                "cheap-model": ModelPricing(input_per_million=1.0, output_per_million=2.0),
                "strong-model": ModelPricing(input_per_million=10.0, output_per_million=20.0),
            },
            monthly_cap_usd=5.0,
        ),
    )


def run_demo(
    db_path: str | Path = ":memory:",
    workspace_root: str | Path | None = None,
) -> DemoReport:
    """Run the whole pipeline offline and return what it produced.

    ``db_path`` defaults to an in-memory database and ``workspace_root`` to a
    fresh temporary directory, so a run leaves nothing behind on disk.
    """
    report = DemoReport()

    if workspace_root is None:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="forgeflow-demo-") as tmp:
            conn = storage.connect(db_path)
            storage.init_db(conn)
            try:
                _run_pipeline(conn, report, Path(tmp))
            finally:
                conn.close()
        return report

    conn = storage.connect(db_path)
    storage.init_db(conn)
    try:
        _run_pipeline(conn, report, Path(workspace_root))
    finally:
        conn.close()
    return report


def _run_pipeline(conn: sqlite3.Connection, report: DemoReport, workspace_root: Path) -> None:
    config = _demo_config()
    _fixture_workspace(workspace_root)

    # 1. Seed the discovered issue (discovery itself needs the network; the
    #    offline demo starts from the record discovery would have written).
    storage.upsert_repository(conn, Repository(full_name=DEMO_REPO, stars=0))
    storage.upsert_issue(
        conn,
        Issue(
            repo_full_name=DEMO_REPO,
            number=DEMO_ISSUE,
            title="Add a Notes section to the README",
            body="Synthetic demonstration issue used to exercise the publish path.",
        ),
    )
    report.note("discover", f"seeded {DEMO_REPO}#{DEMO_ISSUE} at 'discovered' (no network)")

    # 2. Rank.
    issue = storage.get_issue(conn, DEMO_REPO, DEMO_ISSUE)
    assert issue is not None
    ranked = rank_issues([issue], config)
    top = ranked[0]
    report.note(
        "rank",
        f"#{top.rank} total={top.score.compute_total(dict(config.weights)):.2f}/100 "
        f"for {DEMO_REPO}#{DEMO_ISSUE}",
    )

    # 3. Context (from the controlled workspace, no clone).
    context_outcome = run_context(
        conn, DEMO_REPO, DEMO_ISSUE, workspace_root=workspace_root, clone=False
    )
    context = context_outcome.context
    report.note(
        "context",
        f"readme={context.readme is not None} contributing={len(context.contributing)} "
        f"test_layout={context.test_layout} -> state '{context_outcome.issue.task_state.value}'",
    )

    # 4. Analyze (scripted backend, no model call).
    analyzer = IssueAnalyzer(
        FakeBackend(replies=[TRIAGE_JSON, ANALYSIS_JSON]),
        config.llm,
        budget_query=make_budget_query(conn),
        usage_recorder=make_usage_recorder(conn),
    )
    analysis_outcome = run_analysis(conn, analyzer, DEMO_REPO, DEMO_ISSUE, context=context)
    report.note(
        "analyze",
        f"plan='{analysis_outcome.analysis.minimal_fix_proposal}' "
        f"-> state '{analysis_outcome.issue.task_state.value}'",
    )

    # 5. Implement with the deterministic mock agent, no isolation.
    sandbox = Sandbox(status=NO_ISOLATION)
    implement_outcome = run_implement(
        conn,
        MockAgent(diff=MOCK_DIFF),
        sandbox,
        DEMO_REPO,
        DEMO_ISSUE,
        plan=analysis_outcome.analysis.minimal_fix_proposal,
        context=context,
    )
    patch = implement_outcome.patch
    assert patch is not None
    report.note(
        "implement",
        f"provider={patch.provider} sha256={patch.patch_sha256[:12]}… "
        f"-> state '{implement_outcome.issue.task_state.value}'",
    )

    # 6. Validate. No container runtime, so every check is recorded as SKIPPED and
    #    the task moves to failed_validation. Skipped is not passed.
    validator = Validator(sandbox, limits=sandbox.limits)
    validation_outcome = run_validation(
        conn,
        DEMO_REPO,
        DEMO_ISSUE,
        validator=validator,
        workspace_root=workspace_root,
        commands=ValidationCommands(tests=["python", "-m", "pytest"], lint=["ruff", "check", "."]),
    )
    statuses = ", ".join(f"{c.name}={c.status.value}" for c in validation_outcome.report.checks)
    report.note(
        "validate",
        f"passed={validation_outcome.passed} ({statuses}) "
        f"-> state '{validation_outcome.issue.task_state.value}'",
    )

    # 7. Record the documented exception that authorizes this exact patch. Without
    #    it, an all-skipped report could never be approved.
    storage.record_validation_exception(
        conn,
        DEMO_REPO,
        DEMO_ISSUE,
        patch_sha256=patch.patch_sha256,
        authorized_by="demo-operator",
        reason=EXCEPTION_REASON,
    )
    report.note("exception", f"recorded for sha256={patch.patch_sha256[:12]}… (bound to the patch)")

    # 8. Approve. The approval binds to the SHA-256 of the exact diff.
    _, state = approval.apply_action(
        conn,
        DEMO_REPO,
        DEMO_ISSUE,
        ApprovalAction.APPROVE,
        actor="demo-operator",
        reason="reviewed the dry-run bundle",
        target_branch=TARGET_BRANCH,
    )
    report.note("approve", f"approval bound to the patch hash -> state '{state.value}'")

    # 9. Dry-run publish. Fake clients, DRY_RUN mode: the full plan is built and
    #    the attempt is recorded, but no write is issued.
    http = RecordingHTTP(
        base_files={"README.md": "# forgeflow-sandbox\n"},
        existing_branches=[TARGET_BRANCH],
    )
    write_client = GitHubWriteClient(token="demo-token", client=http)
    read_client = FakeReadClient()

    plan = plan_publish(
        conn,
        DEMO_REPO,
        DEMO_ISSUE,
        target_branch=TARGET_BRANCH,
        mode=PublishMode.DRY_RUN,
        client=write_client,
        read_client=read_client,
        actor="demo-operator",
    )
    report.plan_render = plan.render()
    report.body = plan.body

    record = publish(
        conn,
        DEMO_REPO,
        DEMO_ISSUE,
        target_branch=TARGET_BRANCH,
        mode=PublishMode.DRY_RUN,
        client=write_client,
        read_client=read_client,
        actor="demo-operator",
    )
    attempts = storage.list_publications(conn, DEMO_REPO, DEMO_ISSUE)
    report.dry_run_recorded = (
        record.published is False
        and record.mode is PublishMode.DRY_RUN
        and any(a.mode is PublishMode.DRY_RUN and not a.published for a in attempts)
    )
    report.writes_attempted = len(http.posts)
    report.note(
        "publish (dry run)",
        f"refused={plan.refused} checks={len(plan.checks)} "
        f"write requests attempted={len(http.posts)}",
    )

    # 10. Safety: a repository that is not on the allowlist is refused before any
    #     database lookup or client is used.
    third_party = plan_publish(
        conn,
        THIRD_PARTY_REPO,
        999,
        target_branch=TARGET_BRANCH,
        mode=PublishMode.DRY_RUN,
    )
    report.third_party_refused_reason = third_party.refusal_reason
    report.note("safety", f"{THIRD_PARTY_REPO} refused: {third_party.refusal_reason}")

    final_issue = storage.get_issue(conn, DEMO_REPO, DEMO_ISSUE)
    report.task_state = final_issue.task_state.value if final_issue else ""


def main() -> int:
    """Run the demonstration and print the report. Returns a process exit code."""
    report = run_demo()
    print(report.render())
    if report.writes_attempted != 0:
        print(f"\nERROR: the dry run attempted {report.writes_attempted} write(s).")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
