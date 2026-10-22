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
    AnalysisRecord,
    Issue,
    IssueScore,
    LLMUsage,
    PatchRecord,
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

CREATE TABLE IF NOT EXISTS excluded_issues (
    repo_full_name TEXT NOT NULL,
    number         INTEGER NOT NULL,
    title          TEXT NOT NULL DEFAULT '',
    url            TEXT NOT NULL DEFAULT '',
    reason         TEXT NOT NULL,
    detail         TEXT NOT NULL DEFAULT '',
    recorded_at    TEXT NOT NULL,
    PRIMARY KEY (repo_full_name, number, reason)
);

CREATE TABLE IF NOT EXISTS patches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    repo_full_name TEXT NOT NULL,
    issue_number   INTEGER NOT NULL,
    provider       TEXT NOT NULL,
    session_id     TEXT,
    patch_sha256   TEXT NOT NULL,
    diff           TEXT NOT NULL DEFAULT '',
    files_changed  INTEGER NOT NULL DEFAULT 0,
    additions      INTEGER NOT NULL DEFAULT 0,
    deletions      INTEGER NOT NULL DEFAULT 0,
    isolated       INTEGER NOT NULL DEFAULT 1,
    notes          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS analyses (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    repo_full_name TEXT NOT NULL,
    issue_number   INTEGER NOT NULL,
    model          TEXT NOT NULL,
    confidence     REAL NOT NULL DEFAULT 0.0,
    payload_json   TEXT NOT NULL,
    FOREIGN KEY (repo_full_name, issue_number)
        REFERENCES issues(repo_full_name, number)
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT NOT NULL,
    repo_full_name    TEXT NOT NULL,
    issue_number      INTEGER NOT NULL,
    purpose           TEXT NOT NULL,
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL NOT NULL DEFAULT 0.0,
    success           INTEGER NOT NULL DEFAULT 1,
    error             TEXT
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
    # Phase 3 tables live in their own modules so each owns its schema.
    from forgeflow import approval, audit, killswitch

    audit.ensure_schema(conn)
    killswitch.ensure_schema(conn)
    approval.ensure_schema(conn)


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


def upsert_excluded_issue(
    conn: sqlite3.Connection,
    *,
    repo_full_name: str,
    number: int,
    reason: str,
    detail: str = "",
    title: str = "",
    url: str = "",
) -> None:
    """Record an excluded issue. Idempotent on (repo, number, reason)."""
    conn.execute(
        """
        INSERT INTO excluded_issues (
            repo_full_name, number, title, url, reason, detail, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo_full_name, number, reason) DO UPDATE SET
            title = excluded.title,
            url = excluded.url,
            detail = excluded.detail,
            recorded_at = excluded.recorded_at
        """,
        (repo_full_name, number, title, url, reason, detail, utcnow()),
    )
    conn.commit()


def list_excluded_issues(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """Return every recorded exclusion, ordered by repository then number."""
    rows = conn.execute(
        """
        SELECT repo_full_name, number, title, url, reason, detail
        FROM excluded_issues
        ORDER BY repo_full_name, number, reason
        """
    ).fetchall()
    return [dict(row) for row in rows]


def count_excluded_issues(conn: sqlite3.Connection) -> int:
    """Return the number of recorded exclusions."""
    return int(conn.execute("SELECT COUNT(*) FROM excluded_issues").fetchone()[0])


def _row_to_score(row: sqlite3.Row) -> IssueScore:
    try:
        reasons = json.loads(row["reasons"]) if row["reasons"] else {}
    except (TypeError, ValueError):
        reasons = {}
    return IssueScore(
        repo_full_name=row["repo_full_name"],
        issue_number=row["issue_number"],
        skill_relevance=row["skill_relevance"],
        issue_clarity=row["issue_clarity"],
        feasible_scope=row["feasible_scope"],
        testability=row["testability"],
        repository_health=row["repository_health"],
        duplicate_conflict_risk=row["duplicate_conflict_risk"],
        reasons=reasons,
    )


def get_issue_score(
    conn: sqlite3.Connection, repo_full_name: str, issue_number: int
) -> IssueScore | None:
    """Return a stored score, or None."""
    row = conn.execute(
        "SELECT * FROM issue_scores WHERE repo_full_name = ? AND issue_number = ?",
        (repo_full_name, issue_number),
    ).fetchone()
    return None if row is None else _row_to_score(row)


def list_issue_scores(conn: sqlite3.Connection) -> list[IssueScore]:
    """Return every stored score, ordered by repository then issue number."""
    rows = conn.execute(
        "SELECT * FROM issue_scores ORDER BY repo_full_name, issue_number"
    ).fetchall()
    return [_row_to_score(row) for row in rows]


def list_repositories(conn: sqlite3.Connection) -> list[Repository]:
    """Return every stored repository."""
    rows = conn.execute("SELECT * FROM repositories ORDER BY full_name").fetchall()
    return [
        Repository(
            full_name=row["full_name"],
            stars=row["stars"],
            open_issues=row["open_issues"],
            has_contributing=bool(row["has_contributing"]),
            has_tests=bool(row["has_tests"]),
            last_pushed_at=row["last_pushed_at"],
        )
        for row in rows
    ]


# --------------------------------------------------------------------------
# Phase 2: LLM cost tracking
# --------------------------------------------------------------------------


def record_llm_usage(conn: sqlite3.Connection, usage: LLMUsage) -> int:
    """Insert one model call and return its row id."""
    cursor = conn.execute(
        """
        INSERT INTO llm_usage (
            created_at, repo_full_name, issue_number, purpose, model,
            prompt_tokens, completion_tokens, cost_usd, success, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            usage.created_at.isoformat() if usage.created_at else utcnow(),
            usage.repo_full_name,
            usage.issue_number,
            usage.purpose,
            usage.model,
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.cost_usd,
            1 if usage.success else 0,
            usage.error,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def _row_to_usage(row: sqlite3.Row) -> LLMUsage:
    created = row["created_at"]
    return LLMUsage(
        id=row["id"],
        created_at=datetime.fromisoformat(created) if created else None,
        repo_full_name=row["repo_full_name"],
        issue_number=row["issue_number"],
        purpose=row["purpose"],
        model=row["model"],
        prompt_tokens=row["prompt_tokens"],
        completion_tokens=row["completion_tokens"],
        cost_usd=row["cost_usd"],
        success=bool(row["success"]),
        error=row["error"],
    )


def list_llm_usage(conn: sqlite3.Connection) -> list[LLMUsage]:
    """Return every recorded model call, oldest first."""
    rows = conn.execute("SELECT * FROM llm_usage ORDER BY id").fetchall()
    return [_row_to_usage(row) for row in rows]


def spent_since(conn: sqlite3.Connection, since: datetime) -> float:
    """Total recorded cost at or after ``since``.

    Failed calls are counted: a request that errored may still have consumed
    tokens, and under-reporting spend is the unsafe direction for a spending cap.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM llm_usage WHERE created_at >= ?",
        (since.isoformat(),),
    ).fetchone()
    return float(row["total"])


def spent_in_month(conn: sqlite3.Connection, *, now: datetime | None = None) -> float:
    """Total recorded cost in the current calendar month, UTC."""
    moment = now or datetime.now(UTC)
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return spent_since(conn, start)


def total_tokens_in_month(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    """Total prompt plus completion tokens recorded this calendar month."""
    moment = now or datetime.now(UTC)
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    row = conn.execute(
        """
        SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS total
        FROM llm_usage WHERE created_at >= ?
        """,
        (start.isoformat(),),
    ).fetchone()
    return int(row["total"])


# --------------------------------------------------------------------------
# Phase 2: patches
# --------------------------------------------------------------------------


def record_patch(conn: sqlite3.Connection, patch: PatchRecord) -> int:
    """Insert one patch record and return its row id."""
    cursor = conn.execute(
        """
        INSERT INTO patches (
            created_at, repo_full_name, issue_number, provider, session_id,
            patch_sha256, diff, files_changed, additions, deletions, isolated, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            patch.created_at.isoformat() if patch.created_at else utcnow(),
            patch.repo_full_name,
            patch.issue_number,
            patch.provider,
            patch.session_id,
            patch.patch_sha256,
            patch.diff,
            patch.files_changed,
            patch.additions,
            patch.deletions,
            1 if patch.isolated else 0,
            patch.notes,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def _row_to_patch(row: sqlite3.Row) -> PatchRecord:
    created = row["created_at"]
    return PatchRecord(
        id=row["id"],
        created_at=datetime.fromisoformat(created) if created else None,
        repo_full_name=row["repo_full_name"],
        issue_number=row["issue_number"],
        provider=row["provider"],
        session_id=row["session_id"],
        patch_sha256=row["patch_sha256"],
        diff=row["diff"],
        files_changed=row["files_changed"],
        additions=row["additions"],
        deletions=row["deletions"],
        isolated=bool(row["isolated"]),
        notes=row["notes"],
    )


def list_patches(
    conn: sqlite3.Connection,
    repo_full_name: str | None = None,
    issue_number: int | None = None,
) -> list[PatchRecord]:
    """Return recorded patches, newest last, optionally filtered to one issue."""
    query = "SELECT * FROM patches"
    params: list[object] = []
    if repo_full_name is not None and issue_number is not None:
        query += " WHERE repo_full_name = ? AND issue_number = ?"
        params += [repo_full_name, issue_number]
    query += " ORDER BY id"
    rows = conn.execute(query, params).fetchall()
    return [_row_to_patch(row) for row in rows]


def latest_patch(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
) -> PatchRecord | None:
    """Return the most recent patch for one issue, or None."""
    patches = list_patches(conn, repo_full_name, issue_number)
    return patches[-1] if patches else None


# --------------------------------------------------------------------------
# Phase 2: analyses
# --------------------------------------------------------------------------


def record_analysis(conn: sqlite3.Connection, record: AnalysisRecord) -> int:
    """Insert one analysis and return its row id."""
    cursor = conn.execute(
        """
        INSERT INTO analyses (
            created_at, repo_full_name, issue_number, model, confidence, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            record.created_at.isoformat() if record.created_at else utcnow(),
            record.repo_full_name,
            record.issue_number,
            record.model,
            record.confidence,
            record.payload_json,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def _row_to_analysis(row: sqlite3.Row) -> AnalysisRecord:
    created = row["created_at"]
    return AnalysisRecord(
        id=row["id"],
        created_at=datetime.fromisoformat(created) if created else None,
        repo_full_name=row["repo_full_name"],
        issue_number=row["issue_number"],
        model=row["model"],
        confidence=row["confidence"],
        payload_json=row["payload_json"],
    )


def latest_analysis(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
) -> AnalysisRecord | None:
    """Return the most recent stored analysis for one issue, or None."""
    row = conn.execute(
        """
        SELECT * FROM analyses
        WHERE repo_full_name = ? AND issue_number = ?
        ORDER BY id DESC LIMIT 1
        """,
        (repo_full_name, issue_number),
    ).fetchone()
    return _row_to_analysis(row) if row else None


def list_analyses(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
) -> list[AnalysisRecord]:
    """Return every stored analysis for one issue, oldest first."""
    rows = conn.execute(
        """
        SELECT * FROM analyses
        WHERE repo_full_name = ? AND issue_number = ?
        ORDER BY id
        """,
        (repo_full_name, issue_number),
    ).fetchall()
    return [_row_to_analysis(row) for row in rows]
