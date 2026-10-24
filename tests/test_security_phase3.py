"""Tests for the Phase 3 security safeguards (SPEC section 9).

Covers the append-only audit log, the persistent kill switch, and the per-task
resource guardrails. No network, no container runtime.
"""

from __future__ import annotations

import sqlite3

import pytest

from forgeflow import audit, killswitch, storage
from forgeflow.config import (
    KILL_SWITCH_ENV,
    AppConfig,
    kill_switch_engaged,
    require_kill_switch_clear,
)
from forgeflow.killswitch import GuardLimits, KillSwitchError, TaskGuard
from forgeflow.models import AuditEvent


@pytest.fixture
def conn(tmp_path):
    connection = storage.connect(tmp_path / "t.db")
    storage.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def clear_kill_switch_env(monkeypatch):
    monkeypatch.delenv(KILL_SWITCH_ENV, raising=False)


# --------------------------------------------------------------------------
# Append-only audit log
# --------------------------------------------------------------------------


def test_audit_event_is_stored(conn) -> None:
    event = audit.record(conn, "test.event", detail="hello")
    assert event.id
    assert audit.count_events(conn) >= 1


def test_audit_is_append_only_for_update(conn) -> None:
    event = audit.record(conn, "test.event", detail="original")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE audit_log SET detail = 'tampered' WHERE id = ?", (event.id,))


def test_audit_is_append_only_for_delete(conn) -> None:
    event = audit.record(conn, "test.event")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM audit_log WHERE id = ?", (event.id,))


def test_verify_append_only_reports_true(conn) -> None:
    ok, why = audit.verify_append_only(conn)
    assert ok is True
    assert why == ""


