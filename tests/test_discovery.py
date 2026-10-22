"""Tests for discovery. Uses the injected fake client; no network."""

from __future__ import annotations

import pytest

from forgeflow.config import AppConfig
from forgeflow.discovery import (
    build_search_query,
    collect_linked_open_prs,
    discover_issues,
    fetch_repository,
    issue_from_api,
    parse_github_datetime,
    repository_from_api,
)
from forgeflow.models import Issue

REPO = "pydantic/pydantic"


def make_config(*repos: str, exclude_labels: list[str] | None = None) -> AppConfig:
    return AppConfig(
        repositories=list(repos) or [REPO],
        exclude_labels=exclude_labels or ["duplicate", "wontfix", "invalid"],
    )


# --- query construction -----------------------------------------------------


def test_search_query_requests_open_issues_only() -> None:
    query = build_search_query(REPO)
    assert query == "repo:pydantic/pydantic is:issue is:open"


# --- payload mapping --------------------------------------------------------


def test_issue_from_api_maps_fields(sample_issue_payload: dict) -> None:
    issue = issue_from_api(sample_issue_payload, REPO)
    assert issue.repo_full_name == REPO
    assert issue.number == 1234
    assert issue.title.startswith("Bug:")
    assert issue.labels == ["bug"]
    assert issue.comments_count == 3
    assert issue.is_pull_request is False


def test_issue_from_api_detects_pull_request(sample_issue_payload: dict) -> None:
    payload = {**sample_issue_payload, "pull_request": {"url": "https://example.invalid"}}
    assert issue_from_api(payload, REPO).is_pull_request is True


def test_issue_from_api_maps_assignees(sample_issue_payload: dict) -> None:
    payload = {**sample_issue_payload, "assignees": [{"login": "someone"}]}
    assert issue_from_api(payload, REPO).assignees == ["someone"]


def test_issue_from_api_handles_missing_body(sample_issue_payload: dict) -> None:
    payload = {**sample_issue_payload, "body": None}
    assert issue_from_api(payload, REPO).body == ""


def test_issue_from_api_requires_number(sample_issue_payload: dict) -> None:
    payload = {key: value for key, value in sample_issue_payload.items() if key != "number"}
    with pytest.raises(ValueError, match="number"):
        issue_from_api(payload, REPO)


def test_repository_from_api_maps_fields(sample_repo_payload: dict) -> None:
    repo = repository_from_api(sample_repo_payload)
    assert repo.full_name == REPO
    assert repo.stars == 21000
    assert repo.open_issues == 350
    assert repo.last_pushed_at is not None


def test_parse_github_datetime_handles_z_suffix() -> None:
    parsed = parse_github_datetime("2026-10-20T12:00:00Z")
    assert parsed is not None
    assert parsed.year == 2026


def test_parse_github_datetime_returns_none_for_garbage() -> None:
    assert parse_github_datetime("not-a-date") is None
    assert parse_github_datetime(None) is None


# --- repository metadata ----------------------------------------------------


def test_fetch_repository_detects_contributing_and_tests(fake_client) -> None:
    fake_client.repositories[REPO] = {
        "full_name": REPO,
        "stargazers_count": 100,
        "open_issues_count": 5,
        "pushed_at": "2026-10-20T12:00:00Z",
    }
    fake_client.files[REPO] = {"CONTRIBUTING.md", "tests"}
    repo = fetch_repository(fake_client, REPO)
    assert repo.has_contributing is True
    assert repo.has_tests is True


def test_fetch_repository_reports_absence(fake_client) -> None:
    fake_client.repositories[REPO] = {"full_name": REPO}
    repo = fetch_repository(fake_client, REPO)
    assert repo.has_contributing is False
    assert repo.has_tests is False


# --- full discovery pass ----------------------------------------------------


