"""Coding-agent adapter for ForgeFlow Phase 2 (SPEC sections 3 and 4).

The interface is :class:`CodingAgent`, with one method, ``run_task``. Provider
specifics live in :class:`JulesAgent` and nowhere else, so swapping providers
means adding a class rather than editing call sites.

**This module never publishes anything.** Jules can open pull requests on its
own when ``automationMode`` is set, so that field is deliberately never sent.
Session creation is still an external write (it creates a session in Google's
infrastructure), which is why :class:`JulesAgent` is constructed with an
explicit opt-in flag and is not reachable from the default code path.

The honest limitation, established in ``docs/agent-evaluation.md``: no
``JULES_API_KEY`` exists in this environment and one cannot be created without
an interactive browser session. :class:`JulesAgent` therefore **fails closed**:
with no key it raises :class:`AgentUnavailableError` before any request is made,
rather than silently pretending to work. :class:`MockAgent` is what the tests
and the local demo use.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from forgeflow.models import RepoContext
from forgeflow.prompts import build_agent_task

JULES_BASE_URL = "https://jules.googleapis.com/v1alpha"
JULES_API_KEY_ENV = "JULES_API_KEY"

# Documented session states, used only for polling decisions.
TERMINAL_SESSION_STATES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})


class AgentError(RuntimeError):
    """Base class for coding-agent failures."""


class AgentUnavailableError(AgentError):
    """Raised when a provider cannot run, for a reason the caller cannot fix.

    Missing credentials and a missing binary are the two cases. The message
    states the remedy so the operator is not left guessing.
    """


class AgentRunError(AgentError):
    """Raised when a provider ran but did not produce a usable result."""


@dataclass(frozen=True)
class AgentCommand:
    """One command the agent reported running, with its real output."""

    command: str
    output: str
    exit_code: int | None = None


@dataclass(frozen=True)
class AgentResult:
    """The outcome of one agent task.

    ``diff`` is the unified diff the agent produced, or ``None`` when the agent
    made no changes. ``session_id`` identifies the remote session so a human can
    inspect it. Nothing here has been published anywhere.
    """

    provider: str
    session_id: str | None
    diff: str | None
    commands: list[AgentCommand] = field(default_factory=list)
    summary: str = ""
    published: bool = False
    raw_state: str | None = None

    @property
    def has_changes(self) -> bool:
        return bool(self.diff and self.diff.strip())


@runtime_checkable
class CodingAgent(Protocol):
    """The single interface ForgeFlow uses to drive a coding agent."""

    @property
    def name(self) -> str:
        """Short provider name, for reports and storage."""
        ...

    def is_available(self) -> tuple[bool, str]:
        """Return (available, reason). Never raises."""
        ...

    def run_task(self, context: RepoContext, plan: str, *, timeout: float = 600.0) -> AgentResult:
        """Run one implementation task and return its result.

        Implementations must not publish anything to a remote.
        """
        ...


# --------------------------------------------------------------------------
# Jules
# --------------------------------------------------------------------------


class JulesAgent:
    """Coding agent backed by the documented Jules REST API.

    Only read-and-create-session calls are made. ``automationMode`` is never
    sent, so Jules never opens a pull request on its own.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = JULES_BASE_URL,
        source: str | None = None,
        poll_interval: float = 5.0,
        timeout: float = 600.0,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get(JULES_API_KEY_ENV)
        self._base_url = base_url.rstrip("/")
        self._source = source
        self._poll_interval = poll_interval
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "jules"

    def is_available(self) -> tuple[bool, str]:
        """Report whether a key is present. Does not make a network call."""
        if not self._api_key:
            return (
                False,
                f"{JULES_API_KEY_ENV} is not set. Jules API keys can only be "
                "created in the Jules web app (jules.google.com/settings), so "
                "this provider cannot run unattended in this environment.",
            )
        return True, "JULES_API_KEY is set."

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Make one authenticated API call. Raises AgentError on failure."""
        if not self._api_key:
            raise AgentUnavailableError(
                f"{JULES_API_KEY_ENV} is not set; refusing to call the Jules API."
            )
        url = f"{self._base_url}/{path.lstrip('/')}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("X-Goog-Api-Key", self._api_key)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise AgentRunError(
                f"Jules API {method} {path} failed: HTTP {exc.code} {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise AgentRunError(f"Jules API {method} {path} unreachable: {exc.reason}") from exc
        if not raw:
            return {}
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise AgentRunError(f"Jules API {method} {path} returned unexpected JSON")
        return parsed

    def run_task(
        self,
        context: RepoContext,
        plan: str,
        *,
        timeout: float = 600.0,
    ) -> AgentResult:
        """Create a session, poll it, and return the produced diff.

        ``requirePlanApproval`` is left at its default (auto-approve) because
        ForgeFlow already gates implementation behind its own human approval of
        the plan; adding a second remote approval step would stall unattended
        runs without adding a safety property we do not already enforce locally.
        ``automationMode`` is never set, so no PR is created.
        """
        available, reason = self.is_available()
        if not available:
            raise AgentUnavailableError(reason)

        source = self._source or context.repo_full_name
        payload: dict[str, object] = {
            "prompt": build_agent_task(context, plan),
            "title": f"ForgeFlow: {context.repo_full_name}#{context.issue_number}",
            "sourceContext": {
                "source": source,
                "githubRepoContext": {"startingBranch": "main"},
            },
        }

        created = self._request("POST", "sessions", payload)
        session_name = created.get("name")
        if not isinstance(session_name, str):
            raise AgentRunError("Jules API did not return a session name")
        session_id = str(created.get("id") or session_name.rsplit("/", 1)[-1])

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            activities = self._request("GET", f"{session_name}/activities")
            diff, commands, state = self._parse_activities(activities)
            if state in TERMINAL_SESSION_STATES:
                return AgentResult(
                    provider=self.name,
                    session_id=session_id,
                    diff=diff,
                    commands=commands,
                    summary=f"Jules session {state}",
                    published=False,
                    raw_state=state,
                )
            time.sleep(self._poll_interval)

        raise AgentRunError(f"Jules session {session_id} did not finish within {timeout}s")

    @staticmethod
    def _parse_activities(
        payload: dict[str, object],
    ) -> tuple[str | None, list[AgentCommand], str | None]:
        """Extract diff, commands, and state from an activities response."""
        activities = payload.get("activities")
        if not isinstance(activities, list):
            return None, [], None

        diff: str | None = None
        commands: list[AgentCommand] = []
        state: str | None = None

        for activity in activities:
            if not isinstance(activity, dict):
                continue
            for key in ("sessionCompleted", "sessionFailed", "sessionCancelled"):
                if key in activity:
                    state = {
                        "sessionCompleted": "COMPLETED",
                        "sessionFailed": "FAILED",
                        "sessionCancelled": "CANCELLED",
                    }[key]
            artifacts = activity.get("artifacts")
            if not isinstance(artifacts, list):
                continue
            for artifact in artifacts:
                if not isinstance(artifact, dict):
                    continue
                change_set = artifact.get("changeSet")
                if isinstance(change_set, dict):
                    git_patch = change_set.get("gitPatch")
                    if isinstance(git_patch, dict):
                        patch = git_patch.get("unidiffPatch")
                        if isinstance(patch, str) and patch.strip():
                            diff = patch
                bash_output = artifact.get("bashOutput")
                if isinstance(bash_output, dict):
                    command = bash_output.get("command")
                    output = bash_output.get("output")
                    if isinstance(command, str):
                        exit_code = bash_output.get("exitCode")
                        commands.append(
                            AgentCommand(
                                command=command,
                                output=output if isinstance(output, str) else "",
                                exit_code=exit_code if isinstance(exit_code, int) else None,
                            )
                        )
        return diff, commands, state


# --------------------------------------------------------------------------
# Mock
# --------------------------------------------------------------------------


class MockAgent:
    """Deterministic in-process agent for tests and local demos.

    It makes no network calls and produces a real unified diff, so the whole
    downstream pipeline (diff capture, hashing, review report) is exercised for
    real rather than stubbed. It cannot implement anything: the diff is fixed at
    construction, which is the point.
    """

    def __init__(
        self,
        *,
        diff: str | None = None,
        commands: list[AgentCommand] | None = None,
        available: bool = True,
        reason: str = "MockAgent is always available.",
    ) -> None:
        self._diff = diff if diff is not None else DEFAULT_MOCK_DIFF
        self._commands = commands if commands is not None else []
        self._available = available
        self._reason = reason
        self.calls: list[tuple[str, int]] = []

    @property
    def name(self) -> str:
        return "mock"

    def is_available(self) -> tuple[bool, str]:
        return self._available, self._reason

    def run_task(
        self,
        context: RepoContext,
        plan: str,
        *,
        timeout: float = 600.0,
    ) -> AgentResult:
        self.calls.append((context.repo_full_name, context.issue_number))
        if not self._available:
            raise AgentUnavailableError(self._reason)
        return AgentResult(
            provider=self.name,
            session_id="mock-session",
            diff=self._diff,
            commands=list(self._commands),
            summary="MockAgent produced a deterministic patch.",
            published=False,
            raw_state="COMPLETED",
        )


DEFAULT_MOCK_DIFF = """diff --git a/widget/parser.py b/widget/parser.py
index 1111111..2222222 100644
--- a/widget/parser.py
+++ b/widget/parser.py
@@ -1,2 +1,5 @@
 def parse(value):
-    return int(value)
+    try:
+        return int(value)
+    except ValueError as exc:
+        raise ValueError(f"not an integer: {value!r}") from exc
"""


def get_agent(
    provider: str,
    *,
    allow_external: bool = False,
    **kwargs: object,
) -> CodingAgent:
    """Return a coding agent by name.

    ``allow_external`` must be set explicitly to construct a provider that talks
    to a remote service. The default path can only ever return a local agent, so
    an accidental configuration change cannot start making network calls.
    """
    if provider == "mock":
        return MockAgent(**kwargs)  # type: ignore[arg-type]
    if provider == "jules":
        if not allow_external:
            raise AgentUnavailableError(
                "Refusing to construct the Jules agent: pass allow_external=True "
                "to acknowledge that it contacts an external service."
            )
        return JulesAgent(**kwargs)  # type: ignore[arg-type]
    raise AgentError(f"unknown agent provider: {provider!r}")
