"""Build a read-only GitHub client from the session.

Hosted mode uses the visitor's own token when they supply one and falls back to
anonymous requests when they do not, so the page works without any secret. Local
mode may use the operator's ``GITHUB_TOKEN`` from the environment.

Only the read-only :class:`~forgeflow.github_client.GitHubClient` is built here.
Nothing in this module can write to GitHub.
"""

from __future__ import annotations

from forgeflow.github_client import GitHubClient
from web.modes import ModeInfo


def build_read_client(
    mode: ModeInfo,
    *,
    visitor_token: str | None = None,
    base_url: str | None = None,
) -> GitHubClient:
    """Return a read-only client.

    Preference order for the token:

    1. the visitor's token, if they supplied one (hosted and local);
    2. in local mode only, the operator's ``GITHUB_TOKEN`` from the environment;
    3. no token, using anonymous requests.

    Anonymous mode is an explicit opt-in, so the default guard in
    ``GitHubClient`` (refusing to run without a token) is preserved for every
    other caller.
    """
    token = (visitor_token or "").strip()
    if not token and mode.is_local:
        token = _operator_token() or ""

    kwargs: dict[str, object] = {"allow_anonymous": True}
    if base_url:
        kwargs["base_url"] = base_url
    return GitHubClient(token or None, **kwargs)


def _operator_token() -> str | None:
    """Read ``GITHUB_TOKEN`` from the environment. Local mode only."""
    from forgeflow.config import load_settings

    settings = load_settings()
    value = settings.github_token
    return value.get_secret_value() if value is not None else None
