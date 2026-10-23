"""Secret scrubbing for ForgeFlow (SPEC section 9).

Captured output is untrusted: a repository's test run, a model response, or an
agent transcript can all contain a credential, either because a file leaked it
or because a tool echoed it. Everything ForgeFlow writes to a log, a report, or
the database passes through :func:`redact` first.

The rule is subtractive: known secret shapes are removed, and anything that
still looks like a high-entropy credential is removed too. A false positive
costs a little legibility in a log. A false negative leaks a live token, so the
patterns lean towards over-redacting.
"""

from __future__ import annotations

import os
import re

REDACTED = "[REDACTED]"

# Environment variables whose *values* must never appear in output. The values
# are read only to redact them; they are never logged.
SECRET_ENV_VARS = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "JULES_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "FORGEFLOW_TOKEN",
    # The publishing credential (SPEC 8). Listed here so it is scrubbed from any
    # captured output even though only publisher.py ever reads it.
    "FORGEFLOW_PUBLISH_TOKEN",
)

# Token shapes, most specific first. Each entry is (name, compiled pattern).
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github_pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_\-]{30,}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_\-]{20,}")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("slack_token", re.compile(r"xox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("basic_auth_url", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")),
    (
        "auth_scheme",
        re.compile(r"(?i)\b(bearer|basic|digest|token)\s+[A-Za-z0-9._\-+/=]{6,}"),
    ),
    (
        "auth_header",
        re.compile(r"(?i)\bauthorization\b\s*[:=]\s*\S+"),
    ),
    (
        "assigned_key",
        re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token)\b\s*[:=]\s*\S+"),
    ),
    ("assigned_secret", re.compile(r"(?i)\b(secret|password|passwd|credential)\b\s*[:=]\s*\S+")),
)

# A long unbroken run of base64-ish characters with no spaces is treated as a
# possible credential. A pure hex run is excluded: a SHA-1 or SHA-256 digest is
# a content hash, not a secret, and redacting it would destroy the audit trail
# that binds an approval to an exact patch.
_ENTROPY_CANDIDATE = re.compile(r"\b[A-Za-z0-9+/=_\-]{40,}\b")
_PURE_HEX = re.compile(r"^[0-9a-fA-F]+$")


def redact(text: str, *, extra_secrets: tuple[str, ...] = ()) -> str:
    """Return ``text`` with credential-like substrings replaced.

    ``extra_secrets`` allows a caller to pass known values (for example a token
    it is about to use) so an exact match is scrubbed even if the shape is
    unusual. Empty and very short values are ignored: redacting every "1" would
    destroy the log.
    """
    if not text:
        return text

    result = text

    for value in extra_secrets:
        if value and len(value) >= 8:
            result = result.replace(value, REDACTED)

    for _name, pattern in _PATTERNS:
        result = pattern.sub(REDACTED, result)

    result = _ENTROPY_CANDIDATE.sub(_redact_unless_hex, result)
    return result


def _redact_unless_hex(match: re.Match[str]) -> str:
    """Redact a candidate, unless it is a plain hex digest.

    Called for each entropy candidate. A 40- or 64-character hex string is
    almost always a git object id or a content hash, both of which are useful
    evidence and neither of which is a credential.
    """
    value = match.group(0)
    if len(value) in (40, 64) and _PURE_HEX.match(value):
        return value
    return REDACTED


def environment_secrets() -> tuple[str, ...]:
    """Return the values of known secret environment variables.

    Read so they can be scrubbed from captured output. Callers must not log the
    returned values.
    """
    found: list[str] = []
    for name in SECRET_ENV_VARS:
        value = os.environ.get(name)
        if value and len(value) >= 8:
            found.append(value)
    return tuple(found)


def redact_all(text: str) -> str:
    """Redact ``text`` against the patterns and the current environment."""
    return redact(text, extra_secrets=environment_secrets())
