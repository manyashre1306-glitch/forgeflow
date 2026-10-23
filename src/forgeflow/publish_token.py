"""The publishing credential, isolated in its own module (SPEC sections 8 and 9).

SPEC section 8 requires that discovery, analysis, and coding code cannot reach
the publishing credential. A convention is not enough: any module could read an
environment variable. So the token is read in exactly one place, this module,
and :mod:`forgeflow.publisher` is the only module allowed to import it.

``tests/test_publish_isolation.py`` enforces that with a static import scan and
an audit-hook guard that fails if any module outside the publisher reads the
variable at runtime.

The variable is ``FORGEFLOW_PUBLISH_TOKEN``, deliberately different from
``GITHUB_TOKEN``. A read-only token can be used for discovery while a separate,
narrowly scoped token is used for the single write path.
"""

from __future__ import annotations

import os

PUBLISH_TOKEN_ENV = "FORGEFLOW_PUBLISH_TOKEN"

# Modules permitted to import this module. Enforced by a test, not by trust.
ALLOWED_IMPORTERS: frozenset[str] = frozenset(
    {
        "forgeflow.publisher",
        # The CLI must be able to report whether a token is present, and to
        # construct a publisher, without ever reading the value itself.
        "forgeflow.cli",
    }
)


class PublishTokenError(RuntimeError):
    """Raised when the publishing credential is missing or unusable.

    The message never contains the token or any part of it.
    """


def read_publish_token() -> str:
    """Return the publishing token, or raise if it is absent.

    The value is returned to the caller and never logged. Callers must not store
    it on an object that is printed or serialised.
    """
    value = os.environ.get(PUBLISH_TOKEN_ENV, "")
    if not value.strip():
        raise PublishTokenError(
            f"{PUBLISH_TOKEN_ENV} is not set. Publishing requires a separate token "
            "with repository write scope. ForgeFlow will not fall back to "
            "GITHUB_TOKEN: the whole point is that the publishing credential is "
            "distinct from the read-only one."
        )
    return value.strip()


def publish_token_present() -> bool:
    """Whether a publishing token is configured, without revealing it."""
    return bool(os.environ.get(PUBLISH_TOKEN_ENV, "").strip())


def token_fingerprint(token: str) -> str:
    """A short, non-reversible label for a token, safe to log.

    Uses the length and the first two characters only. This is enough to tell
    two tokens apart in a log without disclosing either.
    """
    if not token:
        return "none"
    return f"len={len(token)} prefix={token[:2]}..."
