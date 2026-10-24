"""Tests for optional scheduled discovery (SPEC section 13, Phase 4).

The scheduler must be discovery-only, off by default, and unable to reach the
publishing path. These tests run against a real temporary database and an
injected fake discovery callable. No network, no credentials.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from forgeflow import scheduler
from forgeflow.config import ScheduleConfig


class FakeDiscover:
    """A stand-in for one discovery pass. Records how often it ran."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return "discovered 3 issue(s)"


NOW = datetime(2026, 10, 24, tzinfo=UTC)


# ==========================================================================
# Off by default
# ==========================================================================


def test_schedule_is_off_by_default() -> None:
    assert ScheduleConfig().enabled is False


def test_disabled_schedule_does_not_run(conn) -> None:
    discover = FakeDiscover()
    outcome = scheduler.run_due(conn, ScheduleConfig(), discover=discover, now=NOW)
    assert outcome.ran is False
    assert outcome.reason == "scheduling is disabled"
    assert discover.calls == 0


def test_describe_schedule_says_disabled_by_default() -> None:
    assert "disabled" in scheduler.describe_schedule(ScheduleConfig())


# ==========================================================================
# Due calculation
# ==========================================================================


def test_first_cycle_is_due_when_enabled() -> None:
    assert scheduler.is_due(ScheduleConfig(enabled=True), None, now=NOW) is True


def test_not_due_before_the_interval_elapses() -> None:
    config = ScheduleConfig(enabled=True, interval_minutes=60)
    last = NOW - timedelta(minutes=30)
    assert scheduler.is_due(config, last, now=NOW) is False


def test_due_after_the_interval_elapses() -> None:
    config = ScheduleConfig(enabled=True, interval_minutes=60)
    last = NOW - timedelta(minutes=61)
    assert scheduler.is_due(config, last, now=NOW) is True


def test_disabled_is_never_due() -> None:
    assert scheduler.is_due(ScheduleConfig(enabled=False), None, now=NOW) is False


# ==========================================================================
# Running one cycle
# ==========================================================================


def test_enabled_schedule_runs_one_cycle(conn) -> None:
    discover = FakeDiscover()
    config = ScheduleConfig(enabled=True, interval_minutes=60)
    outcome = scheduler.run_due(conn, config, discover=discover, now=NOW)
    assert outcome.ran is True
    assert discover.calls == 1


def test_second_call_within_the_interval_does_not_run(conn) -> None:
    discover = FakeDiscover()
    config = ScheduleConfig(enabled=True, interval_minutes=60)
    scheduler.run_due(conn, config, discover=discover, now=NOW)
    outcome = scheduler.run_due(conn, config, discover=discover, now=NOW + timedelta(minutes=1))
    assert outcome.ran is False
    assert outcome.reason == "not due yet"
    assert discover.calls == 1


def test_cycle_runs_again_after_the_interval(conn) -> None:
    discover = FakeDiscover()
    config = ScheduleConfig(enabled=True, interval_minutes=60)
    scheduler.run_due(conn, config, discover=discover, now=NOW)
    outcome = scheduler.run_due(conn, config, discover=discover, now=NOW + timedelta(minutes=61))
    assert outcome.ran is True
    assert discover.calls == 2


def test_last_run_is_recorded(conn) -> None:
    config = ScheduleConfig(enabled=True, interval_minutes=60)
    scheduler.run_due(conn, config, discover=FakeDiscover(), now=NOW)
    assert scheduler.last_run(conn) is not None


# ==========================================================================
# Kill switch
# ==========================================================================


def test_kill_switch_prevents_the_cycle(conn) -> None:
    discover = FakeDiscover()
    config = ScheduleConfig(enabled=True, interval_minutes=60)
    outcome = scheduler.run_due(
        conn,
        config,
        discover=discover,
        kill_switch_engaged=lambda: True,
        now=NOW,
    )
    assert outcome.ran is False
    assert outcome.reason == "kill switch engaged"
    assert discover.calls == 0


# ==========================================================================
# Discovery only: the scheduler cannot reach publishing
# ==========================================================================


def test_only_discovery_may_be_scheduled() -> None:
    assert frozenset({"discover"}) == scheduler.ALLOWED_SCHEDULED_COMMANDS


def test_refuses_to_schedule_publishing(conn) -> None:
    config = ScheduleConfig(enabled=True, interval_minutes=60)
    with pytest.raises(scheduler.ScheduleError, match="only"):
        scheduler.run_due(
            conn,
            config,
            discover=FakeDiscover(),
            now=NOW,
            command="publish",
        )


def test_scheduler_module_imports_no_publisher_or_token_module() -> None:
    """A scheduled path to publishing would defeat SPEC section 8."""
    tree = ast.parse(Path(scheduler.__file__).read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    assert not any("publisher" in name for name in modules)
    assert not any("publish_token" in name for name in modules)


def test_scheduler_module_has_no_publish_call() -> None:
    """The module must not name a publishing function at all."""
    text = Path(scheduler.__file__).read_text(encoding="utf-8")
    assert "plan_publish" not in text
    assert "publish(" not in text


def test_schedule_schema_is_created_by_init_db(conn) -> None:
    names = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "schedule_state" in names
