"""Run-mode resolution and the hosted/local capability gate.

``FORGEFLOW_WEB_MODE`` selects the mode. ``hosted`` is the default when the
variable is unset, because the safe assumption for a public deployment is that
it *is* public. A missing or misspelled value therefore degrades to the stricter
mode rather than the more powerful one.

The two modes differ in exactly three capabilities, and each is refused in code
by a function here, not by a page deciding not to draw a button:

* reading the local database,
* executing repository code through the sandbox,
* importing the publisher or the publishing token.

Tests call these guards directly, so a page that forgot to check still cannot
reach a hosted-forbidden action.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum

MODE_ENV = "FORGEFLOW_WEB_MODE"


class WebMode(StrEnum):
    """Which environment the website is running in."""

    HOSTED = "hosted"
    LOCAL = "local"


class HostedModeRefused(RuntimeError):
    """Raised when an action is unavailable in hosted mode.

    A distinct type (rather than a bare ``RuntimeError``) so a caller can tell a
    policy refusal apart from an operational failure, and so tests can assert on
    the exact reason.
    """


@dataclass(frozen=True)
class ModeInfo:
    """The resolved mode plus the text the banner and the docs both use."""

    mode: WebMode
    source: str
    """Where the mode came from: the environment variable, or the default."""

    @property
    def is_hosted(self) -> bool:
        return self.mode is WebMode.HOSTED

    @property
    def is_local(self) -> bool:
        return self.mode is WebMode.LOCAL

    @property
    def banner(self) -> str:
        """One line describing the active mode, shown on every page."""
        if self.is_hosted:
            return (
                "Mode: HOSTED (public server). No server-side secrets, no local "
                "database, and repository code is never executed here. Your own "
                "keys stay in this browser session only."
            )
        return (
            "Mode: LOCAL (your machine). The real database and, if you choose, the "
            "real publishing token may be used, only through ForgeFlow's existing "
            "guards. Never expose this mode to the public internet."
        )


def resolve_mode(env: dict[str, str] | None = None) -> ModeInfo:
    """Resolve the active mode from ``FORGEFLOW_WEB_MODE``.

    ``env`` defaults to ``os.environ`` and exists so tests can pass a controlled
    mapping instead of mutating the real environment. An unset or unrecognised
    value yields ``hosted``: the stricter mode.
    """
    environ = os.environ if env is None else env
    raw = environ.get(MODE_ENV, "").strip().lower()
    if raw == WebMode.LOCAL.value:
        return ModeInfo(mode=WebMode.LOCAL, source=f"{MODE_ENV}={raw}")
    if raw == WebMode.HOSTED.value:
        return ModeInfo(mode=WebMode.HOSTED, source=f"{MODE_ENV}={raw}")
    source = "default (unset)" if not raw else f"default (unrecognised {MODE_ENV}={raw!r})"
    return ModeInfo(mode=WebMode.HOSTED, source=source)


def require_local(mode: ModeInfo, capability: str) -> None:
    """Raise unless ``mode`` is local. ``capability`` names what was requested."""
    if mode.is_hosted:
        raise HostedModeRefused(
            f"{capability} is unavailable in hosted mode. Hosted mode never reads "
            "the local database, never executes repository code, and never imports "
            "the publisher. Run the website locally "
            f"({MODE_ENV}=local) to use it."
        )


def require_hosted_context_allowed(mode: ModeInfo) -> None:
    """Refuse hosted-mode access to the local database explicitly.

    Named separately from :func:`require_local` so a reader (and a test) can see
    that "no local database in hosted mode" is its own rule, not a side effect
    of a generic check.
    """
    if mode.is_hosted:
        raise HostedModeRefused(
            "hosted mode must not read the local database (data/forgeflow.db). "
            "Use local mode for database-backed pages."
        )
