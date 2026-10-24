"""Hosted mode must be isolated from the local machine and from publishing.

The website ships two modes. Hosted is the default because a public deployment
must not hold the operator's credentials or run repository code. That promise is
only worth anything if it is enforced by code and proven by tests, so this file
asserts three separate properties:

1. **Static isolation.** No module a hosted page can import reaches
   ``forgeflow.publisher`` or ``forgeflow.publish_token``. ``web/local.py`` is the
   one exception, and it is imported lazily, so it is not in the closure.
2. **Runtime isolation.** In hosted mode, validation and publishing refuse even
   when the functions are called directly, bypassing the pages entirely. The
   refusal happens before the database or the token is touched.
3. **No local database and no ``.env``.** Hosted code paths never open
   ``data/forgeflow.db`` and never read a ``.env`` file.

A comment claiming isolation is worthless; these tests fail if the isolation
breaks.
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import importlib
import sys
from pathlib import Path

import pytest
from web.modes import HostedModeRefused, ModeInfo, WebMode

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB = REPO_ROOT / "web"

HOSTED = ModeInfo(mode=WebMode.HOSTED, source="test")
LOCAL = ModeInfo(mode=WebMode.LOCAL, source="test")

#: The only web module allowed to name the publishing credential or the
#: publisher. It refuses in hosted mode before importing either.
PUBLISHING_MODULE = "local.py"


def _imports_of(path: Path) -> set[str]:
    """Every module name a source file imports, in both import spellings."""
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
            for alias in node.names:
                found.add(f"{module}.{alias.name}" if module else alias.name)
    return found


def _web_sources() -> list[Path]:
    return sorted(p for p in WEB.rglob("*.py") if "__pycache__" not in p.parts)


def _reads_publish_token_env(path: Path) -> bool:
    """True if the file accesses ``os.environ``/``os.getenv`` with the token name.

    A plain substring search would flag the Safety page, which *mentions* the
    variable in prose ("publishing uses FORGEFLOW_PUBLISH_TOKEN"). This walks the
    AST so only real environment access counts.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        # os.environ["FORGEFLOW_PUBLISH_TOKEN"] or os.environ.get(...)
        if (
            isinstance(node, ast.Subscript)
            and "environ" in ast.dump(node.value)
            and "FORGEFLOW_PUBLISH_TOKEN" in ast.dump(node.slice)
        ):
            return True
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", "")
            if name in {"get", "getenv"}:
                args = [*node.args, *(kw.value for kw in node.keywords)]
                if any(
                    isinstance(a, ast.Constant) and a.value == "FORGEFLOW_PUBLISH_TOKEN"
                    for a in args
                ):
                    return True
    return False


# --------------------------------------------------------------------------
# 1. Static isolation
# --------------------------------------------------------------------------


def test_only_the_local_module_names_the_publishing_credential() -> None:
    """No hosted-reachable module may import the publisher or read the token env var.

    Documentation may *name* the variable (the Safety page explains it), so the
    check is about reaching it: importing the credential modules, or reading the
    variable through ``os.environ``/``getenv``.
    """
    offenders: list[str] = []
    for path in _web_sources():
        if path.name == PUBLISHING_MODULE:
            continue
        imports = _imports_of(path)
        if "forgeflow.publisher" in imports or "forgeflow.publish_token" in imports:
            offenders.append(f"{path.relative_to(REPO_ROOT)} (imports it)")
        if _reads_publish_token_env(path):
            offenders.append(f"{path.relative_to(REPO_ROOT)} (reads the variable)")
    assert offenders == [], (
        "these web modules reach the publishing credential: "
        f"{offenders}. Only web/local.py may, and only after require_local()."
    )


#: Pure helpers in web/local.py that hold no capability and touch no credential.
_PURE_LOCAL_HELPERS = {"confirmation_string"}


def test_local_module_guards_every_capability() -> None:
    """Every capability function in web/local.py must begin by requiring local mode."""
    source = (WEB / "local.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    missing: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
            continue
        if node.name in _PURE_LOCAL_HELPERS:
            continue
        body = node.body
        # Skip a leading docstring; the guard follows it.
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        first = ast.get_source_segment(source, body[0]) if body else ""
        if "require_local" not in (first or ""):
            missing.append(node.name)
    assert missing == [], (
        f"these functions in web/local.py do not begin with require_local: {missing}"
    )


def test_pages_do_not_import_streamlit_at_module_level() -> None:
    """Streamlit must only be imported inside functions, so CI needs no extra.

    ``web/ui.py`` imports Streamlit at call time; every other module goes through
    ``web.ui``. A module-level import would break the core install and the CI job,
    which runs a plain ``uv sync --frozen`` with no extras.
    """
    offenders: list[str] = []
    for path in _web_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.Import) and any(a.name == "streamlit" for a in node.names):
                offenders.append(str(path.relative_to(REPO_ROOT)))
            if isinstance(node, ast.ImportFrom) and node.module == "streamlit":
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], f"these modules import streamlit at module level: {offenders}"


