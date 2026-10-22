"""SQLite persistence for ForgeFlow Phase 1.

Uses the standard library ``sqlite3`` module only. All SQL lives here so the
rest of the application deals in models, not rows.

Idempotency (SPEC section 10): issues and scores are keyed on
``(repo_full_name, issue_number)``. Re-running discovery updates the fetched
fields but never duplicates a row, and never resets the pipeline's
``task_state`` backwards.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from forgeflow.models import (
    Issue,
    IssueScore,
    Repository,
    TaskState,
    validate_transition,
)

SCHEMA: str = """
CREATE TABLE IF NOT EXISTS repositories (
    full_name        TEXT PRIMARY KEY,
    stars            INTEGER NOT NULL DEFAULT 0,
    open_issues      INTEGER NOT NULL DEFAULT 0,
    has_contributing INTEGER NOT NULL DEFAULT 0,
    has_tests        INTEGER NOT NULL DEFAULT 0,
    last_pushed_at   TEXT,
    fetched_at       TEXT
);

CREATE TABLE IF NOT EXISTS issues (
    repo_full_name TEXT NOT NULL,
    number         INTEGER NOT NULL,
    title          TEXT NOT NULL,
    body           TEXT NOT NULL DEFAULT '',
    state          TEXT NOT NULL DEFAULT 'open',
    labels         TEXT NOT NULL DEFAULT '',
    url            TEXT NOT NULL DEFAULT '',
    comments_count INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT,
    updated_at     TEXT,
    task_state     TEXT NOT NULL DEFAULT 'discovered',
    PRIMARY KEY (repo_full_name, number),
    FOREIGN KEY (repo_full_name) REFERENCES repositories(full_name)
);

CREATE TABLE IF NOT EXISTS issue_scores (
    repo_full_name        TEXT NOT NULL,
    issue_number          INTEGER NOT NULL,
    skill_relevance       REAL NOT NULL,
    issue_clarity         REAL NOT NULL,
    feasible_scope        REAL NOT NULL,
    testability           REAL NOT NULL,
    repository_health     REAL NOT NULL,
    duplicate_conflict_risk REAL NOT NULL,
    reasons               TEXT NOT NULL DEFAULT '',
    scored_at             TEXT NOT NULL,
    PRIMARY KEY (repo_full_name, issue_number),
    FOREIGN KEY (repo_full_name, issue_number)
        REFERENCES issues(repo_full_name, number)
);

CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    command        TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL DEFAULT 'running',
    issues_seen    INTEGER NOT NULL DEFAULT 0,
    notes          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS status_changes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_full_name TEXT NOT NULL,
    issue_number   INTEGER NOT NULL,
    from_state     TEXT NOT NULL,
    to_state       TEXT NOT NULL,
    changed_at     TEXT NOT NULL,
    actor          TEXT NOT NULL DEFAULT 'forgeflow',
    reason         TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (repo_full_name, issue_number)
        REFERENCES issues(repo_full_name, number)
);
"""


def utcnow() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys enforced.

    ``":memory:"`` is accepted for tests.
    """
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the schema. Safe to call repeatedly."""
    conn.executescript(SCHEMA)
    conn.commit()


def upsert_repository(conn: sqlite3.Connection, repo: Repository) -> None:
    """Insert or update a repository row."""
    conn.execute(
        """
        INSERT INTO repositories (
            full_name, stars, open_issues, has_contributing, has_tests,
            last_pushed_at, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(full_name) DO UPDATE SET
            stars = excluded.stars,
            open_issues = excluded.open_issues,
            has_contributing = excluded.has_contributing,
            has_tests = excluded.has_tests,
            last_pushed_at = excluded.last_pushed_at,
            fetched_at = excluded.fetched_at
        """,
        (
            repo.full_name,
            repo.stars,
            repo.open_issues,
            int(repo.has_contributing),
            int(repo.has_tests),
            repo.last_pushed_at.isoformat() if repo.last_pushed_at else None,
            repo.fetched_at.isoformat() if repo.fetched_at else utcnow(),
        ),
    )
    conn.commit()


def upsert_issue(conn: sqlite3.Connection, issue: Issue) -> None:
    """Insert or update an issue, preserving any existing pipeline state.

    ``task_state`` is deliberately not overwritten on conflict. Re-running
    discovery must not drag an issue that is already being worked on back to
    ``discovered``. Use :func:`transition_task_state` to change state.
    """
    conn.execute(
        """
        INSERT INTO issues (
            repo_full_name, number, title, body, state, labels, url,
            comments_count, created_at, updated_at, task_state
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo_full_name, number) DO UPDATE SET
            title = excluded.title,
            body = excluded.body,
            state = excluded.state,
            labels = excluded.labels,
            url = excluded.url,
            comments_count = excluded.comments_count,
            created_at = excluded.created_at,
            updated_at = excluded.updated_at
        """,
        (
            issue.repo_full_name,
            issue.number,
            issue.title,
            issue.body,
            issue.state,
            ",".join(issue.labels),
            issue.url,
            issue.comments_count,
            issue.created_at.isoformat() if issue.created_at else None,
            issue.updated_at.isoformat() if issue.updated_at else None,
            issue.task_state.value,
        ),
    )
    conn.commit()


def upsert_issue_score(conn: sqlite3.Connection, score: IssueScore) -> None:
    """Insert or update the score row for an issue."""
    conn.execute(
        """
        INSERT INTO issue_scores (
            repo_full_name, issue_number, skill_relevance, issue_clarity,
            feasible_scope, testability, repository_health,
            duplicate_conflict_risk, reasons, scored_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo_full_name, issue_number) DO UPDATE SET
            skill_relevance = excluded.skill_relevance,
            issue_clarity = excluded.issue_clarity,
            feasible_scope = excluded.feasible_scope,
            testability = excluded.testability,
            repository_health = excluded.repository_health,
            duplicate_conflict_risk = excluded.duplicate_conflict_risk,
            reasons = excluded.reasons,
            scored_at = excluded.scored_at
        """,
        (
            score.repo_full_name,
            score.issue_number,
            score.skill_relevance,
            score.issue_clarity,
            score.feasible_scope,
            score.testability,
            score.repository_health,
            score.duplicate_conflict_risk,
            json.dumps(score.reasons, sort_keys=True),
            utcnow(),
        ),
    )
    conn.commit()


def _row_to_issue(row: sqlite3.Row) -> Issue:
    labels = [label for label in str(row["labels"]).split(",") if label]
    return Issue(
        repo_full_name=row["repo_full_name"],
        number=row["number"],
        title=row["title"],
        body=row["body"],
        state=row["state"],
        labels=labels,
        url=row["url"],
        comments_count=row["comments_count"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        task_state=TaskState(row["task_state"]),
    )


def get_issue(conn: sqlite3.Connection, repo_full_name: str, issue_number: int) -> Issue | None:
    """Return one issue, or None if it is not stored."""
    row = conn.execute(
        "SELECT * FROM issues WHERE repo_full_name = ? AND number = ?",
        (repo_full_name, issue_number),
    ).fetchone()
    return None if row is None else _row_to_issue(row)


def list_issues(conn: sqlite3.Connection) -> list[Issue]:
    """Return every stored issue, ordered by repository and number."""
    rows = conn.execute("SELECT * FROM issues ORDER BY repo_full_name, number").fetchall()
    return [_row_to_issue(row) for row in rows]


def count_issues(conn: sqlite3.Connection) -> int:
    """Return the number of stored issue rows."""
    return int(conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0])


def record_status_change(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
    from_state: TaskState,
    to_state: TaskState,
    *,
    actor: str = "forgeflow",
    reason: str = "",
) -> None:
    """Append a status change row. Does not commit."""
    conn.execute(
        """
        INSERT INTO status_changes (
            repo_full_name, issue_number, from_state, to_state,
            changed_at, actor, reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            repo_full_name,
            issue_number,
            from_state.value,
            to_state.value,
            utcnow(),
            actor,
            reason,
        ),
    )


def transition_task_state(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
    new_state: TaskState,
    *,
    actor: str = "forgeflow",
    reason: str = "",
) -> Issue:
    """Move an issue to ``new_state``, enforcing the state machine.

    Raises ``IllegalTransitionError`` if the change is not allowed, including a
    change to the state the issue is already in. Raises ``ValueError`` if the
    issue is not stored.
    """
    issue = get_issue(conn, repo_full_name, issue_number)
    if issue is None:
        raise ValueError(f"issue not found: {repo_full_name}#{issue_number}")
    validate_transition(issue.task_state, new_state)
    conn.execute(
        "UPDATE issues SET task_state = ? WHERE repo_full_name = ? AND number = ?",
        (new_state.value, repo_full_name, issue_number),
    )
    record_status_change(
        conn,
        repo_full_name,
        issue_number,
        issue.task_state,
        new_state,
        actor=actor,
        reason=reason,
    )
    conn.commit()
    updated = get_issue(conn, repo_full_name, issue_number)
    assert updated is not None
    return updated


def list_status_changes(
    conn: sqlite3.Connection, repo_full_name: str, issue_number: int
) -> list[dict[str, str]]:
    """Return the status change history for an issue, oldest first."""
    rows = conn.execute(
        """
        SELECT from_state, to_state, changed_at, actor, reason
        FROM status_changes
        WHERE repo_full_name = ? AND issue_number = ?
        ORDER BY id
        """,
        (repo_full_name, issue_number),
    ).fetchall()
    return [dict(row) for row in rows]


def start_run(conn: sqlite3.Connection, command: str) -> int:
    """Record the start of a run and return its id."""
    cursor = conn.execute(
        "INSERT INTO runs (command, started_at, status) VALUES (?, ?, 'running')",
        (command, utcnow()),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str = "completed",
    issues_seen: int = 0,
    notes: str = "",
) -> None:
    """Mark a run finished."""
    conn.execute(
        """
        UPDATE runs
        SET finished_at = ?, status = ?, issues_seen = ?, notes = ?
        WHERE id = ?
        """,
        (utcnow(), status, issues_seen, notes, run_id),
    )
    conn.commit()
