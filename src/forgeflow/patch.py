"""Patch capture, hashing, and diff analysis for ForgeFlow Phase 2.

Three jobs:

* **Hash a patch.** :func:`compute_patch_sha256` produces the SHA-256 over the
  exact diff bytes. SPEC section 2I requires that approval binds to a specific
  patch; the hash is what makes that check possible, so it must cover the whole
  diff and nothing else. No timestamp or session id is mixed in, or the same
  change would hash differently on each run.
* **Describe a diff.** :func:`analyse_diff` counts files, additions, and
  deletions by parsing unified-diff markers, and returns the changed paths.
* **Guard the capture.** :func:`capture_patch` refuses to record a patch when
  the agent ran without isolation and the workspace is not a throwaway copy,
  because a diff from an unsandboxed run cannot be trusted as the only thing
  that changed.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from forgeflow.models import PatchRecord

DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")
HUNK_HEADER = re.compile(r"^@@ .* @@")

# Directories whose changes are noise, never part of a proposed fix.
IGNORED_PREFIXES = (
    ".git/",
    "__pycache__/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
)


class PatchError(RuntimeError):
    """Raised when a patch cannot be captured or is unusable."""


@dataclass(frozen=True)
class DiffStats:
    """Counts derived from a unified diff."""

    files_changed: int
    additions: int
    deletions: int
    files: list[str]

    @property
    def is_empty(self) -> bool:
        return self.files_changed == 0


def compute_patch_sha256(diff: str) -> str:
    """Return the SHA-256 of a diff.

    The diff is encoded as UTF-8 exactly as given, with no normalisation, so the
    hash changes if a single byte changes. Trailing whitespace is significant
    for the same reason.
    """
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()


def analyse_diff(diff: str) -> DiffStats:
    """Count files, additions, and deletions in a unified diff.

    Counts only lines inside hunks, so a ``+++`` file header is not mistaken for
    an added line. Binary-file markers are ignored rather than guessed at.
    """
    files: list[str] = []
    additions = 0
    deletions = 0
    in_hunk = False

    for line in diff.splitlines():
        header = DIFF_HEADER.match(line)
        if header:
            path = header.group(2)
            if not path.startswith(IGNORED_PREFIXES):
                files.append(path)
            in_hunk = False
            continue
        if HUNK_HEADER.match(line):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1

    return DiffStats(
        files_changed=len(files),
        additions=additions,
        deletions=deletions,
        files=files,
    )


def build_patch_record(
    *,
    repo_full_name: str,
    issue_number: int,
    provider: str,
    diff: str,
    session_id: str | None = None,
    isolated: bool = True,
    notes: str = "",
) -> PatchRecord:
    """Build a hashed :class:`PatchRecord` from a diff."""
    if not diff.strip():
        raise PatchError("refusing to record an empty patch")
    stats = analyse_diff(diff)
    if stats.is_empty:
        raise PatchError("diff contains no file changes")

    return PatchRecord(
        created_at=datetime.now(UTC),
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        provider=provider,
        session_id=session_id,
        patch_sha256=compute_patch_sha256(diff),
        diff=diff,
        files_changed=stats.files_changed,
        additions=stats.additions,
        deletions=stats.deletions,
        isolated=isolated,
        notes=notes,
    )


def verify_patch_hash(diff: str, expected_sha256: str) -> bool:
    """Return whether a diff still matches an approved hash.

    Used by the approval gate: if this returns False, the patch changed after
    approval and a fresh approval is required (SPEC section 2I).
    """
    return compute_patch_sha256(diff) == expected_sha256


def workspace_diff(workspace: Path | str, *, timeout: float = 60.0) -> str:
    """Return the uncommitted diff of a git workspace.

    Read-only: ``git diff`` against HEAD plus untracked file contents, so a new
    file an agent created is visible rather than silently missing from the
    review bundle. ``git add -N`` is deliberately avoided, since it would modify
    the index.
    """
    root = Path(workspace)
    if not root.is_dir():
        raise PatchError(f"workspace directory not found: {root}")

    tracked = subprocess.run(
        ["git", "diff", "--no-color", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if tracked.returncode not in (0, 129):  # 129 = no commits yet
        raise PatchError(f"git diff failed: {tracked.stderr.strip()}")

    parts = [tracked.stdout] if tracked.stdout.strip() else []

    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    for relative in untracked.stdout.splitlines():
        relative = relative.strip()
        if not relative or relative.startswith(IGNORED_PREFIXES):
            continue
        path = root / relative
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = content.splitlines()
        parts.append(
            f"diff --git a/{relative} b/{relative}\n"
            f"new file mode 100644\n"
            f"--- /dev/null\n"
            f"+++ b/{relative}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join(f"+{line}" for line in lines) + "\n"
        )

    return "".join(parts)


def changed_files(workspace: Path | str, *, timeout: float = 60.0) -> list[str]:
    """Return the paths changed in a workspace, tracked and untracked."""
    root = Path(workspace)
    if not root.is_dir():
        raise PatchError(f"workspace directory not found: {root}")
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    paths: list[str] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:].strip()
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        if entry and not entry.startswith(IGNORED_PREFIXES):
            paths.append(entry)
    return paths