def test_discover_issues_returns_issues_and_repositories(
    fake_client, sample_issue_payload: dict
) -> None:
    fake_client.repositories[REPO] = {
        "full_name": REPO,
        "stargazers_count": 100,
        "open_issues_count": 5,
        "pushed_at": "2026-10-20T12:00:00Z",
    }
    fake_client.issues[REPO] = [sample_issue_payload]
    result = discover_issues(fake_client, make_config(REPO))
    assert len(result.repositories) == 1
    assert len(result.issues) == 1
    assert result.errors == []
    assert result.issues[0].number == 1234


def test_discover_issues_queries_only_the_allowlist(fake_client) -> None:
    fake_client.repositories["psf/requests"] = {"full_name": "psf/requests"}
    discover_issues(fake_client, make_config("psf/requests"))
    queries = [query for query, _limit in fake_client.search_calls]
    assert queries == ["repo:psf/requests is:issue is:open"]


def test_discover_issues_records_search_failure_and_continues(fake_client) -> None:
    fake_client.repositories["psf/requests"] = {"full_name": "psf/requests"}
    fake_client.repositories[REPO] = {"full_name": REPO}
    fake_client.raise_on_search.add("psf/requests")
    result = discover_issues(fake_client, make_config("psf/requests", REPO))
    assert len(result.repositories) == 2
    assert any("issue search failed" in error for error in result.errors)


def test_discover_issues_records_repository_failure_and_continues(fake_client) -> None:
    fake_client.repositories[REPO] = {"full_name": REPO}
    fake_client.raise_on_repo.add("psf/requests")
    result = discover_issues(fake_client, make_config("psf/requests", REPO))
    assert len(result.repositories) == 1
    assert any("repository metadata failed" in error for error in result.errors)


def test_discover_issues_skips_malformed_payload(fake_client) -> None:
    fake_client.repositories[REPO] = {"full_name": REPO}
    fake_client.issues[REPO] = [{"title": "no number here"}]
    result = discover_issues(fake_client, make_config(REPO))
    assert result.issues == []
    assert any("malformed" in error for error in result.errors)


def test_discover_issues_respects_per_repo_limit(fake_client, sample_issue_payload: dict) -> None:
    fake_client.repositories[REPO] = {"full_name": REPO}
    fake_client.issues[REPO] = [
        {**sample_issue_payload, "number": 1000 + index} for index in range(10)
    ]
    result = discover_issues(fake_client, make_config(REPO), per_repo_limit=3)
    assert len(result.issues) == 3


# --- linked pull requests ---------------------------------------------------


def _timeline_event(number: int, state: str, *, is_pr: bool = True) -> dict:
    source_issue: dict = {"number": number, "state": state}
    if is_pr:
        source_issue["pull_request"] = {"url": "https://example.invalid"}
    return {"event": "cross-referenced", "source": {"issue": source_issue}}


def test_collect_linked_open_prs_finds_open_pr(fake_client) -> None:
    issue = Issue(repo_full_name=REPO, number=1234, title="t")
    fake_client.timelines[(REPO, 1234)] = [_timeline_event(99, "open")]
    assert collect_linked_open_prs(fake_client, issue) == [99]


def test_collect_linked_open_prs_ignores_closed_pr(fake_client) -> None:
    issue = Issue(repo_full_name=REPO, number=1234, title="t")
    fake_client.timelines[(REPO, 1234)] = [_timeline_event(99, "closed")]
    assert collect_linked_open_prs(fake_client, issue) == []


def test_collect_linked_open_prs_ignores_plain_issue(fake_client) -> None:
    issue = Issue(repo_full_name=REPO, number=1234, title="t")
    fake_client.timelines[(REPO, 1234)] = [_timeline_event(99, "open", is_pr=False)]
    assert collect_linked_open_prs(fake_client, issue) == []


def test_collect_linked_open_prs_handles_broken_timeline(fake_client) -> None:
    issue = Issue(repo_full_name=REPO, number=1234, title="t")

    def boom(_full_name: str, _number: int) -> list[dict]:
        raise RuntimeError("timeline unavailable")

    fake_client.get_issue_timeline = boom
    assert collect_linked_open_prs(fake_client, issue) == []
