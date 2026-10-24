"""Append-only audit log for ForgeFlow (SPEC section 9).

Every security-relevant event is recorded here: state transitions, approval
decisions, validation outcomes, refusals, and kill-switch changes. Two
properties matter:

* **Append-only.** The SQLite table has triggers that reject UPDATE and DELETE,
  so a bug or a compromised code path cannot quietly rewrite history. Rows are
  never modified after they are written.
* **Scrubbed.** Every field passes through :func:`forgeflow.security.redact_all`
  before it is stored, so a credential that reached an error message does not
  reach the log.

The log is also mirrored to a JSON Lines file when a path is configured, which
survives a lost database and is easy to ship somewhere else.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from forgeflow.models import AuditEvent
from forgeflow.security import redact_all

AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    event       TEXT NOT NULL,
    repo_full_name TEXT,
    issue_number INTEGER,
    actor       TEXT NOT NULL DEFAULT 'forgeflow',
    severity    TEXT NOT NULL DEFAULT 'info',
    detail      TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}'
);

-- Append-only enforcement. Any attempt to rewrite history fails loudly rather
-- than silently succeeding.
CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: UPDATE is not permitted');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: DELETE is not permitted');
END;
"""


class AuditLogError(RuntimeError):
    """Raised when an audit write fails, or history is tampered with."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(AUDIT_SCHEMA)
    conn.commit()


def record(
    conn: sqlite3.Connection,
    event: str,
    *,
    repo_full_name: str | None = None,
    issue_number: int | None = None,
    actor: str = "forgeflow",
    severity: str = "info",
    detail: str = "",
    payload: dict | None = None,
    log_path: Path | str | None = None,
) -> AuditEvent:
    """Append one audit event. Returns the stored event.

    All text is redacted before storage. ``severity`` is one of ``info``,
    ``warning``, or ``critical``; anything else is stored as-is so an unexpected
    value is visible rather than silently coerced.
    """
    created = datetime.now(UTC)
    safe_detail = redact_all(detail)
    safe_payload = redact_all(json.dumps(payload or {}, sort_keys=True, default=str))

    try:
        cursor = conn.execute(
            """
            INSERT INTO audit_log (
                created_at, event, repo_full_name, issue_number,
                actor, severity, detail, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created.isoformat(),
                event,
                repo_full_name,
                issue_number,
                actor,
                severity,
                safe_detail,
                safe_payload,
            ),
        )
        conn.commit()
    except sqlite3.Error as exc:
        raise AuditLogError(f"failed to write audit event {event!r}: {exc}") from exc

    stored = AuditEvent(
        id=int(cursor.lastrowid or 0),
        created_at=created,
        event=event,
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        actor=actor,
        severity=severity,
        detail=safe_detail,
        payload_json=safe_payload,
    )

    if log_path is not None:
        _append_jsonl(Path(log_path), stored)

    return stored


def _append_jsonl(path: Path, event: AuditEvent) -> None:
    """Append the event to a JSON Lines file.

    A failure to write the mirror must not lose the database record, which is
    the source of truth, so the error is raised only after the row is committed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
    except OSError as exc:
        raise AuditLogError(f"failed to append to audit log {path}: {exc}") from exc


def _row_to_event(row: sqlite3.Row) -> AuditEvent:
    created = row["created_at"]
    return AuditEvent(
        id=row["id"],
        created_at=datetime.fromisoformat(created) if created else None,
        event=row["event"],
        repo_full_name=row["repo_full_name"],
        issue_number=row["issue_number"],
        actor=row["actor"],
        severity=row["severity"],
        detail=row["detail"],
        payload_json=row["payload_json"],
    )


def list_events(
    conn: sqlite3.Connection,
    *,
    repo_full_name: str | None = None,
    issue_number: int | None = None,
    event: str | None = None,
    limit: int | None = None,
) -> list[AuditEvent]:
    """Read events, newest first, with optional filters."""
    clauses: list[str] = []
    params: list[object] = []
    if repo_full_name is not None:
        clauses.append("repo_full_name = ?")
        params.append(repo_full_name)
    if issue_number is not None:
        clauses.append("issue_number = ?")
        params.append(issue_number)
    if event is not None:
        clauses.append("event = ?")
        params.append(event)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM audit_log {where} ORDER BY id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    return [_row_to_event(row) for row in rows]


def count_events(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()
    return int(row["n"]) if row else 0


def verify_append_only(conn: sqlite3.Connection) -> tuple[bool, str]:
    """Prove the append-only triggers are active.

    Returns ``(True, "")`` when an UPDATE and a DELETE are both refused. Used by
    a test and by the CLI's ``audit`` command, so the guarantee is checked rather
    than assumed.
    """
    probe = record(conn, "audit.append_only_probe", detail="trigger verification")
    try:
        conn.execute("UPDATE audit_log SET detail = 'tampered' WHERE id = ?", (probe.id,))
    except sqlite3.IntegrityError:
        pass
    else:
        return False, "UPDATE was permitted on audit_log"

    try:
        conn.execute("DELETE FROM audit_log WHERE id = ?", (probe.id,))
    except sqlite3.IntegrityError:
        return True, ""

    return False, "DELETE was permitted on audit_log"
