"""Input validation and per-session limits for the website.

Two jobs:

1. **Validate what a visitor types** before it reaches a network call. Owner and
   repository names are checked against GitHub's own rules, so a crafted value
   cannot become a request to somewhere else, and every text size is capped.
2. **Bound one session's spending and request count.** A visitor supplies their
   own model key, so the cost ceiling is theirs to set; this module enforces it
   per session regardless of the operator's monthly cap, and refuses once the
   cap is reached rather than silently continuing.

Nothing here holds a secret. Values are compared and counted; the key itself is
only ever passed to the client that sends it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# GitHub's own limits: owner (user or organisation) up to 39 characters, repo up
# to 100, both from [A-Za-z0-9-._]. Kept as explicit patterns so a value like
# "owner/../etc" or "owner repo" is rejected before any request is built.
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

MAX_OWNER_LENGTH = 39
MAX_REPO_LENGTH = 100
MAX_FULL_NAME_LENGTH = MAX_OWNER_LENGTH + 1 + MAX_REPO_LENGTH

# Size caps for anything a visitor pastes or that is read from a repository.
MAX_PASTED_TEXT_BYTES = 64 * 1024
MAX_FILE_BYTES = 64 * 1024
MAX_FILES_FETCHED = 20
MAX_FULL_NAME = "owner/name"


class InputError(ValueError):
    """Raised when a visitor-supplied value fails validation."""


def validate_full_name(value: str) -> str:
    """Validate and normalise an ``owner/name`` repository reference.

    Returns the trimmed value. Raises :class:`InputError` with a message safe to
    show a visitor (it never echoes the raw value back, to avoid reflecting a
    hostile string into the page).
    """
    candidate = (value or "").strip()
    if not candidate:
        raise InputError(f"enter a repository as {MAX_FULL_NAME}")
    if len(candidate) > MAX_FULL_NAME_LENGTH:
        raise InputError(f"that repository reference is too long (max {MAX_FULL_NAME_LENGTH})")
    if candidate.count("/") != 1:
        raise InputError(f"use exactly one slash, as in {MAX_FULL_NAME}")
    owner, name = candidate.split("/", 1)
    if not _OWNER_RE.match(owner):
        raise InputError("the owner part contains characters GitHub does not allow")
    if not _REPO_RE.match(name):
        raise InputError("the repository name contains characters GitHub does not allow")
    # A name of "." or "..", or containing "..", is invalid on GitHub and could
    # be used to build a request path that escapes the intended repository.
    if name in {".", ".."} or ".." in name:
        raise InputError("the repository name is not valid")
    return f"{owner}/{name}"


def validate_issue_number(value: object) -> int:
    """Validate an issue number as a positive integer."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise InputError("the issue number must be a whole number") from exc
    if number < 1:
        raise InputError("the issue number must be 1 or greater")
    return number


def cap_text(value: str, *, limit: int = MAX_PASTED_TEXT_BYTES) -> tuple[str, bool]:
    """Truncate ``value`` to ``limit`` bytes, reporting whether it was cut.

    Byte-counted, not character-counted, so a multi-byte payload cannot exceed
    the cap. Truncation is on a byte boundary and a split multi-byte character is
    dropped, so the returned string always re-encodes to at most ``limit`` bytes.
    (Decoding a partial character as a replacement would add bytes and break the
    cap, so it is dropped instead.)
    """
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return value, False
    return raw[:limit].decode("utf-8", errors="ignore"), True


@dataclass
class SessionLimits:
    """Per-session request and cost ceilings.

    Counts live here rather than in the model client so the page can show the
    visitor what is left, and so the cap is enforced even if a caller forgets to
    check the client's own budget.
    """

    max_model_calls: int = 8
    max_cost_usd: float = 0.25
    max_github_requests: int = 30
    model_calls: int = 0
    github_requests: int = 0
    cost_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_breakdown: list[tuple[str, float]] = field(default_factory=list)

    def check_model_call(self) -> None:
        """Refuse another model call when either ceiling is reached."""
        if self.model_calls >= self.max_model_calls:
            raise InputError(
                f"session model-call limit reached ({self.max_model_calls}). "
                "Reload the page to start a fresh session."
            )
        if self.cost_usd >= self.max_cost_usd:
            raise InputError(
                f"session cost limit reached (${self.cost_usd:.4f} of "
                f"${self.max_cost_usd:.2f}). Reload the page to start a fresh session."
            )

    def check_github_request(self) -> None:
        """Refuse another GitHub request when the ceiling is reached."""
        if self.github_requests >= self.max_github_requests:
            raise InputError(
                f"session GitHub request limit reached ({self.max_github_requests}). "
                "Reload the page to start a fresh session."
            )

    def record_model_call(
        self,
        *,
        cost_usd: float,
        label: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        self.model_calls += 1
        self.cost_usd = round(self.cost_usd + max(cost_usd, 0.0), 6)
        self.prompt_tokens += max(prompt_tokens, 0)
        self.completion_tokens += max(completion_tokens, 0)
        self.cost_breakdown.append((label, round(max(cost_usd, 0.0), 6)))

    def record_github_request(self, count: int = 1) -> None:
        self.github_requests += max(count, 0)

    @property
    def remaining_cost_usd(self) -> float:
        return round(max(self.max_cost_usd - self.cost_usd, 0.0), 6)
