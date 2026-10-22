"""Unit tests for the SQLite storage layer."""

from __future__ import annotations

import sqlite3

import pytest

from forgeflow import storage
from forgeflow.models import IllegalTransitionError, Issue, IssueScore, Repository, TaskState


def make_issue(**overrides) -> Issue:
    data = {
        "repo_full_name": "pydantic/pydantic",
        "number": 1234,
        "title": "Bug: something is wrong",
        "body": "Steps to reproduce...",
        "labels": ["bug", "help wanted"],
        "url": "https://github.com/pydantic/pydantic/issues/1234",
        "comments_count": 3,
    }
    data.update(overrides)
    return Issue(**data)


def make_score(**overrides) -> IssueScore:
    data = {
        "repo_full_name": "pydantic/pydantic",
        "issue_number": 1234,
        "skill_relevance": 0.9,
        "issue_clarity": 0.8,
        "feasible_scope": 0.7,
        "testability": 0.6,
        "repository_health": 0.5,
        "duplicate_conflict_risk": 0.4,
        "reasons": {"skill_relevance": "matches python"},
    }
    data.update(overrides)
    return IssueScore(**data)


# --- schema and connections -------------------------------------------------


def test_init_db_creates_all_tables(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    names = {row["name"] for row in rows}
    assert {"repositories", "issues", "issue_scores", "runs", "status_changes"} <= names


def test_init_db_is_idempotent(conn: sqlite3.Connection) -> None:
    storage.init_db(conn)
    storage.init_db(conn)
    assert storage.count_issues(conn) == 0


def test_foreign_keys_are_enforced(conn: sqlite3.Connection) -> None:
    """Storing an issue for an unknown repository must fail."""
    with pytest.raises(sqlite3.IntegrityError):
        storage.upsert_issue(conn, make_issue())


def test_connect_creates_parent_directory(tmp_path) -> None:
    nested = tmp_path / "a" / "b" / "forgeflow.db"
    connection = storage.connect(nested)
    try:
        storage.init_db(connection)
        assert nested.exists()
    finally:
        connection.close()


def test_memory_connection_works(memory_conn: sqlite3.Connection) -> None:
    storage.upsert_repository(memory_conn, Repository(full_name="psf/requests"))
    assert storage.count_issues(memory_conn) == 0


# --- repository upsert ------------------------------------------------------


def test_upsert_repository_inserts(conn: sqlite3.Connection) -> None:
    storage.upsert_repository(conn, Repository(full_name="psf/requests", stars=10))
    row = conn.execute(
        "SELECT * FROM repositories WHERE full_name = ?", ("psf/requests",)
    ).fetchone()
    assert row["stars"] == 10


def test_upsert_repository_updates_in_place(conn: sqlite3.Connection) -> None:
    storage.upsert_repository(conn, Repository(full_name="psf/requests", stars=10))
    storage.upsert_repository(conn, Repository(full_name="psf/requests", stars=99))
    rows = conn.execute(
        "SELECT * FROM repositories WHERE full_name = ?", ("psf/requests",)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["stars"] == 99


# --- idempotency ------------------------------------------------------------


def test_inserting_same_issue_twice_yields_one_row(conn: sqlite3.Connection) -> None:
    """The core idempotency requirement from SPEC section 10."""
    storage.upsert_repository(conn, Repository(full_name="pydantic/pydantic"))
    storage.upsert_issue(conn, make_issue())
    storage.upsert_issue(conn, make_issue())
    assert storage.count_issues(conn) == 1


def test_second_upsert_updates_fetched_fields(conn: sqlite3.Connection) -> None:
    storage.upsert_repository(conn, Repository(full_name="pydantic/pydantic"))
    storage.upsert_issue(conn, make_issue(title="old title"))
    storage.upsert_issue(conn, make_issue(title="new title", comments_count=7))
    stored = storage.get_issue(conn, "pydantic/pydantic", 1234)
    assert stored is not None
    assert stored.title == "new title"
    assert stored.comments_count == 7
    assert storage.count_issues(conn) == 1


def test_upsert_does_not_reset_task_state(conn: sqlite3.Connection) -> None:
    """Re-running discovery must not drag an in-progress issue back to discovered."""
    storage.upsert_repository(conn, Repository(full_name="pydantic/pydantic"))
    storage.upsert_issue(conn, make_issue())
    storage.transition_task_state(conn, "pydantic/pydantic", 1234, TaskState.SHORTLISTED)
    storage.upsert_issue(conn, make_issue(title="refreshed"))
    stored = storage.get_issue(conn, "pydantic/pydantic", 1234)
    assert stored is not None
    assert stored.task_state is TaskState.SHORTLISTED


def test_many_repeated_upserts_stay_at_one_row(conn: sqlite3.Connection) -> None:
    storage.upsert_repository(conn, Repository(full_name="pydantic/pydantic"))
    for _ in range(10):
        storage.upsert_issue(conn, make_issue())
    assert storage.count_issues(conn) == 1


def test_issue_score_upsert_is_idempotent(conn: sqlite3.Connection) -> None:
    storage.upsert_repository(conn, Repository(full_name="pydantic/pydantic"))
    storage.upsert_issue(conn, make_issue())
    storage.upsert_issue_score(conn, make_score())
    storage.upsert_issue_score(conn, make_score(skill_relevance=0.1))
    rows = conn.execute("SELECT * FROM issue_scores").fetchall()
    assert len(rows) == 1
    assert rows[0]["skill_relevance"] == pytest.approx(0.1)


# --- reading back -----------------------------------------------------------


def test_round_trip_preserves_fields(conn: sqlite3.Connection) -> None:
    storage.upsert_repository(conn, Repository(full_name="pydantic/pydantic"))
    original = make_issue()
    storage.upsert_issue(conn, original)
    stored = storage.get_issue(conn, "pydantic/pydantic", 1234)
    assert stored is not None
    assert stored.title == original.title
    assert stored.labels == original.labels
    assert stored.comments_count == original.comments_count


def test_get_issue_returns_none_when_missing(conn: sqlite3.Connection) -> None:
    assert storage.get_issue(conn, "pydantic/pydantic", 999999) is None


def test_list_issues_is_ordered(conn: sqlite3.Connection) -> None:
    storage.upsert_repository(conn, Repository(full_name="psf/requests"))
    storage.upsert_repository(conn, Repository(full_name="pydantic/pydantic"))
    storage.upsert_issue(conn, make_issue(repo_full_name="psf/requests", number=2))
    storage.upsert_issue(conn, make_issue(repo_full_name="psf/requests", number=1))
    storage.upsert_issue(conn, make_issue(repo_full_name="pydantic/pydantic", number=5))
    issues = storage.list_issues(conn)
    assert [(i.repo_full_name, i.number) for i in issues] == [
        ("psf/requests", 1),
        ("psf/requests", 2),
        ("pydantic/pydantic", 5),
    ]


# --- task state transitions through storage ---------------------------------


def test_transition_updates_state(conn: sqlite3.Connection) -> None:
    storage.upsert_repository(conn, Repository(full_name="pydantic/pydantic"))
    storage.upsert_issue(conn, make_issue())
    updated = storage.transition_task_state(conn, "pydantic/pydantic", 1234, TaskState.SHORTLISTED)
    assert updated.task_state is TaskState.SHORTLISTED


def test_illegal_transition_raises_and_leaves_state_unchanged(
    conn: sqlite3.Connection,
) -> None:
    storage.upsert_repository(conn, Repository(full_name="pydantic/pydantic"))
    storage.upsert_issue(conn, make_issue())
    with pytest.raises(IllegalTransitionError):
        storage.transition_task_state(conn, "pydantic/pydantic", 1234, TaskState.PUBLISHED)
    stored = storage.get_issue(conn, "pydantic/pydantic", 1234)
    assert stored is not None
    assert stored.task_state is TaskState.DISCOVERED
    assert storage.list_status_changes(conn, "pydantic/pydantic", 1234) == []


def test_transition_to_same_state_raises(conn: sqlite3.Connection) -> None:
    storage.upsert_repository(conn, Repository(full_name="pydantic/pydantic"))
    storage.upsert_issue(conn, make_issue())
    with pytest.raises(IllegalTransitionError, match="already in state"):
        storage.transition_task_state(conn, "pydantic/pydantic", 1234, TaskState.DISCOVERED)


def test_transition_on_missing_issue_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="issue not found"):
        storage.transition_task_state(conn, "psf/requests", 1, TaskState.SHORTLISTED)


def test_status_changes_are_recorded_in_order(conn: sqlite3.Connection) -> None:
    storage.upsert_repository(conn, Repository(full_name="pydantic/pydantic"))
    storage.upsert_issue(conn, make_issue())
    storage.transition_task_state(
        conn, "pydantic/pydantic", 1234, TaskState.SHORTLISTED, reason="passed filters"
    )
    storage.transition_task_state(conn, "pydantic/pydantic", 1234, TaskState.ANALYZED)
    history = storage.list_status_changes(conn, "pydantic/pydantic", 1234)
    assert [entry["to_state"] for entry in history] == ["shortlisted", "analyzed"]
    assert [entry["from_state"] for entry in history] == ["discovered", "shortlisted"]
    assert history[0]["reason"] == "passed filters"


def test_full_happy_path_to_merged(conn: sqlite3.Connection) -> None:
    storage.upsert_repository(conn, Repository(full_name="pydantic/pydantic"))
    storage.upsert_issue(conn, make_issue())
    path = [
        TaskState.SHORTLISTED,
        TaskState.ANALYZED,
        TaskState.AWAITING_PLAN_APPROVAL,
        TaskState.IMPLEMENTING,
        TaskState.VALIDATING,
        TaskState.AWAITING_PUBLISH_APPROVAL,
        TaskState.PUBLISHED,
        TaskState.MERGED,
    ]
    for state in path:
        storage.transition_task_state(conn, "pydantic/pydantic", 1234, state)
    stored = storage.get_issue(conn, "pydantic/pydantic", 1234)
    assert stored is not None
    assert stored.task_state is TaskState.MERGED
    assert len(storage.list_status_changes(conn, "pydantic/pydantic", 1234)) == len(path)


def test_repair_loop_from_failed_validation(conn: sqlite3.Connection) -> None:
    storage.upsert_repository(conn, Repository(full_name="pydantic/pydantic"))
    storage.upsert_issue(conn, make_issue())
    for state in (
        TaskState.SHORTLISTED,
        TaskState.ANALYZED,
        TaskState.AWAITING_PLAN_APPROVAL,
        TaskState.IMPLEMENTING,
        TaskState.VALIDATING,
        TaskState.FAILED_VALIDATION,
        TaskState.IMPLEMENTING,
    ):
        storage.transition_task_state(conn, "pydantic/pydantic", 1234, state)
    stored = storage.get_issue(conn, "pydantic/pydantic", 1234)
    assert stored is not None
    assert stored.task_state is TaskState.IMPLEMENTING


# --- runs -------------------------------------------------------------------


def test_run_lifecycle(conn: sqlite3.Connection) -> None:
    run_id = storage.start_run(conn, "discover")
    assert run_id > 0
    storage.finish_run(conn, run_id, status="completed", issues_seen=42)
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row["status"] == "completed"
    assert row["issues_seen"] == 42
    assert row["finished_at"] is not None
