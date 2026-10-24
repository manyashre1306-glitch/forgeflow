"""The only module in ForgeFlow that performs network I/O.

Read-only by design. Phase 1 never writes to GitHub: there is no method here
that creates, edits, comments, or pushes. Any future write capability belongs in
the Phase 4 publisher behind the approval boundary, not here.

The token is supplied by the caller (normally read from the environment by
``config.Settings``) and is sent as an ``Authorization`` header. It is never
printed, logged, or included in an exception message.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

GITHUB_API = "https://api.github.com"
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_PER_PAGE = 100
MAX_RETRIES = 3

# Status codes worth retrying: rate limiting and transient server errors.
RETRY_STATUS_CODES = frozenset({403, 429, 500, 502, 503, 504})


class GitHubError(RuntimeError):
    """Raised for GitHub API failures. Never includes the token."""


class MissingTokenError(GitHubError):
    """Raised when no token is configured. Fails safely and explains how to fix."""


@dataclass(frozen=True)
class RateLimit:
    """Rate limit state parsed from response headers."""

    limit: int | None = None
    remaining: int | None = None
    reset: int | None = None

    @classmethod
    def from_headers(cls, headers: Any) -> RateLimit:
        def as_int(name: str) -> int | None:
            raw = headers.get(name) if headers else None
            try:
                return int(raw) if raw is not None else None
            except (TypeError, ValueError):
                return None

        return cls(
            limit=as_int("x-ratelimit-limit"),
            remaining=as_int("x-ratelimit-remaining"),
            reset=as_int("x-ratelimit-reset"),
        )

    def seconds_until_reset(self, now: float | None = None) -> float:
        if self.reset is None:
            return 0.0
        current = time.time() if now is None else now
        return max(0.0, float(self.reset) - current)


@dataclass
class ClientStats:
    """Counters useful for tests and reporting. Contains no secrets."""

    requests: int = 0
    retries: int = 0
    rate_limit_waits: int = 0
    last_rate_limit: RateLimit | None = None
    pages_fetched: int = 0


class GitHubReader(Protocol):
    """Read-only interface used by discovery. Implemented by the real client and by test fakes."""

    def search_issues(self, query: str, *, limit: int = ...) -> list[dict[str, Any]]: ...

    def get_repository(self, full_name: str) -> dict[str, Any]: ...

    def has_file(self, full_name: str, path: str, *, ref: str | None = ...) -> bool: ...

    def get_file_content(
        self,
        full_name: str,
        path: str,
        *,
        ref: str | None = ...,
        max_bytes: int | None = ...,
    ) -> str | None: ...

    def list_directory(self, full_name: str, path: str = ...) -> list[str]: ...

    def list_directory_entries(self, full_name: str, path: str = ...) -> list[dict[str, str]]: ...

    def get_issue_timeline(self, full_name: str, number: int) -> list[dict[str, Any]]: ...

    def get_issue(self, full_name: str, number: int) -> dict[str, Any]: ...


class GitHubClient:
    """A thin, read-only wrapper over the GitHub REST API.

    Responsibilities: authentication headers, pagination, retry with backoff, and
    honouring rate limits. It deliberately knows nothing about ForgeFlow models;
    it returns plain dicts and lets ``discovery`` map them.
    """

    def __init__(
        self,
        token: str | None,
        *,
        client: httpx.Client | None = None,
        base_url: str = GITHUB_API,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = MAX_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
        max_rate_limit_wait: float = 60.0,
        allow_anonymous: bool = False,
    ) -> None:
        if token is None or not token.strip():
            if not allow_anonymous:
                raise MissingTokenError(
                    "GITHUB_TOKEN is not set. Set it in your environment or in a .env file "
                    "(see .env.example). ForgeFlow needs a read-only token to query GitHub."
                )
            # Anonymous mode is an explicit opt-in used by the hosted website,
            # where a visitor may supply no token. Requests carry no Authorization
            # header, so GitHub applies its much lower anonymous rate limit.
            self._token = ""
        else:
            self._token = token.strip()
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._sleep = sleep
        self._max_rate_limit_wait = max_rate_limit_wait
        self.stats = ClientStats()

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- internals -----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _request(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """Perform one GET with retries, honouring rate limits.

        The token is only ever placed in the request header. It never appears in
        an exception message, which is why errors report the URL, not headers.
        """
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            self.stats.requests += 1
            try:
                response = self._client.get(url, params=params, headers=self._headers())
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    raise GitHubError(
                        f"request failed after {attempt + 1} attempts for {url}: "
                        f"{type(exc).__name__}"
                    ) from exc
                self.stats.retries += 1
                self._sleep(self._backoff_seconds(attempt))
                continue

            self.stats.last_rate_limit = RateLimit.from_headers(response.headers)

            if response.status_code in RETRY_STATUS_CODES:
                if self._should_wait_for_rate_limit(response):
                    wait = self._rate_limit_wait(response)
                    if wait > 0:
                        self.stats.rate_limit_waits += 1
                        self._sleep(wait)
                        continue
                if attempt >= self._max_retries:
                    raise GitHubError(
                        f"GitHub returned {response.status_code} for {url} after "
                        f"{attempt + 1} attempts"
                    )
                self.stats.retries += 1
                self._sleep(self._backoff_seconds(attempt))
                continue

            if response.status_code == 404:
                raise GitHubError(f"not found: {url}")
            if response.status_code >= 400:
                raise GitHubError(
                    f"GitHub returned {response.status_code} for {url}: "
                    f"{self._safe_message(response)}"
                )
            return response

        raise GitHubError(f"request failed for {url}: {type(last_error).__name__}")

    @staticmethod
    def _safe_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return "unparseable response body"
        if isinstance(payload, dict) and "message" in payload:
            return str(payload["message"])
        return "no message field in response"

    @staticmethod
    def _backoff_seconds(attempt: int) -> float:
        return float(2**attempt)

    @staticmethod
    def _should_wait_for_rate_limit(response: httpx.Response) -> bool:
        if response.headers.get("x-ratelimit-remaining") == "0":
            return True
        return response.status_code == 429

    def _rate_limit_wait(self, response: httpx.Response) -> float:
        rate = RateLimit.from_headers(response.headers)
        wait = rate.seconds_until_reset()
        if wait <= 0:
            retry_after = response.headers.get("retry-after")
            try:
                wait = float(retry_after) if retry_after is not None else 0.0
            except ValueError:
                wait = 0.0
        # Do not block indefinitely in an unattended run.
        return min(wait, self._max_rate_limit_wait)

    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        response = self._request(url, params)
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubError(f"GitHub returned non-JSON for {url}") from exc

    def _paginate(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        max_items: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield items across pages using the Link header, or list length for search."""
        page = 1
        yielded = 0
        base_params = dict(params or {})
        while True:
            page_params = {**base_params, "per_page": DEFAULT_PER_PAGE, "page": page}
            payload = self._get_json(url, page_params)
            self.stats.pages_fetched += 1

            if isinstance(payload, dict):
                items = payload.get("items", [])
            elif isinstance(payload, list):
                items = payload
            else:
                raise GitHubError(f"unexpected payload type for {url}: {type(payload).__name__}")

            if not items:
                return
            for item in items:
                if max_items is not None and yielded >= max_items:
                    return
                yielded += 1
                yield item
            if len(items) < DEFAULT_PER_PAGE:
                return
            page += 1

    # -- read-only API -------------------------------------------------------

    def search_issues(self, query: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """Search issues. ``limit`` caps the number of items returned."""
        return list(
            self._paginate(
                f"{self._base_url}/search/issues",
                {"q": query, "sort": "updated", "order": "desc"},
                max_items=limit,
            )
        )

    def get_repository(self, full_name: str) -> dict[str, Any]:
        """Fetch repository metadata."""
        payload = self._get_json(f"{self._base_url}/repos/{full_name}")
        if not isinstance(payload, dict):
            raise GitHubError(f"unexpected repository payload for {full_name}")
        return payload

    def has_file(self, full_name: str, path: str, *, ref: str | None = None) -> bool:
        """Return True if ``path`` exists in the repository.

        Uses the contents endpoint, which returns 404 when the path is absent.
        """
        params = {"ref": ref} if ref else None
        try:
            self._get_json(f"{self._base_url}/repos/{full_name}/contents/{path}", params)
        except GitHubError as exc:
            if "not found" in str(exc):
                return False
            raise
        return True

    def get_file_content(
        self,
        full_name: str,
        path: str,
        *,
        ref: str | None = None,
        max_bytes: int | None = None,
    ) -> str | None:
        """Read one file's text from the contents API. Read-only.

        Returns ``None`` when the path is absent, is a directory, or is not
        valid UTF-8 text. The contents endpoint returns base64 for a file under
        1 MB; a larger file comes back with ``encoding="none"`` and no body, and
        is treated as absent so a caller cannot be handed a truncated file that
        looks complete.

        ``max_bytes`` caps the decoded size; a file that would exceed it is
        refused (``None``) rather than silently truncated, so the caller's size
        cap is honest.
        """
        import base64

        params = {"ref": ref} if ref else None
        try:
            payload = self._get_json(f"{self._base_url}/repos/{full_name}/contents/{path}", params)
        except GitHubError as exc:
            if "not found" in str(exc):
                return None
            raise
        if not isinstance(payload, dict):
            # A list means the path is a directory, not a file.
            return None
        if payload.get("encoding") != "base64":
            return None
        content = payload.get("content")
        if not isinstance(content, str):
            return None
        try:
            raw = base64.b64decode(content, validate=False)
        except Exception:
            return None
        if max_bytes is not None and len(raw) > max_bytes:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def list_directory(self, full_name: str, path: str = "") -> list[str]:
        """List entry names in a repository directory.

        Returns an empty list when the path does not exist, so callers can treat
        "missing" and "empty" the same way. Read-only.
        """
        return [entry["name"] for entry in self.list_directory_entries(full_name, path)]

    def list_directory_entries(self, full_name: str, path: str = "") -> list[dict[str, str]]:
        """List a directory's entries as ``{"name": ..., "type": ...}`` dicts.

        ``type`` is GitHub's own value (``file``, ``dir``, ``symlink``,
        ``submodule``). The website's hosted context builder needs the type to
        walk a tree without cloning; name-based guessing misclassifies a
        directory that contains a dot. Returns an empty list for a missing path.
        """
        url = f"{self._base_url}/repos/{full_name}/contents"
        if path:
            url = f"{url}/{path}"
        try:
            payload = self._get_json(url)
        except GitHubError as exc:
            if "not found" in str(exc):
                return []
            raise
        if not isinstance(payload, list):
            return []
        entries: list[dict[str, str]] = []
        for entry in payload:
            if isinstance(entry, dict) and "name" in entry:
                entries.append({"name": str(entry["name"]), "type": str(entry.get("type", "file"))})
        return entries

    def get_issue(self, full_name: str, number: int) -> dict[str, Any]:
        """Fetch a single issue by number. Read-only.

        Used by the website to read an issue's title and body without cloning.
        Returns the raw payload; the caller maps it with ``discovery.issue_from_api``.
        """
        payload = self._get_json(f"{self._base_url}/repos/{full_name}/issues/{number}")
        if not isinstance(payload, dict):
            raise GitHubError(f"unexpected issue payload for {full_name}#{number}")
        return payload

    def get_issue_timeline(self, full_name: str, number: int) -> list[dict[str, Any]]:
        """Fetch an issue's timeline, used to detect linked pull requests."""
        return list(
            self._paginate(
                f"{self._base_url}/repos/{full_name}/issues/{number}/timeline",
                {"per_page": DEFAULT_PER_PAGE},
            )
        )

    def issue_exists(self, full_name: str, number: int) -> bool:
        """Whether an issue or PR with this number exists. Read-only.

        Used to verify a referenced issue before a closing keyword is claimed.
        A 404 is a definite "no"; any other failure is raised so the caller can
        fail closed rather than treat an error as absence.
        """
        try:
            self._get_json(f"{self._base_url}/repos/{full_name}/issues/{number}")
        except GitHubError as exc:
            if "not found" in str(exc):
                return False
            raise
        return True

    def get_pull_request(self, full_name: str, number: int) -> dict[str, Any]:
        """Fetch one pull request's current state. Read-only."""
        payload = self._get_json(f"{self._base_url}/repos/{full_name}/pulls/{number}")
        if not isinstance(payload, dict):
            raise GitHubError(f"unexpected pull request payload for {full_name}#{number}")
        return payload

    def list_pull_request_reviews(self, full_name: str, number: int) -> list[dict[str, Any]]:
        """Fetch every review submission on a pull request. Read-only."""
        return list(self._paginate(f"{self._base_url}/repos/{full_name}/pulls/{number}/reviews"))

    def list_pull_request_review_comments(
        self, full_name: str, number: int
    ) -> list[dict[str, Any]]:
        """Fetch every inline review comment on a pull request. Read-only."""
        return list(self._paginate(f"{self._base_url}/repos/{full_name}/pulls/{number}/comments"))
