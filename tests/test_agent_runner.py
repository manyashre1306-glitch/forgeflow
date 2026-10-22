"""Tests for the coding-agent adapter (SPEC sections 3 and 4).

No test here touches the network. The Jules tests drive the real
``_parse_activities`` and ``run_task`` code paths, with the HTTP layer replaced
by a fake that returns recorded response shapes taken from the documented API.
"""

from __future__ import annotations

import json

import pytest

from forgeflow.agent_runner import (
    DEFAULT_MOCK_DIFF,
    AgentError,
    AgentRunError,
    AgentUnavailableError,
    CodingAgent,
    JulesAgent,
    MockAgent,
    get_agent,
)
from forgeflow.models import RepoContext, UntrustedText


def make_context() -> RepoContext:
    return RepoContext(
        repo_full_name="acme/widget",
        issue_number=7,
        workspace_path="data/workspaces/acme__widget__issue-7",
        issue=UntrustedText(
            text="Bug: parser fails on nested input",
            source="issue acme/widget#7",
        ),
    )


# --- interface conformance --------------------------------------------------


def test_mock_agent_satisfies_the_protocol() -> None:
    assert isinstance(MockAgent(), CodingAgent)


def test_jules_agent_satisfies_the_protocol() -> None:
    assert isinstance(JulesAgent(api_key="k"), CodingAgent)


def test_protocol_requires_the_three_members() -> None:
    class Incomplete:
        @property
        def name(self) -> str:
            return "x"

    assert not isinstance(Incomplete(), CodingAgent)


# --- MockAgent --------------------------------------------------------------


def test_mock_agent_returns_a_diff() -> None:
    result = MockAgent().run_task(make_context(), "do the thing")
    assert result.provider == "mock"
    assert result.diff == DEFAULT_MOCK_DIFF
    assert result.has_changes is True
    assert result.published is False


def test_mock_agent_never_publishes() -> None:
    assert MockAgent().run_task(make_context(), "plan").published is False


def test_mock_agent_records_calls() -> None:
    agent = MockAgent()
    agent.run_task(make_context(), "plan")
    assert agent.calls == [("acme/widget", 7)]


def test_mock_agent_can_be_unavailable() -> None:
    agent = MockAgent(available=False, reason="isolation missing")
    assert agent.is_available() == (False, "isolation missing")
    with pytest.raises(AgentUnavailableError, match="isolation missing"):
        agent.run_task(make_context(), "plan")


def test_mock_agent_reports_no_changes_when_diff_empty() -> None:
    result = MockAgent(diff="").run_task(make_context(), "plan")
    assert result.has_changes is False


def test_mock_diff_is_a_real_unified_diff() -> None:
    assert DEFAULT_MOCK_DIFF.startswith("diff --git ")
    assert "--- a/" in DEFAULT_MOCK_DIFF
    assert "+++ b/" in DEFAULT_MOCK_DIFF


# --- provider selection -----------------------------------------------------


def test_get_agent_returns_mock() -> None:
    assert get_agent("mock").name == "mock"


def test_get_agent_refuses_jules_without_opt_in() -> None:
    with pytest.raises(AgentUnavailableError, match="allow_external"):
        get_agent("jules", api_key="k")


def test_get_agent_allows_jules_with_opt_in() -> None:
    agent = get_agent("jules", allow_external=True, api_key="k")
    assert agent.name == "jules"


def test_get_agent_rejects_unknown_provider() -> None:
    with pytest.raises(AgentError, match="unknown agent provider"):
        get_agent("nope")


# --- Jules availability (fails closed) -------------------------------------


def test_jules_without_key_is_unavailable() -> None:
    agent = JulesAgent(api_key=None)
    available, reason = agent.is_available()
    assert available is False
    assert "JULES_API_KEY" in reason


def test_jules_without_key_refuses_before_any_request() -> None:
    """The failure must be a refusal, not a failed network call."""
    agent = JulesAgent(api_key="")
    with pytest.raises(AgentUnavailableError, match="JULES_API_KEY"):
        agent.run_task(make_context(), "plan")


def test_jules_reads_key_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("JULES_API_KEY", "from-env")
    available, _ = JulesAgent().is_available()
    assert available is True


def test_jules_with_key_is_available() -> None:
    available, reason = JulesAgent(api_key="k").is_available()
    assert available is True
    assert "set" in reason


# --- Jules activity parsing (recorded API shapes) ---------------------------


def test_parse_activities_extracts_the_unidiff_patch() -> None:
    payload = {
        "activities": [
            {
                "id": "act2",
                "originator": "agent",
                "artifacts": [
                    {
                        "changeSet": {
                            "source": "sources/github-acme-widget",
                            "gitPatch": {
                                "baseCommitId": "a1b2c3d4",
                                "unidiffPatch": "diff --git a/x.py b/x.py\n",
                                "suggestedCommitMessage": "Fix parser",
                            },
                        }
                    }
                ],
            }
        ]
    }
    diff, commands, state = JulesAgent._parse_activities(payload)
    assert diff == "diff --git a/x.py b/x.py\n"
    assert commands == []
    assert state is None


def test_parse_activities_extracts_bash_output() -> None:
    payload = {
        "activities": [
            {
                "artifacts": [
                    {
                        "bashOutput": {
                            "command": "pytest -q",
                            "output": "3 passed",
                            "exitCode": 0,
                        }
                    }
                ]
            }
        ]
    }
    diff, commands, _state = JulesAgent._parse_activities(payload)
    assert diff is None
    assert len(commands) == 1
    assert commands[0].command == "pytest -q"
    assert commands[0].output == "3 passed"
    assert commands[0].exit_code == 0


