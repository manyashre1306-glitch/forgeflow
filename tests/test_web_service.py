"""The website's service layer, exercised without Streamlit.

The service functions call ForgeFlow's real discovery, dedupe, ranking, rule
detection, and diff analysis. Only the two boundaries are faked: the GitHub
client (an in-process ``FakeGitHubClient``) and the model backend (ForgeFlow's
own ``FakeBackend``). No network, no model, no database.
"""

from __future__ import annotations

import pytest
from web import patch_agent, service
from web.analysis import SessionBudgetBackend
from web.limits import InputError, SessionLimits

from forgeflow.config import AppConfig
from forgeflow.issue_analyzer import FakeBackend, IssueAnalyzer
from forgeflow.models import Issue, RepoContext, UntrustedText
from tests.conftest import FakeGitHubClient

TRIAGE_JSON = (
    '{"actionable": true, "problem_summary": "A bug in validation.", "clarity": 0.9, '
    '"scope_hint": "small", "missing_information": [], "injection_flagged": false, '
    '"confidence": 0.8}'
)
ANALYSIS_JSON = (
    '{"problem_explanation": "Nested models fail to validate.", '
    '"minimal_fix_proposal": "Handle the nested case in _validate.", '
    '"risks": ["may change error text"], '
    '"test_plan": ["add a regression test for nested models"], '
    '"files_likely_involved": ["src/app.py"], '
    '"unverified_assumptions": ["the nested path is reachable"], '
    '"confidence": 0.7}'
)


def _repo_payload() -> dict:
    return {
        "full_name": "owner/name",
        "stargazers_count": 1234,
        "open_issues_count": 10,
        "pushed_at": "2026-10-20T12:00:00Z",
    }


def _issue_payload(number: int = 7, title: str = "Bug: nested validation") -> dict:
    return {
        "number": number,
        "title": title,
        "body": "Steps to reproduce.",
        "state": "open",
        "labels": [{"name": "bug"}],
        "html_url": f"https://github.com/owner/name/issues/{number}",
        "comments": 0,
        "assignees": [],
        "created_at": "2026-10-01T08:00:00Z",
        "updated_at": "2026-10-19T09:30:00Z",
    }


def _client(**kwargs) -> FakeGitHubClient:
    return FakeGitHubClient(**kwargs)


def _config() -> AppConfig:
    """A minimal AppConfig for the service tests."""
    return AppConfig(repositories=["owner/name"])


class TestFindAndRank:
    def test_ranks_the_repository_issues(self) -> None:
        client = _client(
            repositories={"owner/name": _repo_payload()},
            issues={"owner/name": [_issue_payload(7), _issue_payload(8, "Another bug")]},
        )
        result = service.find_and_rank(client, _config(), "owner/name")
        assert result.repo_full_name == "owner/name"
        assert result.repository_stars == 1234
        assert result.issues_found == 2
        assert len(result.ranked) == 2

    def test_each_ranked_row_carries_six_factors_with_reasons(self) -> None:
        client = _client(
            repositories={"owner/name": _repo_payload()},
            issues={"owner/name": [_issue_payload()]},
        )
        result = service.find_and_rank(client, _config(), "owner/name")
        row = result.ranked[0]
        assert len(row.factors) == 6
        for factor in row.factors:
            assert factor.reason, f"{factor.key} has no reason"
            assert factor.weight >= 0.0

    def test_invalid_repository_is_refused_before_any_request(self) -> None:
        client = _client()
        with pytest.raises(InputError):
            service.find_and_rank(client, _config(), "not a repo")
        assert client.repo_calls == []
        assert client.search_calls == []

    def test_repository_read_failure_is_reported_not_raised(self) -> None:
        client = _client()
        client.raise_on_repo.add("owner/name")
        result = service.find_and_rank(client, _config(), "owner/name")
        assert result.errors
        assert result.ranked == []

    def test_github_requests_are_counted(self) -> None:
        client = _client(
            repositories={"owner/name": _repo_payload()},
            issues={"owner/name": [_issue_payload()]},
        )
        limits = SessionLimits()
        service.find_and_rank(client, _config(), "owner/name", limits=limits)
        assert limits.github_requests >= 1

    def test_heuristic_caveat_is_attached(self) -> None:
        client = _client(
            repositories={"owner/name": _repo_payload()},
            issues={"owner/name": [_issue_payload()]},
        )
        result = service.find_and_rank(client, _config(), "owner/name")
        assert "heuristic" in result.heuristic_caveat


class TestCheckRepoRules:
    def test_prohibited_policy_is_reported(self) -> None:
        client = _client()
        # A phrase the real detector recognises as a prohibition.
        client.file_text = {
            ("owner/name", "CONTRIBUTING.md"): ("We do not accept AI-generated contributions.")
        }
        _attach_file_text(client)
        result = service.check_repo_rules(client, "owner/name")
        assert "prohibited" in result.verdict
        assert result.unknown_is_not_permission

    def test_no_documents_yields_unknown_not_permission(self) -> None:
        client = _client()
        _attach_file_text(client)
        result = service.check_repo_rules(client, "owner/name")
        assert result.verdict == "unknown"
        assert result.documents_read == []


def _attach_file_text(client: FakeGitHubClient) -> None:
    """Give a FakeGitHubClient a ``get_file_content`` that returns set text."""
    texts = getattr(client, "file_text", {})

    def get_file_content(full_name: str, path: str, *, max_bytes: int = 0, ref=None):
        client.file_calls.append((full_name, path))
        return texts.get((full_name, path))

    client.get_file_content = get_file_content  # type: ignore[attr-defined]


