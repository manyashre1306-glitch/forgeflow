"""Tests for the CLI orchestration functions.

These call the plain functions directly with the injected fake client. No test
invokes the network, and no test needs a real GITHUB_TOKEN.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from forgeflow.cli import (
    ForgeFlowError,
    app,
    parse_issue_reference,
    run_discover,
    run_rank,
    run_report,
    run_shortlist,
)
from forgeflow.config import AppConfig
from forgeflow.models import DEFAULT_WEIGHTS, TaskState

REPO = "pydantic/pydantic"
runner = CliRunner()


def make_config(**overrides) -> AppConfig:
    data = {
        "repositories": [REPO],
        "weights": dict(DEFAULT_WEIGHTS),
        "skill_keywords": {"python": ["python", "typing"]},
        "exclude_labels": ["duplicate", "wontfix", "invalid"],
    }
    data.update(overrides)
    return AppConfig(**data)


def issue_payload(number: int, **overrides) -> dict:
    data = {
        "number": number,
        "title": f"Bug: python typing issue {number}",
        "body": "Steps to reproduce:\nExpected x\nActual y\nTraceback: Error\n" + "x" * 250,
        "state": "open",
        "labels": [{"name": "bug"}],
        "html_url": f"https://github.com/{REPO}/issues/{number}",
        "comments": 1,
        "assignees": [],
        "created_at": "2026-10-01T08:00:00Z",
        "updated_at": "2026-10-19T09:30:00Z",
    }
    data.update(overrides)
    return data


@pytest.fixture
def seeded_client(fake_client):
    fake_client.repositories[REPO] = {
        "full_name": REPO,
        "stargazers_count": 21000,
        "open_issues_count": 300,
        "pushed_at": "2026-10-20T12:00:00Z",
    }
    fake_client.files[REPO] = {"CONTRIBUTING.md", "tests"}
    fake_client.issues[REPO] = [
        issue_payload(1),
        issue_payload(2, labels=[{"name": "duplicate"}]),
        issue_payload(3, state="closed"),
        issue_payload(4, assignees=[{"login": "someone"}]),
    ]
    return fake_client


# --- reference parsing ------------------------------------------------------


def test_parse_issue_reference_valid() -> None:
    assert parse_issue_reference("pydantic/pydantic#1234") == ("pydantic/pydantic", 1234)


@pytest.mark.parametrize("bad", ["pydantic/pydantic", "#12", "pydantic#12", "a/b#x", "a/b#0"])
def test_parse_issue_reference_rejects_bad_input(bad: str) -> None:
    with pytest.raises(ForgeFlowError):
        parse_issue_reference(bad)


# --- run_discover -----------------------------------------------------------


def test_run_discover_stores_eligible_issues(seeded_client, db_path) -> None:
    run_discover(seeded_client, make_config(), db_path)
    ranked = run_rank(db_path, make_config())
    assert [entry.issue.number for entry in ranked] == [1]


def test_run_discover_is_idempotent(seeded_client, db_path) -> None:
    """Running the discover-and-store path twice leaves row counts unchanged."""
    from forgeflow import storage

    run_discover(seeded_client, make_config(), db_path)
    conn = storage.connect(db_path)
    try:
        first_rows = storage.count_issues(conn)
        first_excluded = storage.count_excluded_issues(conn)
    finally:
        conn.close()

    run_discover(seeded_client, make_config(), db_path)
    conn = storage.connect(db_path)
    try:
        assert storage.count_issues(conn) == first_rows
        assert storage.count_excluded_issues(conn) == first_excluded
    finally:
        conn.close()


def test_run_discover_records_exclusions_with_reasons(seeded_client, db_path) -> None:
    from forgeflow import storage

    run_discover(seeded_client, make_config(), db_path)
    conn = storage.connect(db_path)
    try:
        rows = storage.list_excluded_issues(conn)
    finally:
        conn.close()
    reasons = {row["number"]: row["reason"] for row in rows}
    assert reasons[2] == "excluded_label"
    assert reasons[3] == "closed"
    assert reasons[4] == "assigned"


def test_run_discover_deduplicates_identical_issues(seeded_client, db_path) -> None:
    """The same issue returned by two queries yields one stored row."""
    from forgeflow import storage

    seeded_client.issues[REPO] = [issue_payload(1), issue_payload(1)]
    run_discover(seeded_client, make_config(), db_path, fetch_linked_prs=False)
    conn = storage.connect(db_path)
    try:
        assert storage.count_issues(conn) == 1
    finally:
        conn.close()


def test_run_discover_skips_linked_pr_issues(fake_client, db_path) -> None:
    from forgeflow import storage

    fake_client.repositories[REPO] = {"full_name": REPO}
    fake_client.issues[REPO] = [issue_payload(5)]
    fake_client.timelines[(REPO, 5)] = [
        {"source": {"issue": {"number": 99, "state": "open", "pull_request": {"url": "x"}}}}
    ]
    run_discover(fake_client, make_config(), db_path)
    conn = storage.connect(db_path)
    try:
        assert storage.count_issues(conn) == 0
        assert storage.list_excluded_issues(conn)[0]["reason"] == "linked_open_pr"
    finally:
        conn.close()


# --- run_rank ---------------------------------------------------------------


def test_run_rank_persists_scores(seeded_client, db_path) -> None:
    from forgeflow import storage

    run_discover(seeded_client, make_config(), db_path)
    run_rank(db_path, make_config())
    conn = storage.connect(db_path)
    try:
        scores = storage.list_issue_scores(conn)
    finally:
        conn.close()
    assert len(scores) == 1
    assert scores[0].reasons


def test_run_rank_on_empty_database_returns_empty(db_path) -> None:
    assert run_rank(db_path, make_config()) == []


# --- run_report -------------------------------------------------------------


def test_run_report_writes_markdown(seeded_client, db_path, tmp_path) -> None:
    run_discover(seeded_client, make_config(), db_path)
    target = tmp_path / "report.md"
    written = run_report(db_path, make_config(), output=target)
    assert written == target
    text = target.read_text(encoding="utf-8")
    assert "ForgeFlow Phase 1 Report" in text
    assert "heuristics" in text
    assert f"{REPO}#1" in text
    assert "excluded_label" in text


# --- run_shortlist ----------------------------------------------------------


def test_run_shortlist_transitions_state(seeded_client, db_path) -> None:
    run_discover(seeded_client, make_config(), db_path)
    updated = run_shortlist(db_path, f"{REPO}#1")
    assert updated.task_state is TaskState.SHORTLISTED


def test_run_shortlist_rejects_illegal_transition(seeded_client, db_path) -> None:
    from forgeflow import storage

    run_discover(seeded_client, make_config(), db_path)
    run_shortlist(db_path, f"{REPO}#1")
    conn = storage.connect(db_path)
    try:
        storage.transition_task_state(conn, REPO, 1, TaskState.ANALYZED)
    finally:
        conn.close()
    # already past discovered; shortlisting again is an illegal same-state move
    with pytest.raises(ForgeFlowError):
        run_shortlist(db_path, f"{REPO}#1")


def test_run_shortlist_unknown_issue_fails(db_path) -> None:
    with pytest.raises(ForgeFlowError):
        run_shortlist(db_path, f"{REPO}#999")


# --- CLI surface: missing token fails safely --------------------------------


def test_discover_without_token_fails_safely(tmp_path, monkeypatch) -> None:
    """The CLI must exit non-zero with a clear message when GITHUB_TOKEN is absent."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    config_file = tmp_path / "repos.yaml"
    config_file.write_text("repositories:\n  - pydantic/pydantic\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "discover",
            "--config",
            str(config_file),
            "--db",
            str(tmp_path / "x.db"),
        ],
        env={"GITHUB_TOKEN": ""},
    )
    assert result.exit_code == 1
    assert "GITHUB_TOKEN is not set" in result.output


def test_cli_help_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("discover", "rank", "report", "shortlist"):
        assert command in result.output


def test_discover_with_bad_config_path_fails_safely(tmp_path) -> None:
    result = runner.invoke(
        app,
        ["discover", "--config", str(tmp_path / "missing.yaml"), "--db", str(tmp_path / "x.db")],
    )
    assert result.exit_code == 1
    assert "config file not found" in result.output


def test_rank_on_empty_db_reports_clearly(tmp_path) -> None:
    config_file = tmp_path / "repos.yaml"
    config_file.write_text("repositories:\n  - pydantic/pydantic\n", encoding="utf-8")
    result = runner.invoke(
        app,
        ["rank", "--config", str(config_file), "--db", str(tmp_path / "x.db")],
    )
    assert result.exit_code == 0
    assert "No issues to rank" in result.output


def test_shortlist_command_with_bad_reference_fails(tmp_path) -> None:
    result = runner.invoke(app, ["shortlist", "not-a-reference", "--db", str(tmp_path / "x.db")])
    assert result.exit_code == 1
    assert "invalid issue reference" in result.output
