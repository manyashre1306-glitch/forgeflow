"""Issue analysis for ForgeFlow Phase 2 (SPEC sections 2E and 12).

Pipeline: a cheap triage call decides whether the issue is worth spending on,
then a stronger model produces the plan. Both models, the prices, the retry
limit, the timeout, and the monthly cap are configuration, not constants.

Four properties this module is built to guarantee:

1. **Spending is capped.** :meth:`LLMClient.check_budget` runs before every call
   and raises :class:`BudgetExceededError` when the month's recorded spend has
   reached the cap. It is checked *before* the request, so an over-budget call
   never leaves the machine.
2. **Failures are bounded.** Retries are capped by ``max_retries`` and every
   attempt has a timeout. There is no unbounded retry loop.
3. **Every call is recorded.** Successes and failures are both written to
   SQLite with token counts and cost, so spend is auditable.
4. **Untrusted text stays data.** Prompts are built in :mod:`forgeflow.prompts`,
   which fences repository text and states that it carries no authority. The
   model's reply is parsed into a validated Pydantic object, so a malformed or
   hostile response fails validation rather than flowing downstream.

API keys are read from the environment only. They are never logged, never
included in a prompt, and never passed to the sandbox.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from forgeflow.config import LLMConfig
from forgeflow.models import (
    AnalysisResult,
    Issue,
    LLMUsage,
    RepoContext,
    TriageResult,
    compute_cost_usd,
)
from forgeflow.prompts import (
    ANALYSIS_SYSTEM,
    TRIAGE_SYSTEM,
    build_analysis_prompt,
    build_triage_prompt,
)

GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class AnalyzerError(RuntimeError):
    """Base class for analysis failures."""


class BudgetExceededError(AnalyzerError):
    """Raised when the monthly spending cap would be exceeded."""


class ModelUnavailableError(AnalyzerError):
    """Raised when no model can be called, for example a missing API key."""


class ModelResponseError(AnalyzerError):
    """Raised when a model reply cannot be parsed into the expected object."""


class LLMResponse:
    """A raw model reply plus its token usage."""

    __slots__ = ("completion_tokens", "prompt_tokens", "text")

    def __init__(self, text: str, prompt_tokens: int, completion_tokens: int) -> None:
        self.text = text
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


@runtime_checkable
class LLMBackend(Protocol):
    """A provider that can complete one prompt.

    Kept deliberately narrow so a new provider is a small class, and so tests
    can supply a fake without touching the network.
    """

    @property
    def name(self) -> str: ...

    def is_available(self) -> tuple[bool, str]: ...

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        model: str,
        timeout: float,
        max_output_tokens: int,
    ) -> LLMResponse: ...


# --------------------------------------------------------------------------
# Gemini backend
# --------------------------------------------------------------------------


class GeminiBackend:
    """Backend for the documented Gemini generateContent REST endpoint.

    Reads the key from ``GEMINI_API_KEY`` only. The key travels in a request
    header and is never logged.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = GEMINI_BASE_URL,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get(GEMINI_API_KEY_ENV)
        self._base_url = base_url.rstrip("/")

    @property
    def name(self) -> str:
        return "gemini"

    def is_available(self) -> tuple[bool, str]:
        if not self._api_key:
            return False, f"{GEMINI_API_KEY_ENV} is not set."
        return True, f"{GEMINI_API_KEY_ENV} is set."

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        model: str,
        timeout: float,
        max_output_tokens: int,
    ) -> LLMResponse:
        if not self._api_key:
            raise ModelUnavailableError(
                f"{GEMINI_API_KEY_ENV} is not set; refusing to call the model API."
            )
        url = f"{self._base_url}/models/{model}:generateContent"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": max_output_tokens,
                "temperature": 0.0,
                "responseMimeType": "application/json",
            },
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("x-goog-api-key", self._api_key)

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise AnalyzerError(f"model API HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise AnalyzerError(f"model API unreachable: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise ModelResponseError(f"model API returned invalid JSON: {exc}") from exc

        return _parse_gemini_response(body)


def _parse_gemini_response(body: object) -> LLMResponse:
    """Extract text and token usage from a generateContent response."""
    if not isinstance(body, dict):
        raise ModelResponseError("model API returned a non-object response")

    text_parts: list[str] = []
    candidates = body.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content")
            if not isinstance(content, dict):
                continue
            parts = content.get("parts")
            if not isinstance(parts, list):
                continue
            for part in parts:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    text_parts.append(part["text"])

    if not text_parts:
        raise ModelResponseError("model API response contained no text")

    usage = body.get("usageMetadata")
    prompt_tokens = 0
    completion_tokens = 0
    if isinstance(usage, dict):
        raw_prompt = usage.get("promptTokenCount")
        raw_completion = usage.get("candidatesTokenCount")
        prompt_tokens = raw_prompt if isinstance(raw_prompt, int) else 0
        completion_tokens = raw_completion if isinstance(raw_completion, int) else 0

    return LLMResponse("".join(text_parts), prompt_tokens, completion_tokens)


# --------------------------------------------------------------------------
# JSON extraction
# --------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json_object(text: str) -> dict[str, object]:
    """Pull one JSON object out of a model reply.

    Handles a bare object, an object wrapped in a Markdown fence, and an object
    with prose around it. Raises rather than guessing when no object is present.
    """
    candidate = text.strip()

    fence_match = _FENCE.search(candidate)
    if fence_match:
        candidate = fence_match.group(1).strip()

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ModelResponseError("no JSON object found in model reply") from None
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ModelResponseError(f"model reply was not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ModelResponseError("model reply was valid JSON but not an object")
    return parsed


# --------------------------------------------------------------------------
# Analyzer
# --------------------------------------------------------------------------


class IssueAnalyzer:
    """Runs triage and analysis, enforcing budget, retries, and validation."""

    def __init__(
        self,
        backend: LLMBackend,
        config: LLMConfig,
        *,
        budget_query: object = None,
        usage_recorder: object = None,
        kill_switch_check: Callable[[], None] | None = None,
    ) -> None:
        self._backend = backend
        self._config = config
        self._budget_query = budget_query
        self._usage_recorder = usage_recorder
        self._kill_switch_check = kill_switch_check

    @property
    def config(self) -> LLMConfig:
        return self._config

    def check_budget(self, *, now: datetime | None = None) -> float:
        """Return the spend this month, raising if the cap is already reached.

        Called before every request. When no budget query is wired in, spend is
        treated as unknown and the cap cannot be enforced; that case raises
        rather than silently allowing unlimited spending.
        """
        if self._budget_query is None:
            raise BudgetExceededError(
                "no budget query configured; refusing to call a paid model "
                "without a way to enforce the monthly cap"
            )
        spent = float(self._budget_query(now))  # type: ignore[operator]
        if spent >= self._config.monthly_cap_usd:
            raise BudgetExceededError(
                f"monthly cap reached: spent ${spent:.4f} of ${self._config.monthly_cap_usd:.2f}"
            )
        return spent

    def _record(
        self,
        *,
        context: RepoContext,
        purpose: str,
        model: str,
        response: LLMResponse | None,
        success: bool,
        error: str | None = None,
    ) -> None:
        if self._usage_recorder is None:
            return
        prompt_tokens = response.prompt_tokens if response else 0
        completion_tokens = response.completion_tokens if response else 0
        usage = LLMUsage(
            created_at=datetime.now(UTC),
            repo_full_name=context.repo_full_name,
            issue_number=context.issue_number,
            purpose=purpose,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=compute_cost_usd(
                prompt_tokens, completion_tokens, self._config.pricing_for(model)
            ),
            success=success,
            error=error,
        )
        self._usage_recorder(usage)  # type: ignore[operator]

    def _call(
        self,
        *,
        context: RepoContext,
        system: str,
        prompt: str,
        model: str,
        purpose: str,
    ) -> dict[str, object]:
        """Call the model with bounded retries, recording every attempt."""
        # The kill switch is checked first: it must be able to stop a call even
        # when the budget check would have allowed it.
        if self._kill_switch_check is not None:
            self._kill_switch_check()
        self.check_budget()

        attempts = self._config.max_retries + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self._backend.complete(
                    system=system,
                    prompt=prompt,
                    model=model,
                    timeout=self._config.timeout_seconds,
                    max_output_tokens=self._config.max_output_tokens,
                )
            except (AnalyzerError, ModelUnavailableError) as exc:
                last_error = exc
                self._record(
                    context=context,
                    purpose=purpose,
                    model=model,
                    response=None,
                    success=False,
                    error=str(exc),
                )
                if isinstance(exc, ModelUnavailableError):
                    raise
                continue

            self._record(
                context=context,
                purpose=purpose,
                model=model,
                response=response,
                success=True,
            )
            try:
                return extract_json_object(response.text)
            except ModelResponseError as exc:
                last_error = exc
                if attempt == attempts - 1:
                    raise

        raise AnalyzerError(
            f"{purpose} failed after {attempts} attempt(s): {last_error}"
        ) from last_error

    def triage(self, context: RepoContext) -> TriageResult:
        """Run the cheap triage model and validate its verdict."""
        payload = self._call(
            context=context,
            system=TRIAGE_SYSTEM,
            prompt=build_triage_prompt(context),
            model=self._config.triage_model,
            purpose="triage",
        )
        try:
            return TriageResult(**payload)
        except ValidationError as exc:
            raise ModelResponseError(f"triage reply failed validation: {exc}") from exc

    def analyze(self, context: RepoContext, *, skip_triage: bool = False) -> AnalysisResult:
        """Triage then analyse, returning a validated plan.

        Triage can be skipped for a single explicitly chosen issue, which is the
        manual case: the operator already decided the issue is worth analysing.
        """
        triage: TriageResult | None = None
        if not skip_triage:
            triage = self.triage(context)

        payload = self._call(
            context=context,
            system=ANALYSIS_SYSTEM,
            prompt=build_analysis_prompt(context),
            model=self._config.analysis_model,
            purpose="analysis",
        )
        try:
            result = AnalysisResult(**payload)
        except ValidationError as exc:
            raise ModelResponseError(f"analysis reply failed validation: {exc}") from exc
        result.triage = triage
        return result


# --------------------------------------------------------------------------
# Fake backend for tests
# --------------------------------------------------------------------------


class FakeBackend:
    """Scripted backend for tests. No network, deterministic.

    Replies are consumed in order; the last reply repeats if more calls arrive,
    which keeps a test readable when a retry happens.
    """

    def __init__(
        self,
        replies: list[str] | None = None,
        *,
        prompt_tokens: int = 100,
        completion_tokens: int = 50,
        available: bool = True,
        reason: str = "FakeBackend is always available.",
        errors: list[Exception] | None = None,
    ) -> None:
        self.replies = replies if replies is not None else ["{}"]
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self._available = available
        self._reason = reason
        self._errors = errors or []
        self.calls: list[tuple[str, str]] = []

    @property
    def name(self) -> str:
        return "fake"

    def is_available(self) -> tuple[bool, str]:
        return self._available, self._reason

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        model: str,
        timeout: float,
        max_output_tokens: int,
    ) -> LLMResponse:
        self.calls.append((model, prompt))
        if self._errors:
            raise self._errors.pop(0)
        index = min(len(self.calls) - 1, len(self.replies) - 1)
        return LLMResponse(self.replies[index], self.prompt_tokens, self.completion_tokens)


def make_budget_query(conn: object):
    """Return a callable reporting this month's spend from a SQLite connection."""
    from forgeflow import storage

    def query(now: datetime | None = None) -> float:
        return storage.spent_in_month(conn, now=now)  # type: ignore[arg-type]

    return query


def make_usage_recorder(conn: object):
    """Return a callable that writes one LLMUsage row."""
    from forgeflow import storage

    def record(usage: LLMUsage) -> None:
        storage.record_llm_usage(conn, usage)  # type: ignore[arg-type]

    return record


def build_analyzer(
    config: LLMConfig,
    *,
    conn: object | None = None,
    backend: LLMBackend | None = None,
    app_config: object = None,
) -> IssueAnalyzer:
    """Construct an analyzer with budget and usage tracking wired to SQLite.

    When no backend is supplied the Gemini backend is used, which reads its key
    from the environment. That backend fails closed when the key is absent.
    """
    if backend is None:
        backend = GeminiBackend()
    budget_query = make_budget_query(conn) if conn is not None else None
    usage_recorder = make_usage_recorder(conn) if conn is not None else None

    def check_kill_switch() -> None:
        from forgeflow.config import require_kill_switch_clear

        require_kill_switch_clear(app_config)  # type: ignore[arg-type]

    return IssueAnalyzer(
        backend,
        config,
        budget_query=budget_query,
        usage_recorder=usage_recorder,
        kill_switch_check=check_kill_switch,
    )


def require_context_issue(context: RepoContext) -> Issue:
    """Reconstruct a minimal Issue from a context, for reporting."""
    return Issue(
        repo_full_name=context.repo_full_name,
        number=context.issue_number,
        title=context.issue.text.splitlines()[0] if context.issue.text else "",
        body="",
    )
