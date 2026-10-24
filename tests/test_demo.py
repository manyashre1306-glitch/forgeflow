"""Tests for the offline dry-run demonstration (SPEC section 15).

These assert the demonstration actually exercises the real pipeline and, above
all, that it performs no external write. The GitHub clients are in-process fakes,
so "no write" is proven by counting requests rather than asserted by comment.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from demo import run_demo
from demo.run_demo import DEMO_ISSUE, DEMO_REPO, THIRD_PARTY_REPO


@pytest.fixture(scope="module")
def report():
    return run_demo()


def test_demo_runs_every_stage(report):
    stages = [stage for stage, _ in report.stages]
    assert stages == [
        "discover",
        "rank",
        "context",
        "analyze",
        "implement",
        "validate",
        "exception",
        "approve",
        "publish (dry run)",
        "safety",
    ]


def test_demo_makes_no_external_write(report):
    assert report.writes_attempted == 0, "the demonstration must not write to GitHub"


def test_demo_records_a_dry_run_not_a_publish(report):
    assert report.dry_run_recorded is True


def test_demo_ends_awaiting_publish_approval(report):
    # The dry run records the attempt but does not move the task to published.
    assert report.task_state == "awaiting_publish_approval"


def test_demo_refuses_a_third_party_repo(report):
    assert THIRD_PARTY_REPO in report.third_party_refused_reason
    assert "not on the publish allowlist" in report.third_party_refused_reason


def test_plan_render_names_the_mode_and_branch(report):
    assert "mode:          dry_run" in report.plan_render
    assert "head branch:   forgeflow/issue-1-" in report.plan_render
    assert DEMO_REPO in report.plan_render


def test_body_states_the_exception_rather_than_a_pass(report):
    # Honesty check: a docs-only patch that ran no checks must not read as a pass.
    assert "recorded validation exception" in report.body
    assert "No checks were executed for this patch" in report.body


def test_demo_module_runs_and_exits_zero():
    result = subprocess.run(
        [sys.executable, "-m", "demo"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Write requests attempted: 0" in result.stdout
    assert str(DEMO_ISSUE) in result.stdout
