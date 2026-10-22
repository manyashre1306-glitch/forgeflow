"""Shared pytest fixtures.

Tests use a real SQLite database in a temporary directory, not a mock. The
storage layer is thin enough that testing it for real is both simpler and more
meaningful than stubbing it out.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from forgeflow import storage


@pytest.fixture
def db_path(tmp_path) -> str:
    """Path to a temporary SQLite file that is deleted with the test."""
    return str(tmp_path / "forgeflow_test.db")


@pytest.fixture
def conn(db_path: str) -> Iterator[sqlite3.Connection]:
    """An initialized connection to a temporary SQLite database."""
    connection = storage.connect(db_path)
    storage.init_db(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def memory_conn() -> Iterator[sqlite3.Connection]:
    """An initialized in-memory SQLite connection."""
    connection = storage.connect(":memory:")
    storage.init_db(connection)
    try:
        yield connection
    finally:
        connection.close()
