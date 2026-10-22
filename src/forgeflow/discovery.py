"""Issue discovery from an allowlist of GitHub repositories.

Reads only. For each repository in the allowlist this module:

1. fetches repository metadata (stars, open issues, last push, CONTRIBUTING,
   and whether a tests directory is present);
2. searches open issues;
3. converts raw API payloads into validated ``Issue`` models.

The GitHub client is injected, so tests supply a fake and no network is touched.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from forgeflow.config import AppConfig
from forgeflow.github_client import GitHubReader
from forgeflow.models import Issue, Repository

# Files that indicate contribution guidance exists.
CONTRIBUTING_PATHS: tuple[str, ...] = (
    "CONTRIBUTING.md",
    "CONTRIBUTING.rst",
    "CONTRIBUTING.txt",
    ".github/CONTRIBUTING.md",
)

# Paths that indicate a test suite exists.
TEST_PATHS: tuple[str, ...] = ("tests", "test", "tox.ini")


def parse_github_datetime(value: Any) -> datetime | None:
    """Parse a GitHub ISO 8601 timestamp. Returns None if absent or unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_search_query(full_name: str) -> str:
    """Build the search query for open issues in one repository.

    ``is:issue`` excludes pull requests at the API level. Pull request filtering
    is repeated in dedupe as a defence in depth, because the search index can
    return items that are actually PRs.
    """
    return f"repo:{full_name} is:issue is:open"


def issue_from_api(payload: dict[str, Any], repo_full_name: str) -> Issue:
    """Convert one search result into an ``Issue``.

    ``is_pull_request`` is taken from the API's ``pull_request`` marker when
    present, so dedupe can drop PRs even if the search query let one through.
    """
    labels = [
        str(label.get("name", "")) for label in payload.get("labels", []) if isinstance(label, dict)
    ]
    assignees = [
        str(user.get("login", ""))
        for user in payload.get("assignees", [])
        if isinstance(user, dict)
    ]
    number = payload.get("number")
    if not isinstance(number, int):
        raise ValueError(f"issue payload missing an integer 'number' for {repo_full_name}")

    return Issue(
        repo_full_name=repo_full_name,
        number=number,
        title=str(payload.get("title") or "(no title)"),
        body=str(payload.get("body") or ""),
        state=str(payload.get("state") or "open"),
        labels=labels,
        url=str(payload.get("html_url") or ""),
        comments_count=int(payload.get("comments") or 0),
        assignees=assignees,
        is_pull_request="pull_request" in payload,
        created_at=parse_github_datetime(payload.get("created_at")),
        updated_at=parse_github_datetime(payload.get("updated_at")),
    )


def repository_from_api(payload: dict[str, Any]) -> Repository:
    """Convert a repository API payload into a ``Repository``."""
    full_name = str(payload.get("full_name") or "")
    if not full_name:
        raise ValueError("repository payload missing 'full_name'")
    return Repository(
        full_name=full_name,
        stars=int(payload.get("stargazers_count") or 0),
        open_issues=int(payload.get("open_issues_count") or 0),
        last_pushed_at=parse_github_datetime(payload.get("pushed_at")),
    )


@dataclass
class DiscoveryResult:
    """Outcome of one discovery pass."""

    repositories: list[Repository] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def fetch_repository(
    client: GitHubReader,
    full_name: str,
    *,
    detect_tests: bool = True,
) -> Repository:
    """Fetch repository metadata, including CONTRIBUTING and tests detection."""
    payload = client.get_repository(full_name)
    repo = repository_from_api(payload)

    has_contributing = any(client.has_file(full_name, path) for path in CONTRIBUTING_PATHS)
    has_tests = False
    if detect_tests:
        has_tests = any(client.has_file(full_name, path) for path in TEST_PATHS)

    return repo.model_copy(update={"has_contributing": has_contributing, "has_tests": has_tests})


def discover_issues(
    client: GitHubReader,
    config: AppConfig,
    *,
    per_repo_limit: int = 100,
    detect_tests: bool = True,
) -> DiscoveryResult:
    """Discover issues for every repository in the allowlist.

    One repository failing does not abort the run; the error is recorded and
    discovery continues with the rest of the allowlist.
    """
    result = DiscoveryResult()
    for full_name in config.repositories:
        try:
            result.repositories.append(
                fetch_repository(client, full_name, detect_tests=detect_tests)
            )
        except Exception as exc:  # one bad repo must not kill the run
            result.errors.append(f"{full_name}: repository metadata failed: {type(exc).__name__}")
            continue

        try:
            payloads = client.search_issues(build_search_query(full_name), limit=per_repo_limit)
        except Exception as exc:  # a failed search is recorded, not fatal
            result.errors.append(f"{full_name}: issue search failed: {type(exc).__name__}")
            continue

        for payload in payloads:
            try:
                result.issues.append(issue_from_api(payload, full_name))
            except Exception as exc:  # a malformed payload is skipped, not fatal
                result.errors.append(
                    f"{full_name}: skipped malformed issue payload: {type(exc).__name__}"
                )
    return result


def collect_linked_open_prs(client: GitHubReader, issue: Issue) -> list[int]:
    """Return the numbers of open pull requests linked from an issue's timeline.

    Reads the timeline for cross-reference events pointing at open PRs. Failures
    are not fatal: an issue whose timeline cannot be read is treated as having no
    linked PRs, and dedupe still applies the other filters.
    """
    try:
        events: Iterable[dict[str, Any]] = client.get_issue_timeline(
            issue.repo_full_name, issue.number
        )
    except Exception:  # timeline is best-effort
        return []

    linked: list[int] = []
    for event in events:
        source = event.get("source")
        if not isinstance(source, dict):
            continue
        source_issue = source.get("issue")
        if not isinstance(source_issue, dict):
            continue
        if "pull_request" not in source_issue:
            continue
        if str(source_issue.get("state")) != "open":
            continue
        number = source_issue.get("number")
        if isinstance(number, int) and number not in linked:
            linked.append(number)
    return linked
