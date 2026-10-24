"""Model calls for the website, using the visitor's own key.

Two things this module is careful about:

* **The key is the visitor's.** It is read from the session, passed straight to
  the Gemini backend, and never written anywhere. It is not put in a URL, a log,
  a database row, or a report. :func:`forgeflow.security.redact` is applied to
  any captured output before it is shown.
* **The session cap is enforced here, before the call.** The underlying
  :class:`~forgeflow.issue_analyzer.IssueAnalyzer` still runs its own budget and
  kill-switch checks; this adds a per-session ceiling on top, so one visitor
  cannot spend without limit even when the operator's monthly cap is high.

The Gemini backend, the models, the prices, and the retry limit all come from
ForgeFlow's own configuration. Nothing is re-implemented.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from forgeflow.config import LLMConfig
from forgeflow.issue_analyzer import (
    GeminiBackend,
    IssueAnalyzer,
    LLMBackend,
    LLMResponse,
)
from forgeflow.models import compute_cost_usd
from web.limits import SessionLimits


class SessionBudgetBackend:
    """Wrap a backend so each call is counted against the session's limits.

    Implements the :class:`~forgeflow.issue_analyzer.LLMBackend` protocol by
    delegating to the wrapped backend. The check happens *before* the inner call,
    so a refused call costs nothing.
    """

    def __init__(
        self,
        inner: LLMBackend,
        config: LLMConfig,
        limits: SessionLimits,
    ) -> None:
        self._inner = inner
        self._config = config
        self._limits = limits

    @property
    def name(self) -> str:
        return self._inner.name

    def is_available(self) -> tuple[bool, str]:
        return self._inner.is_available()

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        model: str,
        timeout: float,
        max_output_tokens: int,
    ) -> LLMResponse:
        # Refuse before spending: the session cap is checked first.
        self._limits.check_model_call()
        response = self._inner.complete(
            system=system,
            prompt=prompt,
            model=model,
            timeout=timeout,
            max_output_tokens=max_output_tokens,
        )
        cost = compute_cost_usd(
            response.prompt_tokens,
            response.completion_tokens,
            self._config.pricing_for(model),
        )
        self._limits.record_model_call(
            cost_usd=cost,
            label=model,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
        )
        return response


def build_session_analyzer(
    api_key: str,
    config: LLMConfig,
    limits: SessionLimits,
    *,
    conn: sqlite3.Connection | None = None,
    kill_switch_check: Callable[[], None] | None = None,
) -> IssueAnalyzer:
    """Build an analyzer that uses the visitor's key under the session cap.

    The session cap is wired in as the analyzer's own budget, so the existing
    ``check_budget()`` gate runs before every paid call and raises once the
    visitor's ceiling is reached. ``check_budget()`` compares the recorded spend
    against ``config.monthly_cap_usd``, so the config is copied with that field
    set to the session cap: the ceiling a visitor feels is theirs, not the
    operator's. Models, prices, retries, and the timeout are unchanged.

    ``conn`` is optional. In local mode it is the real database, so usage is
    recorded there too. In hosted mode there is no database and no recorder; the
    session cap is the only ceiling.
    """
    session_config = config.model_copy(update={"monthly_cap_usd": limits.max_cost_usd})
    backend = SessionBudgetBackend(GeminiBackend(api_key=api_key), session_config, limits)

    def session_budget_query(_now: object = None) -> float:
        return limits.cost_usd

    usage_recorder = None
    if conn is not None:
        from forgeflow.issue_analyzer import make_usage_recorder

        usage_recorder = make_usage_recorder(conn)

    return IssueAnalyzer(
        backend,
        session_config,
        budget_query=session_budget_query,
        usage_recorder=usage_recorder,
        kill_switch_check=kill_switch_check,
    )
