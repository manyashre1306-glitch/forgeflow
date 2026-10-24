"""The hosted context builder, and a headless smoke test of the app.

The context builder is exercised against an in-process fake client, so it makes
no network call. The app smoke test uses Streamlit's own ``AppTest`` and is
skipped when the optional ``web`` extra is not installed, so the core install and
CI (a plain ``uv sync --frozen``) still pass without Streamlit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from web.context import ContextFetchError, build_hosted_context

from forgeflow.models import Issue, RepoContext
from tests.conftest import FakeGitHubClient

REPO = "owner/name"


class _TextClient(FakeGitHubClient):
    """A fake client that also serves file text and typed directory entries."""

    def __init__(self, *, files: dict[str, str], entries: dict[str, list[dict]]):
        super().__init__(repositories={REPO: {"full_name": REPO, "stargazers_count": 5}})
        self._files = files
        self._entries = entries
        self.file_reads: list[str] = []

    def get_file_content(self, full_name, path, *, max_bytes=0, ref=None):
        self.file_reads.append(path)
        return self._files.get(path)

    def list_directory_entries(self, full_name, path=""):
        return list(self._entries.get(path, []))


def _issue() -> Issue:
    return Issue(
        repo_full_name=REPO,
        number=7,
        title="Bug in src/app.py validate",
        body="The validate function returns the wrong value.",
    )


class TestBuildHostedContext:
    def test_builds_a_context_without_cloning(self) -> None:
        client = _TextClient(
            files={
                "README.md": "# Project\n",
                "CONTRIBUTING.md": "Be nice.\n",
                "src/app.py": "def validate(x):\n    return x\n",
            },
            entries={
                "": [
                    {"name": "README.md", "type": "file"},
                    {"name": "CONTRIBUTING.md", "type": "file"},
                    {"name": "src", "type": "dir"},
                ],
                "src": [{"name": "app.py", "type": "file"}],
            },
        )
        context = build_hosted_context(client, _issue())
        assert isinstance(context, RepoContext)
        assert context.repo_full_name == REPO
        assert "hosted" in context.workspace_path
        assert context.readme is not None
        assert any(excerpt.path == "src/app.py" for excerpt in context.relevant_files)

    def test_repository_read_failure_raises(self) -> None:
        client = _TextClient(files={}, entries={})
        client.raise_on_repo.add(REPO)
        with pytest.raises(ContextFetchError):
            build_hosted_context(client, _issue())

    def test_missing_documents_is_not_an_error(self) -> None:
        client = _TextClient(files={}, entries={"": []})
        context = build_hosted_context(client, _issue())
        assert context.readme is None
        assert context.contribution_rules.ai_policy.value == "unknown"

    def test_tree_walk_is_bounded(self) -> None:
        """A pathologically deep tree must not cause unbounded requests."""
        entries = {
            "": [{"name": "a", "type": "dir"}],
            "a": [{"name": "b", "type": "dir"}],
            "a/b": [{"name": "c", "type": "dir"}],
            "a/b/c": [{"name": "d", "type": "dir"}],
            "a/b/c/d": [{"name": "deep.py", "type": "file"}],
        }
        client = _TextClient(files={}, entries=entries)
        context = build_hosted_context(client, _issue())
        # Depth cap is 3, so a/b/c/d/deep.py is never reached.
        assert all("deep.py" not in excerpt.path for excerpt in context.relevant_files)

    def test_issue_text_is_wrapped_as_untrusted(self) -> None:
        client = _TextClient(files={}, entries={"": []})
        context = build_hosted_context(client, _issue())
        assert context.issue.source.startswith("issue ")
        assert "Bug in src/app.py" in context.issue.text


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("streamlit") is None,
    reason="the optional web extra (streamlit) is not installed",
)
class TestAppSmoke:
    def test_app_renders_in_hosted_mode(self, monkeypatch) -> None:
        """The whole app must render without error, defaulting to hosted mode."""
        from streamlit.testing.v1 import AppTest

        monkeypatch.delenv("FORGEFLOW_WEB_MODE", raising=False)
        app = Path(__file__).resolve().parent.parent / "web" / "app.py"
        at = AppTest.from_file(str(app), default_timeout=30)
        at.run()
        assert not at.exception, at.exception

    def test_validation_and_publish_pages_are_locked_in_hosted_mode(self, monkeypatch) -> None:
        """Selecting a locked page must show the refusal, not the controls."""
        from streamlit.testing.v1 import AppTest

        monkeypatch.delenv("FORGEFLOW_WEB_MODE", raising=False)
        app = Path(__file__).resolve().parent.parent / "web" / "app.py"
        at = AppTest.from_file(str(app), default_timeout=30)
        at.run()
        at.sidebar.radio[0].set_value("Validation").run()
        assert not at.exception, at.exception
        errors = [e.value for e in at.error]
        assert any("locked in hosted mode" in e for e in errors), errors

    def test_app_renders_in_local_mode(self, monkeypatch) -> None:
        """In local mode the app still renders; the pages are unlocked."""
        from streamlit.testing.v1 import AppTest

        monkeypatch.setenv("FORGEFLOW_WEB_MODE", "local")
        app = Path(__file__).resolve().parent.parent / "web" / "app.py"
        at = AppTest.from_file(str(app), default_timeout=30)
        at.run()
        assert not at.exception, at.exception

    def test_publish_page_renders_in_local_mode(self, monkeypatch) -> None:
        from streamlit.testing.v1 import AppTest

        monkeypatch.setenv("FORGEFLOW_WEB_MODE", "local")
        app = Path(__file__).resolve().parent.parent / "web" / "app.py"
        at = AppTest.from_file(str(app), default_timeout=30)
        at.run()
        at.sidebar.radio[0].set_value("Approval and Publish").run()
        assert not at.exception, at.exception
        # Local mode must not show the hosted lock.
        errors = [e.value for e in at.error]
        assert not any("locked in hosted mode" in e for e in errors), errors