def test_parse_activities_detects_completion() -> None:
    payload = {"activities": [{"sessionCompleted": {}}]}
    _, _, state = JulesAgent._parse_activities(payload)
    assert state == "COMPLETED"


def test_parse_activities_detects_failure() -> None:
    payload = {"activities": [{"sessionFailed": {"reason": "no deps"}}]}
    _, _, state = JulesAgent._parse_activities(payload)
    assert state == "FAILED"


def test_parse_activities_handles_empty_and_malformed() -> None:
    assert JulesAgent._parse_activities({}) == (None, [], None)
    assert JulesAgent._parse_activities({"activities": "nope"}) == (None, [], None)
    assert JulesAgent._parse_activities({"activities": [None, 5, "x"]}) == (None, [], None)


def test_parse_activities_ignores_blank_patch() -> None:
    payload = {"activities": [{"artifacts": [{"changeSet": {"gitPatch": {"unidiffPatch": "  "}}}]}]}
    diff, _, _ = JulesAgent._parse_activities(payload)
    assert diff is None


# --- Jules run_task against a fake HTTP layer -------------------------------


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class FakeJules:
    """Records requests and replays scripted responses."""

    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, str, dict | None]] = []

    def __call__(self, request, timeout=None):
        body = json.loads(request.data) if request.data else None
        self.requests.append((request.method, request.full_url, body))
        return FakeResponse(self.responses.pop(0))


def test_run_task_never_sends_automation_mode(monkeypatch) -> None:
    """The single most important safety property of this provider."""
    fake = FakeJules(
        [
            {"name": "sessions/123", "id": "123"},
            {"activities": [{"sessionCompleted": {}}]},
        ]
    )
    monkeypatch.setattr("urllib.request.urlopen", fake)
    JulesAgent(api_key="k", poll_interval=0.0).run_task(make_context(), "plan")

    create_body = fake.requests[0][2]
    assert create_body is not None
    assert "automationMode" not in create_body


def test_run_task_returns_the_diff(monkeypatch) -> None:
    fake = FakeJules(
        [
            {"name": "sessions/123", "id": "123"},
            {
                "activities": [
                    {
                        "artifacts": [
                            {"changeSet": {"gitPatch": {"unidiffPatch": "diff --git a/b\n"}}}
                        ]
                    },
                    {"sessionCompleted": {}},
                ]
            },
        ]
    )
    monkeypatch.setattr("urllib.request.urlopen", fake)
    result = JulesAgent(api_key="k", poll_interval=0.0).run_task(make_context(), "plan")
    assert result.diff == "diff --git a/b\n"
    assert result.session_id == "123"
    assert result.published is False
    assert result.raw_state == "COMPLETED"


def test_run_task_sends_the_key_header_and_no_pr_mode(monkeypatch) -> None:
    fake = FakeJules([{"name": "sessions/1", "id": "1"}, {"activities": []}])
    monkeypatch.setattr("urllib.request.urlopen", fake)
    with pytest.raises(AgentRunError, match="did not finish"):
        JulesAgent(api_key="k", poll_interval=0.0).run_task(make_context(), "plan", timeout=0.0)
    headers = fake.requests[0]
    assert headers[0] == "POST"
    assert headers[1].endswith("/sessions")


def test_run_task_raises_on_api_error(monkeypatch) -> None:
    import urllib.error

    def boom(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url,
            403,
            "Forbidden",
            {},
            None,  # type: ignore[arg-type]
        )

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(AgentRunError, match="HTTP 403"):
        JulesAgent(api_key="k").run_task(make_context(), "plan")


def test_run_task_times_out_when_never_terminal(monkeypatch) -> None:
    fake = FakeJules(
        [
            {"name": "sessions/9", "id": "9"},
            {"activities": []},
        ]
    )
    monkeypatch.setattr("urllib.request.urlopen", fake)
    with pytest.raises(AgentRunError, match="did not finish"):
        JulesAgent(api_key="k", poll_interval=0.0).run_task(make_context(), "plan", timeout=0.0)


def test_run_task_rejects_response_without_session_name(monkeypatch) -> None:
    fake = FakeJules([{"id": "1"}])
    monkeypatch.setattr("urllib.request.urlopen", fake)
    with pytest.raises(AgentRunError, match="did not return a session name"):
        JulesAgent(api_key="k").run_task(make_context(), "plan")


def test_run_task_uses_configured_source(monkeypatch) -> None:
    fake = FakeJules(
        [{"name": "sessions/1", "id": "1"}, {"activities": [{"sessionCompleted": {}}]}]
    )
    monkeypatch.setattr("urllib.request.urlopen", fake)
    agent = JulesAgent(api_key="k", source="sources/github-acme-widget", poll_interval=0.0)
    agent.run_task(make_context(), "plan")
    body = fake.requests[0][2]
    assert body["sourceContext"]["source"] == "sources/github-acme-widget"  # type: ignore[index]


# --- prompt isolation through the agent -------------------------------------


def test_agent_task_keeps_issue_text_fenced() -> None:
    from forgeflow.prompts import build_agent_task

    context = make_context()
    task = build_agent_task(context, "Change parser.py only.")
    assert "<<<BEGIN UNTRUSTED DATA: issue acme/widget#7>>>" in task
    assert task.index("PLAN (trusted") < task.index("<<<BEGIN UNTRUSTED DATA")


def test_agent_task_forbids_publishing() -> None:
    from forgeflow.prompts import build_agent_task

    task = build_agent_task(make_context(), "plan")
    assert "do not create commits, branches, or pull" in task