def test_app_module_does_not_import_streamlit_at_module_level() -> None:
    """The entry point must import Streamlit lazily, like every other module."""
    tree = ast.parse((WEB / "app.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import) and any(a.name == "streamlit" for a in node.names):
            pytest.fail("web/app.py imports streamlit at module level")
        if isinstance(node, ast.ImportFrom) and node.module == "streamlit":
            pytest.fail("web/app.py imports streamlit at module level")


def test_no_web_module_reads_a_dotenv_file_directly() -> None:
    """The website must not open ``.env``; settings loading is not web's job."""
    offenders = [
        str(p.relative_to(REPO_ROOT))
        for p in _web_sources()
        if '".env"' in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


# --------------------------------------------------------------------------
# 2. Runtime isolation: direct calls still refuse
# --------------------------------------------------------------------------


def test_hosted_refuses_validation_when_called_directly() -> None:
    """Calling run_validation in hosted mode must refuse, not run."""
    from web import local

    with pytest.raises(HostedModeRefused):
        local.run_validation(HOSTED, "owner/name", 1)


def test_hosted_refuses_publishing_when_called_directly() -> None:
    from web import local

    with pytest.raises(HostedModeRefused):
        local.real_publish(
            HOSTED,
            "owner/name",
            1,
            expected_patch_sha256="a" * 64,
            confirmation="PUBLISH owner/name main aaaaaaaaaaaa",
        )


def test_hosted_refuses_reading_the_database_when_called_directly() -> None:
    from web import local

    with pytest.raises(HostedModeRefused):
        local.open_local_db(HOSTED)


def test_hosted_refuses_loading_a_publish_context() -> None:
    from web import local

    with pytest.raises(HostedModeRefused):
        local.publish_context(HOSTED, "owner/name", 1)


def test_hosted_refuses_recording_an_approval() -> None:
    from web import local

    with pytest.raises(HostedModeRefused):
        local.apply_approval(HOSTED, "owner/name", 1, "approve")


def test_hosted_refuses_planning_a_publish() -> None:
    from web import local

    with pytest.raises(HostedModeRefused):
        local.dry_run_publish(HOSTED, "owner/name", 1)


def test_hosted_refuses_sandbox_detection() -> None:
    from web import local

    with pytest.raises(HostedModeRefused):
        local.sandbox_status(HOSTED)


def test_hosted_refusal_does_not_touch_the_database(monkeypatch) -> None:
    """The refusal must happen before any file is opened.

    ``sqlite3.connect`` is replaced with a tripwire: if hosted mode reached the
    database, this test would see the tripwire fire instead of the policy
    refusal.
    """
    import sqlite3

    def tripwire(*args, **kwargs):
        raise AssertionError("hosted mode opened the database")

    monkeypatch.setattr(sqlite3, "connect", tripwire)
    from web import local

    with pytest.raises(HostedModeRefused):
        local.open_local_db(HOSTED)


def test_hosted_refusal_does_not_read_the_publish_token(monkeypatch) -> None:
    """Publishing must refuse before the credential module is even consulted."""
    monkeypatch.setenv("FORGEFLOW_PUBLISH_TOKEN", "should-never-be-read")
    from forgeflow import publish_token

    def tripwire(*args, **kwargs):
        raise AssertionError("hosted mode read the publishing token")

    monkeypatch.setattr(publish_token, "read_publish_token", tripwire)
    from web import local

    with pytest.raises(HostedModeRefused):
        local.real_publish(
            HOSTED,
            "owner/name",
            1,
            expected_patch_sha256="a" * 64,
            confirmation="anything",
        )


def test_local_mode_does_not_refuse_before_reading_the_database(tmp_path) -> None:
    """In local mode the guard passes, so the failure is operational, not policy.

    This proves the refusal above is mode-based and not an unconditional error:
    with a local mode and a missing database, the error is a FileNotFoundError,
    never a HostedModeRefused.
    """
    from web import local

    missing = tmp_path / "nope.db"
    with pytest.raises(FileNotFoundError):
        local.open_local_db(LOCAL, db_path=missing)


# --------------------------------------------------------------------------
# 3. The publisher is not loaded in a hosted process
# --------------------------------------------------------------------------


def test_publisher_module_is_not_imported_by_importing_the_web_package() -> None:
    """Importing web (and its page modules) must not pull in the publisher.

    ``web.local`` imports the publisher inside functions, so a hosted process
    that never calls a local-only function never loads it. This test runs in a
    fresh interpreter to avoid a previously imported module masking the result.

    ``web.app`` guards its ``main()`` behind ``__name__ == "__main__"``, so it is
    importable without Streamlit and is imported here too.
    """
    import subprocess

    script = (
        "import sys\n"
        "import web\n"
        "import web.app\n"
        "import web.clients, web.service, web.modes, web.limits, web.session\n"
        "import web.analysis, web.context, web.patch_agent, web.local\n"
        "from web.views import (home, finder, rules, analysis_page, patch_page,\n"
        "                       validation_page, publish_page, demo_page, safety,\n"
        "                       docs_page, contribute)\n"
        "leaked = [m for m in sys.modules "
        "if m in ('forgeflow.publisher', 'forgeflow.publish_token')]\n"
        "print('LEAKED:' + ','.join(leaked))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "LEAKED:" in result.stdout
    leaked = result.stdout.split("LEAKED:", 1)[1].strip()
    assert leaked == "", f"importing the web pages loaded: {leaked}"


def test_builtins_open_is_not_called_with_env_during_import() -> None:
    """Importing the web pages must not read a .env file.

    A tripwire on ``open`` fails the import if any module tries to open a path
    ending in ``.env`` while the pages are being imported.
    """
    opened: list[str] = []
    real_open = builtins.open

    def watching_open(file, *args, **kwargs):
        with contextlib.suppress(Exception):
            opened.append(str(file))
        return real_open(file, *args, **kwargs)

    for name in [m for m in sys.modules if m.startswith("web")]:
        del sys.modules[name]

    builtins.open = watching_open
    try:
        # Import the modules under the tripwire so an open() during import is seen.
        for name in (
            "web",
            "web.app",
            "web.clients",
            "web.service",
            "web.modes",
            "web.limits",
            "web.views.home",
            "web.views.safety",
        ):
            importlib.import_module(name)
    finally:
        builtins.open = real_open

    offenders = [p for p in opened if p.endswith(".env")]
    assert offenders == [], f"importing the web pages opened: {offenders}"
