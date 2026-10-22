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


# --- fake GitHub client -----------------------------------------------------
#
# Tests never touch the network. This fake implements the read-only
# GitHubReader protocol with in-memory data, and records every call so tests can
# assert on query construction and call counts.


class FakeGitHubClient:
    """An in-memory stand-in for GitHubClient.

    Attributes are plain dicts/lists so tests can set up exactly the responses
    they need. No HTTP is performed anywhere in this class.
    """

    def __init__(
        self,
        repositories: dict[str, dict] | None = None,
        issues: dict[str, list[dict]] | None = None,
        files: dict[str, set[str]] | None = None,
        timelines: dict[tuple[str, int], list[dict]] | None = None,
    ) -> None:
        self.repositories = repositories or {}
        self.issues = issues or {}
        self.files = files or {}
        self.timelines = timelines or {}
        self.search_calls: list[tuple[str, int]] = []
        self.repo_calls: list[str] = []
        self.file_calls: list[tuple[str, str]] = []
        self.timeline_calls: list[tuple[str, int]] = []
        self.raise_on_repo: set[str] = set()
        self.raise_on_search: set[str] = set()

    def search_issues(self, query: str, *, limit: int = 100) -> list[dict]:
        self.search_calls.append((query, limit))
        for full_name in self.raise_on_search:
            if f"repo:{full_name} " in query:
                raise RuntimeError("simulated search failure")
        for full_name, payloads in self.issues.items():
            if f"repo:{full_name} " in query:
                return list(payloads)[:limit]
        return []

    def get_repository(self, full_name: str) -> dict:
        self.repo_calls.append(full_name)
        if full_name in self.raise_on_repo:
            raise RuntimeError("simulated repository failure")
        if full_name not in self.repositories:
            raise RuntimeError(f"no fake repository data for {full_name}")
        return dict(self.repositories[full_name])

    def has_file(self, full_name: str, path: str, *, ref: str | None = None) -> bool:
        self.file_calls.append((full_name, path))
        return path in self.files.get(full_name, set())

    def get_issue_timeline(self, full_name: str, number: int) -> list[dict]:
        self.timeline_calls.append((full_name, number))
        return list(self.timelines.get((full_name, number), []))


@pytest.fixture
def fake_client() -> FakeGitHubClient:
    """A fake GitHub client with no data loaded."""
    return FakeGitHubClient()


@pytest.fixture
def sample_repo_payload() -> dict:
    return {
        "full_name": "pydantic/pydantic",
        "stargazers_count": 21000,
        "open_issues_count": 350,
        "pushed_at": "2026-10-20T12:00:00Z",
    }


@pytest.fixture
def sample_issue_payload() -> dict:
    return {
        "number": 1234,
        "title": "Bug: validation fails on nested models",
        "body": "Steps to reproduce:\n1. define nested model\n2. validate",
        "state": "open",
        "labels": [{"name": "bug"}],
        "html_url": "https://github.com/pydantic/pydantic/issues/1234",
        "comments": 3,
        "assignees": [],
        "created_at": "2026-10-01T08:00:00Z",
        "updated_at": "2026-10-19T09:30:00Z",
    }
