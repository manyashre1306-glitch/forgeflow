"""Validation and quality gates for ForgeFlow Phase 3 (SPEC section 7).

Runs the target repository's documented tests and lint/type checks, inspects the
diff for dangerous changes, and decides whether the task may proceed to
publishing. Three rules shape this module:

* **No check runs on the host.** Every command goes through the sandbox. When
  isolation is unavailable the check is recorded as SKIPPED with the real
  reason, never as PASSED. There is no host fallback.
* **A pass requires execution evidence.** ``CheckStatus.PASSED`` is only ever set
  from an observed exit code of zero. Every check carries its command, exit
  code, and captured output.
* **Failure blocks publishing.** A failed check moves the task to
  ``failed_validation`` and refuses to publish unless a human records a
  documented exception, which is itself stored.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from forgeflow.models import (
    CheckKind,
    CheckStatus,
    DiffFinding,
    ValidationCheck,
    ValidationReport,
)
from forgeflow.patch import analyse_diff
from forgeflow.sandbox import IsolationError, Sandbox, SandboxLimits

MAX_REPAIR_ATTEMPTS = 2

# Commands that can destroy work or reach outside the workspace. These are
# refused even inside the sandbox, because a validation step has no reason to
# run them and a repository's instructions must not be able to introduce them.
DESTRUCTIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("recursive_delete", re.compile(r"\brm\s+(-[A-Za-z]*\s+)*-?[A-Za-z]*[rf]{1,2}\b")),
    ("disk_write", re.compile(r"\bdd\b.*\bof=/dev/")),
    ("filesystem_format", re.compile(r"\bmkfs(\.\w+)?\b")),
    ("privilege_escalation", re.compile(r"\bsudo\b|\bsu\s+-")),
    ("remote_shell", re.compile(r"\b(curl|wget)\b[^|;]*\|\s*(ba|z|da)?sh\b")),
    ("git_force_push", re.compile(r"\bgit\s+push\b.*(--force|-f)\b")),
    ("git_history_rewrite", re.compile(r"\bgit\s+(filter-branch|rebase\s+--root)\b")),
    ("chmod_world", re.compile(r"\bchmod\s+(-R\s+)?777\b")),
    ("host_path_mount", re.compile(r"/var/run/docker\.sock|/etc/shadow|~/\.ssh")),
    ("credential_read", re.compile(r"\b(cat|less|head|tail|cp)\b[^|;]*(\.env\b|\.ssh/|id_rsa)")),
    ("network_egress_tool", re.compile(r"\bnc\b\s+-|/dev/tcp/")),
)

# Files that should not be in a contribution patch: build output, caches,
# editor state, and binaries.
GENERATED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "python_cache",
        re.compile(r"(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache)(/|$)"),
    ),
    ("compiled", re.compile(r"\.(pyc|pyo|so|o|a|dll|dylib)$")),
    ("build_dir", re.compile(r"(^|/)(dist|build|\.eggs|htmlcov|node_modules)(/|$)")),
    ("coverage", re.compile(r"(^|/)(\.coverage|coverage\.xml|\.coverage\.\w+)$")),
    ("editor", re.compile(r"(^|/)(\.idea|\.vscode)(/|$)|\.(swp|swo)$")),
    ("os_junk", re.compile(r"(^|/)(\.DS_Store|Thumbs\.db)$")),
    ("lockfile", re.compile(r"(^|/)(poetry\.lock|uv\.lock|package-lock\.json|yarn\.lock)$")),
)

# Dependency manifests: a change here is legitimate for some issues but always
# worth a human look, so it is reported as a finding rather than blocked.
DEPENDENCY_FILES = re.compile(
    r"(^|/)(pyproject\.toml|setup\.py|setup\.cfg|requirements[\w\-]*\.txt|"
    r"Pipfile|environment\.ya?ml|package\.json|Cargo\.toml|go\.mod)$"
)

# Test files: their deletion or weakening is a strong signal something is wrong.
TEST_FILE = re.compile(r"(^|/)(tests?|spec)/|(^|/)test_[\w\-]*\.py$|_test\.go$")

# Where validation output is echoed back to the operator. Kept short so a
# hostile repository cannot flood the log through a failing test.
MAX_OUTPUT_CHARS = 4000


@dataclass(frozen=True)
class ValidationCommands:
    """The commands to run, derived from the repository's own layout.

    Each field is ``None`` when the repository does not document or ship that
    kind of check, in which case nothing is run for it. Commands are never
    invented: a check only appears when a real signal (a config file, a
    dependency) justifies it.
    """

    tests: list[str] | None = None
    lint: list[str] | None = None
    format: list[str] | None = None
    types: list[str] | None = None


def _exists(root: Path, *names: str) -> bool:
    return any((root / name).exists() for name in names)


def detect_commands(root: Path | str) -> ValidationCommands:
    """Derive validation commands from what the repository actually contains.

    Detection is file-based and conservative. If a tool's configuration is
    absent, its check is omitted rather than guessed, because running a checker
    the project does not use would produce noise, not signal.
    """
    base = Path(root)
    if not base.is_dir():
        return ValidationCommands()

    has_tests = (
        _exists(base, "tests", "test", "spec")
        or _exists(base, "pytest.ini", "tox.ini", "noxfile.py")
        or _exists(base, "pyproject.toml", "setup.cfg")
    )
    tests = ["python", "-m", "pytest", "-q"] if has_tests else None

    lint = (
        ["ruff", "check", "."]
        if _exists(base, "ruff.toml", ".ruff.toml", "pyproject.toml")
        else None
    )
    fmt = ["ruff", "format", "--check", "."] if lint is not None else None
    types = ["mypy", "."] if _exists(base, "mypy.ini", ".mypy.ini", "pyproject.toml") else None

    return ValidationCommands(tests=tests, lint=lint, format=fmt, types=types)


def scan_command(command: list[str]) -> DiffFinding | None:
    """Return a blocking finding if ``command`` looks destructive.

    Applied to every command before it runs, so a command injected by repository
    text cannot reach the sandbox.
    """
    joined = " ".join(command)
    for name, pattern in DESTRUCTIVE_PATTERNS:
        if pattern.search(joined):
            return DiffFinding(
                category="destructive_command",
                severity="blocking",
                detail=f"refused to run a destructive command matching '{name}'",
                evidence=joined,
            )
    return None


def _diff_paths(diff: str) -> list[str]:
    """Return every path the diff touches, including noise-filtered ones.

    ``analyse_diff`` deliberately ignores cache directories when counting a
    patch. Generated-file detection needs the opposite: those are exactly the
    paths it must flag, so this reads the headers directly.
    """
    paths: list[str] = []
    for line in diff.splitlines():
        header = re.match(r"^diff --git a/(.+?) b/(.+)$", line)
        if header:
            paths.append(header.group(2).strip())
            continue
        added = re.match(r"^\+\+\+ b/(.+)$", line)
        if added:
            path = added.group(1).strip()
            if path not in paths:
                paths.append(path)
    return paths


def analyse_diff_findings(diff: str) -> list[DiffFinding]:
    """Inspect a diff for the hazards SPEC section 7 lists.

    Categories: unrelated file changes, destructive operations, suspicious
    dependency changes, generated files, and unexpected modifications. Deleting
    a test file or adding a destructive command is ``blocking``; the rest are
    ``warning`` so a human decides.
    """
    findings: list[DiffFinding] = []
    # analyse_diff is still used for its counts, but generated-file detection
    # must see the raw paths it filters out.
    analyse_diff(diff)

    for path in _diff_paths(diff):
        for name, pattern in GENERATED_PATTERNS:
            if pattern.search(path):
                findings.append(
                    DiffFinding(
                        category="generated_file",
                        severity="warning",
                        path=path,
                        detail=f"patch touches a generated or cached file ({name})",
                    )
                )
                break

        if DEPENDENCY_FILES.search(path):
            findings.append(
                DiffFinding(
                    category="dependency_change",
                    severity="warning",
                    path=path,
                    detail="patch modifies a dependency manifest; review the change",
                )
            )

    for path, added, removed in _per_file_changes(diff):
        if TEST_FILE.search(path) and removed > added:
            findings.append(
                DiffFinding(
                    category="test_weakening",
                    severity="blocking",
                    path=path,
                    detail=(
                        "patch removes more test lines than it adds "
                        f"({removed} removed, {added} added)"
                    ),
                )
            )
        elif TEST_FILE.search(path) and removed > 0:
            findings.append(
                DiffFinding(
                    category="test_modified",
                    severity="warning",
                    path=path,
                    detail=(
                        f"patch changes an existing test file ({removed} removed, {added} added)"
                    ),
                )
            )

    findings.extend(_scan_added_lines(diff))
    return findings


def _per_file_changes(diff: str) -> list[tuple[str, int, int]]:
    """Return (path, additions, deletions) for each file in the diff."""
    results: list[tuple[str, int, int]] = []
    current: str | None = None
    added = 0
    removed = 0
    in_hunk = False

    def flush() -> None:
        if current is not None:
            results.append((current, added, removed))

    for line in diff.splitlines():
        header = re.match(r"^\+\+\+ b/(.+)$", line)
        if header:
            flush()
            current = header.group(1).strip()
            added = 0
            removed = 0
            in_hunk = False
            continue
        if line.startswith("@@ "):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1

    flush()
    return results


def _scan_added_lines(diff: str) -> list[DiffFinding]:
    """Scan only added lines for destructive operations."""
    findings: list[DiffFinding] = []
    for line in diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        added = line[1:]
        for name, pattern in DESTRUCTIVE_PATTERNS:
            if pattern.search(added):
                findings.append(
                    DiffFinding(
                        category="destructive_command",
                        severity="blocking",
                        detail=f"patch adds a line matching '{name}'",
                        evidence=added.strip()[:200],
                    )
                )
                break
    return findings


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n... [truncated, {len(text)} chars total]"


class Validator:
    """Runs the quality gates for one patch."""

    def __init__(
        self,
        sandbox: Sandbox,
        *,
        limits: SandboxLimits | None = None,
        timeout: float = 900.0,
    ) -> None:
        self._sandbox = sandbox
        self._limits = limits or SandboxLimits(timeout_seconds=timeout)
        self._timeout = timeout

    @property
    def isolated(self) -> bool:
        return self._sandbox.is_isolated

    def _skipped(
        self, kind: CheckKind, name: str, command: list[str], reason: str
    ) -> ValidationCheck:
        return ValidationCheck(
            kind=kind,
            name=name,
            command=command,
            status=CheckStatus.SKIPPED,
            skip_reason=reason,
            limitation=reason,
        )

    def _run_check(
        self,
        kind: CheckKind,
        name: str,
        command: list[str],
        workspace: Path | str,
    ) -> ValidationCheck:
        """Run one command in the sandbox and record the evidence."""
        refusal = scan_command(command)
        if refusal is not None:
            return ValidationCheck(
                kind=kind,
                name=name,
                command=command,
                status=CheckStatus.SKIPPED,
                skip_reason=refusal.detail,
                limitation=refusal.detail,
            )

        if not self.isolated:
            return self._skipped(
                kind,
                name,
                command,
                f"no container isolation available ({self._sandbox.explain()})",
            )

        started = time.monotonic()
        try:
            result = self._sandbox.run(command, workspace=workspace, limits=self._limits)
        except IsolationError as exc:
            return self._skipped(kind, name, command, f"isolation refused: {exc}")
        duration = time.monotonic() - started

        output = _truncate((result.stdout or "") + (result.stderr or ""))

        if result.timed_out:
            return ValidationCheck(
                kind=kind,
                name=name,
                command=command,
                status=CheckStatus.ERRORED,
                exit_code=result.exit_code,
                output=output,
                duration_seconds=duration,
                timed_out=True,
                limitation=f"command exceeded the {self._timeout}s limit",
            )

        status = CheckStatus.PASSED if result.exit_code == 0 else CheckStatus.FAILED
        return ValidationCheck(
            kind=kind,
            name=name,
            command=command,
            status=status,
            exit_code=result.exit_code,
            output=output,
            duration_seconds=duration,
        )

    def validate(
        self,
        repo_full_name: str,
        issue_number: int,
        patch_sha256: str,
        diff: str,
        workspace: Path | str,
        *,
        commands: ValidationCommands | None = None,
        repair_attempts: int = 0,
    ) -> ValidationReport:
        """Run every applicable check and return the full report.

        The diff is analysed before any command runs, because a blocking finding
        means the patch should not be executed at all.
        """
        root = Path(workspace)
        resolved = commands if commands is not None else detect_commands(root)

        findings = analyse_diff_findings(diff)
        checks: list[ValidationCheck] = []
        limitations: list[str] = []

        if not self.isolated:
            limitations.append(
                f"no container isolation: {self._sandbox.explain()}. "
                "Repository tests were NOT executed."
            )

        planned: list[tuple[CheckKind, str, list[str] | None]] = [
            (CheckKind.TESTS, "pytest", resolved.tests),
            (CheckKind.LINT, "ruff check", resolved.lint),
            (CheckKind.FORMAT, "ruff format --check", resolved.format),
            (CheckKind.TYPES, "mypy", resolved.types),
        ]

        for kind, name, command in planned:
            if command is None:
                checks.append(
                    self._skipped(
                        kind,
                        name,
                        [],
                        "no configuration for this check was found in the repository",
                    )
                )
                continue
            checks.append(self._run_check(kind, name, command, root))

        return ValidationReport(
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            patch_sha256=patch_sha256,
            isolated=self.isolated,
            checks=checks,
            findings=findings,
            repair_attempts=repair_attempts,
            environment_limitations=limitations,
        )


def can_repair(report: ValidationReport) -> bool:
    """Whether another automated repair attempt is allowed.

    SPEC section 7 allows at most two repairs after the initial attempt.
    """
    if not report.blocking_failures:
        return False
    return report.repair_attempts < MAX_REPAIR_ATTEMPTS


def build_validator(
    *,
    sandbox: Sandbox | None = None,
    timeout: float = 900.0,
    app_config: object = None,
) -> Validator:
    """Construct a validator with the detected sandbox and kill switch."""
    from forgeflow.sandbox import build_sandbox

    resolved = sandbox if sandbox is not None else build_sandbox(app_config=app_config)
    return Validator(resolved, timeout=timeout)
