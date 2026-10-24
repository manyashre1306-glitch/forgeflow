"""Optional scheduled discovery (SPEC section 13, Phase 4).

SPEC section 13 says "Add scheduling only after manual runs are reliable." This
module is that scheduling, and it is deliberately minimal:

* **Discovery only.** The only command it will ever run is discovery. There is
  no code path from here to publishing, validation, or a model call. The allowed
  command set is a one-element frozenset, and a test asserts this module imports
  neither the publisher nor the token module.
* **Off by default.** ``schedule.enabled`` is false in the shipped config, so
  nothing runs on a timer until a human turns it on.
* **No background daemon.** There is no thread, no subprocess, and no loop that
  sleeps. A caller invokes :func:`run_due`; if a cycle is not due it does
  nothing. This keeps behaviour observable and keeps ForgeFlow from quietly
  making API calls.
* **Kill switch honoured.** If the kill switch is engaged, a due cycle is
  refused before any client is built.

Publishing is never scheduled. SPEC section 8 requires an explicit human
approval for every write, which a timer cannot give.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from forgeflow.config import ScheduleConfig

# The complete set of commands the scheduler may run. Discovery and nothing
# else. Publishing, validation, and implementation are absent on purpose.
ALLOWED_SCHEDULED_COMMANDS: frozenset[str] = frozenset({"discover"})

SCHEDULE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schedule_state (
    name       TEXT PRIMARY KEY,
    last_run_at TEXT,
    last_status TEXT NOT NULL DEFAULT '',
    last_detail TEXT NOT NULL DEFAULT ''
);
"""


class ScheduleError(RuntimeError):
    """Raised when a scheduled cycle is refused or misconfigured."""


@dataclass(frozen=True)
class ScheduledRun:
    """The outcome of one scheduler tick."""

    ran: bool
    command: str
    reason: str = ""
    detail: str = ""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the schedule-state table. Safe to call repeatedly."""
    conn.executescript(SCHEDULE_SCHEMA)
    conn.commit()


def _last_run(conn: sqlite3.Connection, name: str) -> datetime | None:
    row = conn.execute("SELECT last_run_at FROM schedule_state WHERE name = ?", (name,)).fetchone()
    if row is None or not row["last_run_at"]:
        return None
    try:
        return datetime.fromisoformat(row["last_run_at"])
    except ValueError:
        return None


def _record_run(
    conn: sqlite3.Connection,
    name: str,
    *,
    status: str,
    detail: str,
    at: datetime | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO schedule_state (name, last_run_at, last_status, last_detail)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            last_run_at = excluded.last_run_at,
            last_status = excluded.last_status,
            last_detail = excluded.last_detail
        """,
        (name, (at or datetime.now(UTC)).isoformat(), status, detail),
    )
    conn.commit()


def is_due(
    config: ScheduleConfig,
    last_run_at: datetime | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Whether a discovery cycle is due.

    False when scheduling is disabled, or when the interval has not elapsed.
    ``last_run_at`` of None means never run, so a first cycle is due as soon as
    scheduling is enabled.
    """
    if not config.enabled:
        return False
    if last_run_at is None:
        return True
    moment = now or datetime.now(UTC)
    return moment >= last_run_at + timedelta(minutes=config.interval_minutes)


def run_due(
    conn: sqlite3.Connection,
    config: ScheduleConfig,
    *,
    discover: Callable[[], str],
    kill_switch_engaged: Callable[[], bool] | None = None,
    now: datetime | None = None,
    command: str = "discover",
) -> ScheduledRun:
    """Run one discovery cycle if due. Never runs anything else.

    ``discover`` is a zero-argument callable that performs one discovery pass and
    returns a short status string. Injecting it keeps this function testable
    without a network and keeps the scheduler free of any knowledge of how
    discovery works.

    Refuses in three cases, each with a distinct reason, so a log shows why:
    disabled, not due, or the kill switch is engaged. The kill-switch check
    happens before ``discover`` is called.
    """
    if command not in ALLOWED_SCHEDULED_COMMANDS:
        raise ScheduleError(
            f"refusing to schedule {command!r}; only {sorted(ALLOWED_SCHEDULED_COMMANDS)} "
            "may be scheduled. Publishing is never automatic (SPEC section 8)."
        )

    if not config.enabled:
        return ScheduledRun(ran=False, command=command, reason="scheduling is disabled")

    last = _last_run(conn, command)
    if not is_due(config, last, now=now):
        return ScheduledRun(ran=False, command=command, reason="not due yet")

    if kill_switch_engaged is not None and kill_switch_engaged():
        # Deliberately not recorded as a run: a refusal is not a run, and
        # recording it would delay the next real cycle by a whole interval after
        # the kill switch is disengaged.
        return ScheduledRun(ran=False, command=command, reason="kill switch engaged")

    detail = discover()
    _record_run(conn, command, status="completed", detail=detail, at=now)
    return ScheduledRun(ran=True, command=command, detail=detail)


def last_run(conn: sqlite3.Connection, command: str = "discover") -> datetime | None:
    """When the named scheduled command last ran, or None."""
    return _last_run(conn, command)


def describe_schedule(config: ScheduleConfig) -> str:
    """A one-line human summary of the schedule state."""
    if not config.enabled:
        return "scheduled discovery: disabled (this is the default)"
    return f"scheduled discovery: every {config.interval_minutes} minute(s), discovery only"
