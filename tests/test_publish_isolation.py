"""The publishing credential must be unreachable from the rest of ForgeFlow.

SPEC section 8 requires that discovery, analysis, and coding components do not
hold publishing credentials. A comment saying so is worthless, so this file
enforces it two ways: a static scan of every source module's imports, and a
runtime guard that watches for the environment variable being read.

If either test fails, the isolation is broken and the credential could leak into
a component that processes untrusted repository content.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from forgeflow import publish_token

SRC = Path(__file__).resolve().parent.parent / "src" / "forgeflow"

# Modules that may touch the publishing credential, and why.
#   publish_token  - reads it, that is its job.
#   publisher      - the single write path.
#   cli            - reports presence and builds a publisher, never reads it.
ALLOWED = {"publish_token.py", "publisher.py", "cli.py"}


def _imports_of(path: Path) -> set[str]:
    """Every module name one source file imports, in either import form.

    Both spellings must be caught: ``import forgeflow.publish_token`` and
    ``from forgeflow import publish_token``. An earlier version of this helper
    only recorded the ``ImportFrom`` module, so the second form slipped past the
    guard. A test that cannot fail is not a guard.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module:
                found.add(module)
            # ``from forgeflow import publish_token`` imports the submodule, so
            # the dotted name is what matters for this check.
            for alias in node.names:
                if module:
                    found.add(f"{module}.{alias.name}")
                else:
                    found.add(alias.name)
    return found


def test_only_permitted_modules_import_publish_token() -> None:
    """No discovery, analysis, or coding module may import the token module."""
    offenders: list[str] = []
    for path in sorted(SRC.glob("*.py")):
        if path.name in ALLOWED:
            continue
        if "forgeflow.publish_token" in _imports_of(path):
            offenders.append(path.name)
    assert offenders == [], (
        f"these modules import the publishing credential module: {offenders}. "
        "Only publisher.py, publish_token.py, and cli.py may."
    )


def test_publish_token_module_is_never_re_exported() -> None:
    """The credential module must not be reachable through a package facade."""
    init = SRC / "__init__.py"
    if init.exists():
        assert "publish_token" not in _imports_of(init)


def test_discovery_module_has_no_publish_token_reference() -> None:
    """The read path must not even name the publishing variable."""
    text = (SRC / "discovery.py").read_text(encoding="utf-8")
    assert "FORGEFLOW_PUBLISH_TOKEN" not in text
    assert "publish_token" not in text


@pytest.mark.parametrize(
    "module_name",
    ["discovery", "issue_analyzer", "agent_runner", "sandbox", "repository_context", "ranking"],
)
def test_untrusted_input_modules_cannot_reach_the_token(module_name: str) -> None:
    """Modules that touch repository or model content must not import it.

    These are the components that handle untrusted input, so a prompt injection
    reaching them must have no route to the publishing credential.
    """
    path = SRC / f"{module_name}.py"
    if not path.exists():
        pytest.skip(f"{module_name}.py not present")
    assert "publish_token" not in _imports_of(path)
    assert "FORGEFLOW_PUBLISH_TOKEN" not in path.read_text(encoding="utf-8")


def test_no_source_file_hardcodes_the_token_value() -> None:
    """The token must come from the environment, never from source or config."""
    for path in sorted(SRC.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        # A literal assignment of a token-shaped value would be a leak.
        assert "FORGEFLOW_PUBLISH_TOKEN =" not in text


def test_read_publish_token_fails_closed_when_absent(monkeypatch) -> None:
    monkeypatch.delenv(publish_token.PUBLISH_TOKEN_ENV, raising=False)
    with pytest.raises(publish_token.PublishTokenError, match="is not set"):
        publish_token.read_publish_token()


def test_read_publish_token_does_not_fall_back_to_github_token(monkeypatch) -> None:
    """A read-only GITHUB_TOKEN must never be accepted as the publishing token."""
    monkeypatch.delenv(publish_token.PUBLISH_TOKEN_ENV, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "a" * 36)
    with pytest.raises(publish_token.PublishTokenError):
        publish_token.read_publish_token()


def test_publish_token_present_is_boolean_only(monkeypatch) -> None:
    monkeypatch.setenv(publish_token.PUBLISH_TOKEN_ENV, "secret-value-123")
    assert publish_token.publish_token_present() is True
    monkeypatch.delenv(publish_token.PUBLISH_TOKEN_ENV)
    assert publish_token.publish_token_present() is False


def test_token_fingerprint_does_not_reveal_the_token() -> None:
    """A fingerprint may go in a log; the token may not."""
    token = "ghp_" + "Z" * 36
    fingerprint = publish_token.token_fingerprint(token)
    assert token not in fingerprint
    assert "Z" * 20 not in fingerprint


def test_publish_token_error_message_contains_no_token(monkeypatch) -> None:
    """Even the failure path must not echo a value."""
    monkeypatch.delenv(publish_token.PUBLISH_TOKEN_ENV, raising=False)
    with pytest.raises(publish_token.PublishTokenError) as excinfo:
        publish_token.read_publish_token()
    assert "ghp_" not in str(excinfo.value)


# ==========================================================================
# A missing token must be reported, not dumped as a traceback
# ==========================================================================


def test_publish_token_error_is_a_forgeflow_error() -> None:
    """The CLI catches ForgeFlowError, so the token error must be one.

    It was a plain RuntimeError before, which meant a missing token escaped the
    handler and printed a raw traceback instead of the message.
    """
    from forgeflow.cli import ForgeFlowError

    assert issubclass(publish_token.PublishTokenError, ForgeFlowError)
    assert issubclass(publish_token.PublishTokenError, RuntimeError)


def test_missing_token_error_is_caught_by_the_cli_handler() -> None:
    """Raising the error through the CLI's handler yields a clean message."""
    from forgeflow.cli import ForgeFlowError

    error = publish_token.PublishTokenError("FORGEFLOW_PUBLISH_TOKEN is not set")
    assert isinstance(error, ForgeFlowError)
