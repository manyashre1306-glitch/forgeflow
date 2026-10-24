"""Persistent kill switch and per-task resource guardrails (SPEC section 9).

The kill switch has three independent sources so that any one is sufficient to
stop the system: this database flag, ``kill_switch: true`` in the configuration
file, and the ``FORGEFLOW_KILL_SWITCH`` environment variable. See
:func:`forgeflow.config.kill_switch_engaged` for how they combine.

``TaskGuard`` bounds a single task's resource use: wall-clock time, model
retries, tokens, and spend. It is deliberately separate from the model client's
own budget check, because a guard should stop a run even when the accounting is
unavailable.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from forgeflow.models import KillSwitchState

KILL_SWITCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS kill_switch (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    engaged    INTEGER NOT NULL DEFAULT 0,
    reason     TEXT NOT NULL DEFAULT '',
    changed_by TEXT NOT NULL DEFAULT '',
    changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_guard (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_full_name TEXT NOT NULL,
    issue_number  INTEGER NOT NULL,
    started_at    TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    tokens_used   INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0.0,
    UNIQUE (repo_full_name, issue_number)
);
"""


class KillSwitchError(RuntimeError):
    """Raised when a run is refused because the kill switch is engaged."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(KILL_SWITCH_SCHEMA)
    conn.commit()


def read_state(conn: sqlite3.Connection) -> KillSwitchState:
    """Return the stored kill-switch state. Absent means disengaged."""
    row = conn.execute("SELECT * FROM kill_switch WHERE id = 1").fetchone()
    if row is None:
        return KillSwitchState()
    changed = row["changed_at"]
    return KillSwitchState(
        engaged=bool(row["engaged"]),
        reason=row["reason"],
        changed_by=row["changed_by"],
        changed_at=datetime.fromisoformat(changed) if changed else None,
    )


def set_state(
    conn: sqlite3.Connection,
    engaged: bool,
    *,
    reason: str = "",
    actor: str = "operator",
) -> KillSwitchState:
    """Engage or disengage the switch. Returns the new state.

    The change is written to the audit log by the caller-facing CLI, not here,
    so this function stays usable from tests without an audit dependency.
    """
    conn.execute(
        """
        INSERT INTO kill_switch (id, engaged, reason, changed_by, changed_at)
        VALUES (1, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            engaged = excluded.engaged,
            reason = excluded.reason,
            changed_by = excluded.changed_by,
            changed_at = excluded.changed_at
        """,
        (1 if engaged else 0, reason, actor, datetime.now(UTC).isoformat()),
    )
    conn.commit()
    return read_state(conn)


@dataclass
class GuardLimits:
    """Per-task ceilings. Any value of ``None`` disables that limit."""

    max_seconds: float | None = 1800.0
    max_attempts: int | None = 10
    max_tokens: int | None = 500_000
    max_cost_usd: float | None = 2.0


@dataclass
class TaskGuard:
    """Tracks and enforces one task's resource use.

    A guard is created per task and queried before each expensive step. It fails
    closed: once a limit is reached every further check raises, so a loop cannot
    keep going by ignoring a single return value.
    """

    repo_full_name: str
    issue_number: int
    limits: GuardLimits = field(default_factory=GuardLimits)
    started_monotonic: float = field(default_factory=time.monotonic)
    attempts: int = 0
    tokens_used: int = 0
    cost_usd: float = 0.0

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_monotonic

    def check_time(self) -> None:
        if self.limits.max_seconds is None:
            return
        elapsed = self.elapsed_seconds()
        if elapsed > self.limits.max_seconds:
            raise KillSwitchError(
                f"task {self.repo_full_name}#{self.issue_number} exceeded its "
                f"time limit ({elapsed:.1f}s > {self.limits.max_seconds:.1f}s)"
            )

    def check_attempts(self) -> None:
        """Raise once the attempt count exceeds the limit.

        The comparison is ``>`` and not ``>=`` so that ``max_attempts=2`` really
        does permit two attempts, matching how the token and cost ceilings work.
        """
        if self.limits.max_attempts is None:
            return
        if self.attempts > self.limits.max_attempts:
            raise KillSwitchError(
                f"task {self.repo_full_name}#{self.issue_number} exhausted its "
                f"attempt limit ({self.limits.max_attempts})"
            )

    def record_attempt(self) -> int:
        """Count one attempt and return the new total."""
        self.attempts += 1
        self.check_attempts()
        return self.attempts

    def record_usage(self, *, tokens: int = 0, cost_usd: float = 0.0) -> None:
        """Add observed usage, then check the ceilings.

        Usage is added before checking so that a single call which blows the
        budget is caught immediately rather than on the next call.
        """
        self.tokens_used += tokens
        self.cost_usd += cost_usd
        self.check_tokens()
        self.check_cost()

    def check_tokens(self) -> None:
        if self.limits.max_tokens is None:
            return
        if self.tokens_used > self.limits.max_tokens:
            raise KillSwitchError(
                f"task {self.repo_full_name}#{self.issue_number} exceeded its "
                f"token budget ({self.tokens_used} > {self.limits.max_tokens})"
            )

    def check_cost(self) -> None:
        if self.limits.max_cost_usd is None:
            return
        if self.cost_usd > self.limits.max_cost_usd:
            raise KillSwitchError(
                f"task {self.repo_full_name}#{self.issue_number} exceeded its "
                f"cost budget (${self.cost_usd:.4f} > ${self.limits.max_cost_usd:.2f})"
            )

    def check_all(self) -> None:
        """Run every check. Called before a task step begins."""
        self.check_time()
        self.check_attempts()
        self.check_tokens()
        self.check_cost()

    def persist(self, conn: sqlite3.Connection) -> None:
        """Store the guard's counters so a restart resumes rather than resets."""
        conn.execute(
            """
            INSERT INTO task_guard (
                repo_full_name, issue_number, started_at, attempts, tokens_used, cost_usd
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_full_name, issue_number) DO UPDATE SET
                attempts = excluded.attempts,
                tokens_used = excluded.tokens_used,
                cost_usd = excluded.cost_usd
            """,
            (
                self.repo_full_name,
                self.issue_number,
                datetime.now(UTC).isoformat(),
                self.attempts,
                self.tokens_used,
                self.cost_usd,
            ),
        )
        conn.commit()


def load_guard(
    conn: sqlite3.Connection,
    repo_full_name: str,
    issue_number: int,
    *,
    limits: GuardLimits | None = None,
) -> TaskGuard:
    """Rebuild a guard from stored counters, so limits survive a restart."""
    guard = TaskGuard(
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        limits=limits or GuardLimits(),
    )
    row = conn.execute(
        "SELECT * FROM task_guard WHERE repo_full_name = ? AND issue_number = ?",
        (repo_full_name, issue_number),
    ).fetchone()
    if row is not None:
        guard.attempts = int(row["attempts"])
        guard.tokens_used = int(row["tokens_used"])
        guard.cost_usd = float(row["cost_usd"])
    return guard