class TestAnalyseIssue:
    def _context(self) -> RepoContext:
        return RepoContext(
            repo_full_name="owner/name",
            issue_number=7,
            workspace_path="(hosted)",
            issue=UntrustedText(text="Bug: nested validation", source="issue"),
            relevant_files=[],
        )

    def test_analysis_view_carries_tokens_and_cost(self) -> None:
        limits = SessionLimits()
        config = service.default_llm_config()
        backend = SessionBudgetBackend(
            FakeBackend(
                replies=[TRIAGE_JSON, ANALYSIS_JSON],
                prompt_tokens=1000,
                completion_tokens=200,
            ),
            config,
            limits,
        )
        analyzer = IssueAnalyzer(backend, config, budget_query=lambda *a: 0.0)

        client = _client(repositories={"owner/name": _repo_payload()})
        client.get_file_content = lambda *a, **k: None  # type: ignore[attr-defined]
        client.list_directory_entries = lambda *a, **k: []  # type: ignore[attr-defined]

        issue = service.issue_from_inputs("owner/name", 7, title="Bug", body="body")
        view, budget = service.analyse_issue(client, analyzer, issue, limits=limits)

        assert view.problem_explanation
        assert view.minimal_fix_proposal
        assert view.test_plan
        assert view.model_calls == 2
        assert view.prompt_tokens == 2000
        assert view.completion_tokens == 400
        assert view.cost_usd > 0.0
        assert budget.total >= 1

    def test_session_cap_refuses_a_second_call(self) -> None:
        limits = SessionLimits(max_model_calls=1)
        config = service.default_llm_config()
        backend = SessionBudgetBackend(
            FakeBackend(replies=[TRIAGE_JSON, ANALYSIS_JSON]), config, limits
        )
        analyzer = IssueAnalyzer(backend, config, budget_query=lambda *a: 0.0)
        client = _client(repositories={"owner/name": _repo_payload()})
        client.get_file_content = lambda *a, **k: None  # type: ignore[attr-defined]
        client.list_directory_entries = lambda *a, **k: []  # type: ignore[attr-defined]
        issue = service.issue_from_inputs("owner/name", 7, title="Bug")
        with pytest.raises(InputError, match="model-call limit"):
            service.analyse_issue(client, analyzer, issue, limits=limits)


class TestIssueFromInputs:
    def test_builds_an_issue_and_validates(self) -> None:
        issue = service.issue_from_inputs("owner/name", 7, title="T", body="B")
        assert isinstance(issue, Issue)
        assert issue.repo_full_name == "owner/name"
        assert issue.number == 7

    def test_rejects_a_bad_repository(self) -> None:
        with pytest.raises(InputError):
            service.issue_from_inputs("bad repo", 7)


class TestPatchAgent:
    def _context(self) -> RepoContext:
        return RepoContext(
            repo_full_name="owner/name",
            issue_number=7,
            workspace_path="(hosted)",
            issue=UntrustedText(text="Bug", source="issue"),
            relevant_files=[
                _excerpt(
                    "src/app.py",
                    "def validate(x):\n    return x\n",
                )
            ],
        )

    def test_a_diff_that_does_not_apply_is_rejected(self) -> None:
        """The correctness gate must reject a plausible-looking wrong diff."""
        bad_diff = (
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def validate(x):\n"
            "-    return x + 999\n"
            "+    return x\n"
        )
        limits = SessionLimits()
        config = service.default_llm_config()
        agent = patch_agent.build_patch_agent(
            api_key="test-key", model=config.analysis_model, limits=limits, config=config
        )
        agent._backend = SessionBudgetBackend(FakeBackend(replies=[bad_diff]), config, limits)
        with pytest.raises(patch_agent.PatchGenerationError):
            agent.run_task(self._context(), "fix it", timeout=5.0)

    def test_a_diff_that_applies_is_returned(self) -> None:
        good_diff = (
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def validate(x):\n"
            "-    return x\n"
            "+    return x or 0\n"
        )
        limits = SessionLimits()
        config = service.default_llm_config()
        agent = patch_agent.build_patch_agent(
            api_key="test-key", model=config.analysis_model, limits=limits, config=config
        )
        agent._backend = SessionBudgetBackend(FakeBackend(replies=[good_diff]), config, limits)
        result = agent.run_task(self._context(), "fix it", timeout=5.0)
        assert "return x or 0" in result.diff
        assert result.published is False

    def test_no_patch_reply_is_refused(self) -> None:
        limits = SessionLimits()
        config = service.default_llm_config()
        agent = patch_agent.build_patch_agent(
            api_key="test-key", model=config.analysis_model, limits=limits, config=config
        )
        agent._backend = SessionBudgetBackend(FakeBackend(replies=["NO_PATCH"]), config, limits)
        with pytest.raises(patch_agent.PatchGenerationError):
            agent.run_task(self._context(), "fix it", timeout=5.0)

    def test_patch_agent_reports_it_does_not_publish(self) -> None:
        limits = SessionLimits()
        config = service.default_llm_config()
        agent = patch_agent.build_patch_agent(
            api_key="test-key", model=config.analysis_model, limits=limits, config=config
        )
        assert agent.name == "gemini-patch"
        available, _ = agent.is_available()
        assert available is True


def _excerpt(path: str, content: str):
    from forgeflow.models import FileExcerpt

    return FileExcerpt(
        path=path,
        size_bytes=len(content.encode("utf-8")),
        content=UntrustedText(text=content, source=path),
    )
