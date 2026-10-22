"""Tests for the read-only GitHub client.

These use ``httpx.MockTransport``, so no real network is involved: the transport
intercepts every request and returns canned responses.
"""

from __future__ import annotations

import time

import httpx
import pytest

from forgeflow.github_client import (
    GitHubClient,
    GitHubError,
    MissingTokenError,
    RateLimit,
)

TOKEN = "test-token-value"  # a fake token, used only in tests


def make_client(handler, **kwargs) -> GitHubClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    kwargs.setdefault("sleep", lambda _seconds: None)
    return GitHubClient(TOKEN, client=http_client, **kwargs)


# --- missing token ----------------------------------------------------------


def test_missing_token_raises_clear_error() -> None:
    with pytest.raises(MissingTokenError) as excinfo:
        GitHubClient(None)
    message = str(excinfo.value)
    assert "GITHUB_TOKEN is not set" in message
    assert ".env.example" in message


def test_blank_token_raises_clear_error() -> None:
    with pytest.raises(MissingTokenError):
        GitHubClient("   ")


def test_missing_token_error_is_a_github_error() -> None:
    assert issubclass(MissingTokenError, GitHubError)


# --- auth header ------------------------------------------------------------


def test_token_is_sent_as_authorization_header() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers))
        return httpx.Response(200, json={"full_name": "psf/requests"})

    client = make_client(handler)
    client.get_repository("psf/requests")
    assert seen["authorization"] == f"Bearer {TOKEN}"


def test_error_messages_never_contain_the_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "server exploded"})

    client = make_client(handler, max_retries=0)
    with pytest.raises(GitHubError) as excinfo:
        client.get_repository("psf/requests")
    assert TOKEN not in str(excinfo.value)


# --- read-only surface ------------------------------------------------------


def test_client_exposes_no_write_methods() -> None:
    """Phase 1 must not be able to modify anything on GitHub."""
    forbidden = {
        "create_issue",
        "create_pull",
        "create_pull_request",
        "comment",
        "create_comment",
        "merge",
        "push",
        "delete",
        "update",
        "edit",
    }
    public = {name for name in dir(GitHubClient) if not name.startswith("_")}
    assert not (public & forbidden)


# --- retries and rate limits ------------------------------------------------


def test_retries_on_500_then_succeeds() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(500, json={"message": "boom"})
        return httpx.Response(200, json={"full_name": "psf/requests"})

    client = make_client(handler)
    payload = client.get_repository("psf/requests")
    assert payload["full_name"] == "psf/requests"
    assert attempts["count"] == 3
    assert client.stats.retries == 2


def test_gives_up_after_max_retries() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(500, json={"message": "boom"})

    client = make_client(handler, max_retries=2)
    with pytest.raises(GitHubError, match="500"):
        client.get_repository("psf/requests")
    assert attempts["count"] == 3  # initial attempt plus two retries


def test_retries_on_network_error() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={"full_name": "psf/requests"})

    client = make_client(handler)
    payload = client.get_repository("psf/requests")
    assert payload["full_name"] == "psf/requests"


def test_waits_when_rate_limit_exhausted() -> None:
    """A 403 with remaining=0 and a future reset makes the client wait."""
    slept: list[float] = []
    attempts = {"count": 0}
    future_reset = int(time.time()) + 30

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(
                403,
                headers={
                    "x-ratelimit-limit": "30",
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": str(future_reset),
                },
                json={"message": "rate limit exceeded"},
            )
        return httpx.Response(200, json={"full_name": "psf/requests"})

    client = make_client(handler, sleep=slept.append)
    client.get_repository("psf/requests")
    assert client.stats.rate_limit_waits == 1
    assert slept  # it slept rather than hammering the API


def test_past_reset_time_falls_back_to_backoff_retry() -> None:
    """If the rate limit window already reset, retry immediately instead of waiting."""
    slept: list[float] = []
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(
                403,
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1000"},
                json={"message": "rate limit exceeded"},
            )
        return httpx.Response(200, json={"full_name": "psf/requests"})

    client = make_client(handler, sleep=slept.append)
    client.get_repository("psf/requests")
    assert client.stats.rate_limit_waits == 0
    assert client.stats.retries == 1


def test_rate_limit_wait_is_capped() -> None:
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": "99999999999",
            },
            json={"message": "rate limit exceeded"},
        )

    client = make_client(handler, sleep=slept.append, max_retries=0, max_rate_limit_wait=5.0)
    with pytest.raises(GitHubError):
        client.get_repository("psf/requests")
    assert all(wait <= 5.0 for wait in slept)


def test_404_raises_github_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    client = make_client(handler)
    with pytest.raises(GitHubError, match="not found"):
        client.get_repository("psf/does-not-exist")


# --- pagination -------------------------------------------------------------


def test_search_paginates_until_short_page() -> None:
    pages_seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(dict(request.url.params)["page"])
        pages_seen.append(page)
        if page == 1:
            items = [{"number": n} for n in range(1, 101)]
        elif page == 2:
            items = [{"number": n} for n in range(101, 151)]
        else:
            items = []
        return httpx.Response(200, json={"total_count": len(items), "items": items})

    client = make_client(handler)
    results = client.search_issues("repo:psf/requests is:issue is:open", limit=200)
    assert len(results) == 150
    assert pages_seen == [1, 2]


def test_search_respects_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        items = [{"number": n} for n in range(1, 101)]
        return httpx.Response(200, json={"total_count": 100, "items": items})

    client = make_client(handler)
    assert len(client.search_issues("q", limit=10)) == 10


def test_timeline_paginates_list_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(dict(request.url.params)["page"])
        if page == 1:
            return httpx.Response(200, json=[{"event": "x"}] * 100)
        if page == 2:
            return httpx.Response(200, json=[{"event": "y"}])
        return httpx.Response(200, json=[])

    client = make_client(handler)
    events = client.get_issue_timeline("psf/requests", 1)
    assert len(events) == 101


# --- has_file ---------------------------------------------------------------


def test_has_file_true_when_present() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "CONTRIBUTING.md"})

    client = make_client(handler)
    assert client.has_file("psf/requests", "CONTRIBUTING.md") is True


def test_has_file_false_when_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    client = make_client(handler)
    assert client.has_file("psf/requests", "CONTRIBUTING.md") is False


# --- RateLimit --------------------------------------------------------------


def test_rate_limit_from_headers() -> None:
    rate = RateLimit.from_headers(
        {"x-ratelimit-limit": "30", "x-ratelimit-remaining": "7", "x-ratelimit-reset": "100"}
    )
    assert rate.limit == 30
    assert rate.remaining == 7
    assert rate.seconds_until_reset(now=60.0) == pytest.approx(40.0)


def test_rate_limit_handles_missing_headers() -> None:
    rate = RateLimit.from_headers({})
    assert rate.limit is None
    assert rate.seconds_until_reset() == 0.0


def test_stats_count_requests() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"full_name": "psf/requests"})

    client = make_client(handler)
    client.get_repository("psf/requests")
    assert client.stats.requests == 1