def test_audit_redacts_secrets(conn) -> None:
    """A credential in an error message must not reach the log."""
    event = audit.record(conn, "test.leak", detail="token ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    assert "ghp_" not in event.detail
    stored = audit.list_events(conn, event="test.leak")[0]
    assert "ghp_" not in stored.detail


def test_audit_redacts_secrets_in_payload(conn) -> None:
    event = audit.record(
        conn,
        "test.leak",
        payload={"header": "Authorization: Bearer abcdef123456"},
    )
    assert "abcdef123456" not in event.payload_json


def test_audit_records_issue_reference(conn) -> None:
    audit.record(conn, "test.event", repo_full_name="a/b", issue_number=7)
    events = audit.list_events(conn, repo_full_name="a/b", issue_number=7)
    assert events[0].repo_full_name == "a/b"
    assert events[0].issue_number == 7


def test_audit_filters_by_event(conn) -> None:
    audit.record(conn, "alpha")
    audit.record(conn, "beta")
    assert all(e.event == "beta" for e in audit.list_events(conn, event="beta"))


def test_audit_newest_first(conn) -> None:
    audit.record(conn, "first")
    audit.record(conn, "second")
    events = audit.list_events(conn, limit=2)
    assert events[0].event == "second"


def test_audit_limit_is_respected(conn) -> None:
    for index in range(5):
        audit.record(conn, f"e{index}")
    assert len(audit.list_events(conn, limit=3)) == 3


def test_audit_mirrors_to_jsonl(tmp_path, conn) -> None:
    path = tmp_path / "audit" / "log.jsonl"
    audit.record(conn, "mirrored", detail="x", log_path=path)
    assert path.is_file()
    assert "mirrored" in path.read_text(encoding="utf-8")


def test_audit_event_model_is_serialisable(conn) -> None:
    event = audit.record(conn, "test.event")
    restored = AuditEvent.model_validate_json(event.model_dump_json())
    assert restored.event == "test.event"


def test_audit_ordering_is_stable(conn) -> None:
    for index in range(3):
        audit.record(conn, f"seq{index}")
    events = audit.list_events(conn, limit=3)
    ids = [e.id for e in events]
    assert ids == sorted(ids, reverse=True)


# --------------------------------------------------------------------------
# Persistent kill switch
# --------------------------------------------------------------------------


def test_kill_switch_defaults_to_disengaged(conn) -> None:
    assert killswitch.read_state(conn).engaged is False


def test_kill_switch_can_be_engaged(conn) -> None:
    state = killswitch.set_state(conn, True, reason="stop everything", actor="tester")
    assert state.engaged is True
    assert state.reason == "stop everything"
    assert state.changed_by == "tester"


def test_kill_switch_can_be_disengaged(conn) -> None:
    killswitch.set_state(conn, True)
    state = killswitch.set_state(conn, False, actor="tester")
    assert state.engaged is False


def test_kill_switch_persists_across_reconnect(tmp_path) -> None:
    path = tmp_path / "k.db"
    first = storage.connect(path)
    storage.init_db(first)
    killswitch.set_state(first, True, reason="persist")
    first.close()

    second = storage.connect(path)
    state = killswitch.read_state(second)
    assert state.engaged is True
    assert state.reason == "persist"
    second.close()


def test_database_flag_engages_the_combined_check(conn) -> None:
    killswitch.set_state(conn, True, reason="db", actor="tester")
    engaged, why = kill_switch_engaged(None, None, conn)
    assert engaged is True
    assert "database" in why


def test_database_flag_blocks_require_clear(conn) -> None:
    killswitch.set_state(conn, True)
    with pytest.raises(Exception, match="kill switch engaged"):
        require_kill_switch_clear(None, None, conn)


def test_any_source_alone_is_sufficient(conn) -> None:
    """Config, environment, and database are independent stops."""
    config = AppConfig(repositories=["a/b"], kill_switch=True)
    assert kill_switch_engaged(config, None, None)[0] is True
    assert kill_switch_engaged(None, None, conn)[0] is False  # db clear


def test_environment_still_engages_with_clear_database(conn, monkeypatch) -> None:
    monkeypatch.setenv(KILL_SWITCH_ENV, "1")
    assert kill_switch_engaged(None, None, conn)[0] is True


def test_kill_switch_reason_is_reported(conn) -> None:
    killswitch.set_state(conn, True, reason="spend incident", actor="oncall")
    _, why = kill_switch_engaged(None, None, conn)
    assert "spend incident" in why
    assert "oncall" in why


# --------------------------------------------------------------------------
# TaskGuard: per-task resource limits
# --------------------------------------------------------------------------


def test_guard_attempts_are_capped() -> None:
    guard = TaskGuard("a/b", 1, limits=GuardLimits(max_attempts=2))
    guard.record_attempt()
    guard.record_attempt()
    with pytest.raises(KillSwitchError, match="attempt limit"):
        guard.record_attempt()


def test_guard_tokens_are_capped() -> None:
    guard = TaskGuard("a/b", 1, limits=GuardLimits(max_tokens=100))
    guard.record_usage(tokens=50)
    with pytest.raises(KillSwitchError, match="token budget"):
        guard.record_usage(tokens=100)


def test_guard_cost_is_capped() -> None:
    guard = TaskGuard("a/b", 1, limits=GuardLimits(max_cost_usd=1.0))
    with pytest.raises(KillSwitchError, match="cost budget"):
        guard.record_usage(cost_usd=1.5)


def test_guard_time_is_capped() -> None:
    guard = TaskGuard("a/b", 1, limits=GuardLimits(max_seconds=0.0))
    with pytest.raises(KillSwitchError, match="time limit"):
        guard.check_time()


def test_guard_disabled_limits_do_not_raise() -> None:
    guard = TaskGuard(
        "a/b",
        1,
        limits=GuardLimits(max_seconds=None, max_attempts=None, max_tokens=None, max_cost_usd=None),
    )
    guard.record_usage(tokens=10_000_000, cost_usd=999.0)
    for _ in range(50):
        guard.record_attempt()
    guard.check_time()


def test_guard_usage_accumulates() -> None:
    guard = TaskGuard("a/b", 1, limits=GuardLimits(max_tokens=1000, max_cost_usd=10.0))
    guard.record_usage(tokens=100, cost_usd=0.1)
    guard.record_usage(tokens=150, cost_usd=0.2)
    assert guard.tokens_used == 250
    assert guard.cost_usd == pytest.approx(0.3)


def test_guard_check_all_passes_when_within_limits() -> None:
    guard = TaskGuard("a/b", 1)
    guard.check_all()


def test_guard_persists_and_reloads(conn) -> None:
    guard = TaskGuard("a/b", 1, limits=GuardLimits(max_attempts=5))
    guard.record_attempt()
    guard.record_usage(tokens=42, cost_usd=0.5)
    guard.persist(conn)

    reloaded = killswitch.load_guard(conn, "a/b", 1, limits=GuardLimits(max_attempts=5))
    assert reloaded.attempts == 1
    assert reloaded.tokens_used == 42
    assert reloaded.cost_usd == pytest.approx(0.5)


def test_reloaded_guard_still_enforces_limits(conn) -> None:
    """A restart must not reset the counters and grant fresh budget."""
    guard = TaskGuard("a/b", 1, limits=GuardLimits(max_attempts=1))
    guard.record_attempt()
    guard.persist(conn)

    reloaded = killswitch.load_guard(conn, "a/b", 1, limits=GuardLimits(max_attempts=1))
    with pytest.raises(KillSwitchError, match="attempt limit"):
        reloaded.record_attempt()


def test_load_guard_defaults_when_absent(conn) -> None:
    guard = killswitch.load_guard(conn, "x/y", 99)
    assert guard.attempts == 0
    assert guard.tokens_used == 0


def test_guard_error_names_the_task() -> None:
    guard = TaskGuard("acme/widget", 7, limits=GuardLimits(max_attempts=0))
    with pytest.raises(KillSwitchError, match="acme/widget#7"):
        guard.record_attempt()


def test_remedy_names_the_database_when_db_is_the_source(conn) -> None:
    """A generic remedy would send the operator to the wrong control."""
    killswitch.set_state(conn, True)
    with pytest.raises(Exception, match="kill-switch off"):
        require_kill_switch_clear(None, None, conn)


def test_remedy_names_the_config_file_when_config_is_the_source() -> None:
    config = AppConfig(repositories=["a/b"], kill_switch=True)
    with pytest.raises(Exception, match="kill_switch: false"):
        require_kill_switch_clear(config)


def test_remedy_names_the_env_var_when_env_is_the_source(monkeypatch) -> None:
    monkeypatch.setenv(KILL_SWITCH_ENV, "1")
    with pytest.raises(Exception, match=KILL_SWITCH_ENV):
        require_kill_switch_clear()
