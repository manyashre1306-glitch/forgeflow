"""Repository context extraction for ForgeFlow Phase 2 (SPEC section 2D).

Clones a shortlisted repository shallowly into a gitignored workspace and
extracts the issue, README, contribution guidance, test layout, and the files
most likely to be relevant to the issue.

Two rules shape this module:

* **Every repository and issue string is untrusted.** Nothing here returns bare
  prose: text is wrapped in :class:`~forgeflow.models.UntrustedText`, which
  carries its provenance and renders inside an explicit data fence. Downstream
  code cannot accidentally interpolate repository text into an instruction.
* **Every read is size-limited.** A hostile or merely enormous repository must
  not be able to exhaust memory or a token budget, so file counts, per-file
  bytes, and total bytes are all capped and any truncation is recorded.

Only read-only git commands are used. Nothing in this module writes to a remote.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from forgeflow.models import (
    AIPolicy,
    ContributionRules,
    FileExcerpt,
    Issue,
    RepoContext,
    UntrustedText,
)

# --- limits -----------------------------------------------------------------

MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 512 * 1024
MAX_RELEVANT_FILES = 20
MAX_TEST_LAYOUT_ENTRIES = 200
CLONE_TIMEOUT_SECONDS = 300

# Files never worth reading: binaries, lockfiles, vendored code, VCS data.
SKIP_DIRECTORIES = frozenset(
    {
        ".git",
        ".github/workflows",
        "node_modules",
        "vendor",
        "third_party",
        "dist",
        "build",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)

SKIP_SUFFIXES = frozenset(
    {
        ".pyc",
        ".pyo",
        ".so",
        ".dylib",
        ".dll",
        ".exe",
        ".bin",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".ico",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".whl",
        ".lock",
        ".min.js",
        ".min.css",
    }
)

README_CANDIDATES = ("README.md", "README.rst", "README.txt", "README")
CONTRIBUTING_CANDIDATES = (
    "CONTRIBUTING.md",
    "CONTRIBUTING.rst",
    "CONTRIBUTING.txt",
    "CONTRIBUTING",
    ".github/CONTRIBUTING.md",
    "docs/CONTRIBUTING.md",
    "doc/CONTRIBUTING.md",
    "doc/source/development/contributing.rst",
    "doc/developers/contributing.rst",
)
TEST_DIRECTORY_NAMES = frozenset({"tests", "test", "testing"})

# --- contribution-rule detection --------------------------------------------

# Ordered: the first matching group wins, so the strongest signal is reported.
# Every pattern is deliberately explicit; a bare "AI" is never enough, because
# it appears in ordinary prose ("AI/ML engineering").
AI_POLICY_PATTERNS: tuple[tuple[AIPolicy, tuple[str, ...]], ...] = (
    (
        AIPolicy.PROHIBITED,
        (
            r"\bno\s+ai[- ]generated\b",
            r"\bdo(?:es)?\s+not\s+accept\s+(?:ai|llm|machine[- ]generated)",
            r"\bnot\s+accept(?:ed|ing)?\s+ai[- ]generated",
            r"\bai[- ]generated\s+(?:code|contributions?|content)\s+"
            r"(?:is|are)\s+not\s+(?:allowed|accepted|permitted)",
            r"\bprohibit(?:ed|s)?\s+ai\b",
            r"\bai\s+contributions?\s+(?:are\s+)?prohibit",
            r"\bwill\s+not\s+(?:review|merge|accept)\s+ai[- ]generated",
            r"\bbanned\s+ai\b",
        ),
    ),
    (
        AIPolicy.RESTRICTED,
        (
            r"\bmust\s+disclose\b",
            r"\bdisclos(?:e|ure)\b.{0,40}\bai\b",
            r"\bai[- ]assisted\b.{0,60}\b(?:disclos|review|supervis)",
            r"\bhuman\s+(?:review|oversight|responsib)",
            r"\battest\b.{0,40}\b(?:human|own)\b",
            r"\bfully\s+(?:review|understand|own)\b.{0,40}\b(?:code|change)",
        ),
    ),
    (
        AIPolicy.ALLOWED,
        (
            r"\bai\s+tools?\s+(?:are\s+)?(?:welcome|allowed|permitted|encouraged)",
            r"\bai[- ]assisted\s+contributions?\s+(?:are\s+)?(?:welcome|allowed|permitted)",
            r"\b(?:use|using)\s+ai\b.{0,40}\b(?:allowed|permitted|welcome|fine)",
            r"\bgenerative\s+ai\b.{0,40}\b(?:allowed|permitted|welcome)",
        ),
    ),
)

REQUIRES_TESTS_PATTERNS = (
    r"\btests?\s+(?:are|is)\s+required\b",
    r"\bmust\s+(?:add|include|write)\s+(?:a\s+)?test",
    r"\binclude\s+tests?\b",
    r"\badd\s+(?:unit\s+)?tests?\b",
    r"\bnew\s+features?\s+.{0,30}\btests?\b",
)

REQUIRES_SIGNOFF_PATTERNS = (
    r"\bsigned-off-by\b",
    r"\bdeveloper\s+certificate\s+of\s+origin\b",
    r"\bdco\b",
    r"\bcommit\s+sign(?:ing|ature)\b",
)

REQUIRES_CLA_PATTERNS = (
    r"\bcontributor\s+licen[cs]e\s+agreement\b",
    r"\bcla\b",
)


class RepositoryContextError(RuntimeError):
    """Raised when a workspace cannot be prepared or read."""


# --- workspace management ---------------------------------------------------


def default_workspace_root() -> Path:
    """Return the gitignored directory that holds cloned workspaces."""
    return Path("data/workspaces")


def workspace_path_for(root: Path | str, repo_full_name: str, issue_number: int) -> Path:
    """Return the deterministic workspace directory for one issue.

    The repository name is flattened so the path stays one level deep and
    predictable, which keeps cleanup simple.
    """
    slug = repo_full_name.replace("/", "__")
    return Path(root) / f"{slug}__issue-{issue_number}"


def shallow_clone(
    repo_full_name: str,
    destination: Path | str,
    *,
    remote_url: str | None = None,
    source_path: Path | str | None = None,
    timeout: int = CLONE_TIMEOUT_SECONDS,
) -> Path:
    """Clone a repository into ``destination`` with ``--depth 1``.

    Exactly one of ``remote_url`` or ``source_path`` is used. ``source_path``
    exists so tests can clone a local fixture repository with no network access.
    Any existing destination is removed first, so the workspace is disposable.
    """
    if remote_url is None and source_path is None:
        remote_url = f"https://github.com/{repo_full_name}.git"

    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    # git ignores --depth when cloning a plain local path; a file:// URI makes it
    # a real fetch, so the shallow limit is honored.
    origin = Path(source_path).resolve().as_uri() if source_path is not None else str(remote_url)
    command = [
        "git",
        "clone",
        "--depth",
        "1",
        "--single-branch",
        "--no-tags",
        "--quiet",
        origin,
        str(destination),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RepositoryContextError(
            f"git clone timed out after {timeout}s for {repo_full_name}"
        ) from exc
    except FileNotFoundError as exc:
        raise RepositoryContextError("git is not installed or not on PATH") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RepositoryContextError(
            f"git clone failed for {repo_full_name} (exit {completed.returncode}): {detail}"
        )
    return destination


# --- size-limited reading ---------------------------------------------------


def read_limited_text(
    path: Path,
    *,
    source: str,
    max_bytes: int = MAX_FILE_BYTES,
) -> UntrustedText | None:
    """Read a file as untrusted text, truncating at ``max_bytes``.

    Returns ``None`` when the path is not a file or cannot be decoded as UTF-8,
    so a binary file is skipped rather than mangled.
    """
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None

    original_length = len(raw)
    truncated = original_length > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None

    return UntrustedText(
        text=text,
        source=source,
        truncated=truncated,
        original_length=original_length,
    )


def _should_skip(relative: Path) -> bool:
    if any(part in SKIP_DIRECTORIES for part in relative.parts):
        return True
    name = relative.name
    if name in {"package-lock.json", "poetry.lock", "uv.lock", "Cargo.lock"}:
        return True
    return any(name.endswith(suffix) for suffix in SKIP_SUFFIXES)


def detect_test_layout(root: Path) -> list[str]:
    """List directories and files that look like the project's test layout."""
    found: list[str] = []
    for path in sorted(root.rglob("*")):
        if len(found) >= MAX_TEST_LAYOUT_ENTRIES:
            break
        relative = path.relative_to(root)
        if _should_skip(relative):
            continue
        is_test_dir = path.is_dir() and path.name in TEST_DIRECTORY_NAMES
        is_test_file = path.is_file() and (
            path.name in {"tox.ini", "pytest.ini", "conftest.py", "noxfile.py"}
            or (path.name.startswith("test_") and path.suffix == ".py")
        )
        if is_test_dir or is_test_file:
            found.append(str(relative))
    return found


def _readme(root: Path) -> UntrustedText | None:
    for name in README_CANDIDATES:
        text = read_limited_text(root / name, source=name)
        if text is not None:
            return text
    return None


def _contributing(root: Path) -> tuple[list[UntrustedText], list[str]]:
    documents: list[UntrustedText] = []
    sources: list[str] = []
    for name in CONTRIBUTING_CANDIDATES:
        text = read_limited_text(root / name, source=name)
        if text is not None:
            documents.append(text)
            sources.append(name)
    return documents, sources


# --- contribution rules -----------------------------------------------------


def _matching_lines(text: str, patterns: tuple[str, ...]) -> list[str]:
    """Return the lines matching any pattern, trimmed to a readable length."""
    hits: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        for pattern in patterns:
            if re.search(pattern, lowered):
                hits.append(stripped[:300])
                break
    return hits


def detect_contribution_rules(
    contributing_docs: list[UntrustedText],
    readme: UntrustedText | None = None,
) -> ContributionRules:
    """Detect contribution rules, including any stated AI policy.

    Detection is keyword based and conservative. A verdict of ``prohibited`` or
    ``restricted`` always carries the verbatim matching line as evidence, so the
    result is auditable rather than a black-box guess. When nothing matches, the
    policy stays ``unknown`` rather than defaulting to ``allowed``: an absent
    statement is not permission.
    """
    documents = list(contributing_docs)
    if readme is not None:
        documents.append(readme)

    rules = ContributionRules(sources=[doc.source for doc in contributing_docs])

    ai_policy = AIPolicy.UNKNOWN
    evidence: list[str] = []
    for policy, patterns in AI_POLICY_PATTERNS:
        for doc in documents:
            hits = _matching_lines(doc.text, patterns)
            if hits:
                ai_policy = policy
                evidence = [f"{doc.source}: {hit}" for hit in hits[:5]]
                break
        if ai_policy is not AIPolicy.UNKNOWN:
            break

    combined = "\n".join(doc.text for doc in documents)
    rules.ai_policy = ai_policy
    rules.ai_policy_evidence = evidence
    rules.requires_tests = bool(_matching_lines(combined, REQUIRES_TESTS_PATTERNS))
    rules.requires_signoff = bool(_matching_lines(combined, REQUIRES_SIGNOFF_PATTERNS))
    rules.requires_cla = bool(_matching_lines(combined, REQUIRES_CLA_PATTERNS))
    return rules


# --- relevant-file selection ------------------------------------------------


# A bare path in prose, for example "sklearn/preprocessing/_data.py".
_PATH_IN_PROSE = re.compile(
    r"\b[\w.\-]+(?:/[\w.\-]+)*\.(?:py|pyi|rst|md|toml|cfg|ini|txt|json|yaml|yml|sql)\b"
)
_IDENTIFIER = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{3,})\b")

# Words too common to be useful as search terms.
STOPWORDS = frozenset(
    {
        "that",
        "this",
        "with",
        "from",
        "have",
        "when",
        "what",
        "which",
        "there",
        "their",
        "would",
        "should",
        "could",
        "about",
        "after",
        "before",
        "issue",
        "error",
        "expected",
        "actual",
        "traceback",
        "steps",
        "reproduce",
        "please",
        "thanks",
        "using",
        "used",
        "does",
        "doesn",
        "cannot",
        "isn",
        "version",
        "python",
        "instead",
        "because",
        "between",
        "without",
        "however",
        "example",
        "support",
        "reported",
    }
)


def _issue_terms(issue: Issue) -> tuple[set[str], set[str]]:
    """Return (explicit paths, search terms) taken from the issue text."""
    text = f"{issue.title}\n{issue.body}"
    paths = set(_PATH_IN_PROSE.findall(text))
    terms = {match.lower() for match in _IDENTIFIER.findall(text) if match.lower() not in STOPWORDS}
    return paths, terms


def select_relevant_files(
    root: Path,
    issue: Issue,
    *,
    max_files: int = MAX_RELEVANT_FILES,
    max_bytes: int = MAX_FILE_BYTES,
) -> tuple[list[FileExcerpt], bool]:
    """Pick and read the repository files most likely to matter for the issue.

    Files named explicitly in the issue are taken first, then files whose path
    contains an identifier from the issue. Only text files are read, and the
    result is capped at ``max_files``. The boolean return reports whether the
    cap was reached.
    """
    explicit_paths, terms = _issue_terms(issue)

    candidates: list[tuple[int, str, Path]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if _should_skip(relative):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        relative_str = str(relative)

        score = 0
        if relative_str in explicit_paths:
            score = 100
        elif any(relative_str.endswith(candidate) for candidate in explicit_paths):
            score = 80
        else:
            lowered = relative_str.lower()
            score = sum(1 for term in terms if term in lowered)
        if score > 0:
            candidates.append((score, relative_str, path))

    # Highest score first, then a stable alphabetical tie-break.
    candidates.sort(key=lambda item: (-item[0], item[1]))

    excerpts: list[FileExcerpt] = []
    for _score, relative_str, path in candidates[:max_files]:
        excerpt = read_limited_text(path, source=relative_str, max_bytes=max_bytes)
        if excerpt is None:
            continue
        excerpts.append(
            FileExcerpt(
                path=relative_str,
                size_bytes=path.stat().st_size,
                content=excerpt,
            )
        )
    return excerpts, len(candidates) > max_files


# --- top level --------------------------------------------------------------


def build_repo_context(
    repo_full_name: str,
    issue: Issue,
    workspace: Path | str,
    *,
    max_relevant_files: int = MAX_RELEVANT_FILES,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> RepoContext:
    """Extract a context bundle from an already-cloned workspace.

    Separated from cloning so it can be tested against a fixture directory with
    no git and no network at all.
    """
    root = Path(workspace)
    if not root.is_dir():
        raise RepositoryContextError(f"workspace directory not found: {root}")

    issue_text = UntrustedText(
        text=f"{issue.title}\n\n{issue.body}".strip(),
        source=f"issue {repo_full_name}#{issue.number}",
    )
    readme = _readme(root)
    contributing_docs, _sources = _contributing(root)
    test_layout = detect_test_layout(root)
    relevant_files, files_truncated = select_relevant_files(
        root,
        issue,
        max_files=max_relevant_files,
        max_bytes=max_file_bytes,
    )
    rules = detect_contribution_rules(contributing_docs, readme)

    total = len(issue_text.text.encode("utf-8"))
    truncated = False
    if readme is not None:
        total += len(readme.text.encode("utf-8"))
        truncated = truncated or readme.truncated
    for doc in contributing_docs:
        total += len(doc.text.encode("utf-8"))
        truncated = truncated or doc.truncated

    kept: list[FileExcerpt] = []
    for excerpt in relevant_files:
        size = len(excerpt.content.text.encode("utf-8"))
        if total + size > max_total_bytes:
            truncated = True
            break
        total += size
        kept.append(excerpt)

    return RepoContext(
        repo_full_name=repo_full_name,
        issue_number=issue.number,
        workspace_path=str(root),
        issue=issue_text,
        readme=readme,
        contributing=contributing_docs,
        test_layout=test_layout,
        relevant_files=kept,
        contribution_rules=rules,
        total_bytes=total,
        truncated=truncated or files_truncated,
        cloned_at=datetime.now(UTC),
    )


def prepare_repo_context(
    repo_full_name: str,
    issue: Issue,
    *,
    workspace_root: Path | str = "data/workspaces",
    remote_url: str | None = None,
    source_path: Path | str | None = None,
    max_relevant_files: int = MAX_RELEVANT_FILES,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> RepoContext:
    """Clone a repository shallowly and extract its context bundle."""
    workspace = workspace_path_for(workspace_root, repo_full_name, issue.number)
    shallow_clone(
        repo_full_name,
        workspace,
        remote_url=remote_url,
        source_path=source_path,
    )
    return build_repo_context(
        repo_full_name,
        issue,
        workspace,
        max_relevant_files=max_relevant_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
    )


# --- workspace lifecycle ----------------------------------------------------


def workspace_exists(root: Path | str, repo_full_name: str, issue_number: int) -> bool:
    """Return whether a workspace directory exists for one issue."""
    return workspace_path_for(root, repo_full_name, issue_number).is_dir()


def remove_workspace(
    root: Path | str,
    repo_full_name: str,
    issue_number: int,
) -> bool:
    """Delete one workspace. Returns whether anything was removed.

    The path is rebuilt from ``workspace_path_for`` rather than taken from the
    caller, so a crafted repository name cannot direct the delete somewhere else.
    """
    target = workspace_path_for(root, repo_full_name, issue_number)
    if not target.is_dir():
        return False
    shutil.rmtree(target)
    return True


def list_workspaces(root: Path | str) -> list[Path]:
    """Return the workspace directories under ``root``, sorted by name."""
    base = Path(root)
    if not base.is_dir():
        return []
    return sorted(entry for entry in base.iterdir() if entry.is_dir())


def workspace_size_bytes(path: Path | str) -> int:
    """Return the total size of a workspace tree, following no symlinks."""
    base = Path(path)
    if not base.is_dir():
        return 0
    total = 0
    for entry in base.rglob("*"):
        if entry.is_symlink() or not entry.is_file():
            continue
        try:
            total += entry.stat().st_size
        except OSError:
            continue
    return total


def prune_workspaces(
    root: Path | str,
    *,
    keep: int = 5,
) -> list[Path]:
    """Keep the ``keep`` most recently modified workspaces and delete the rest.

    Clones are large and a full repository's history is not needed once its
    context has been extracted, so this bounds disk use. The newest workspaces
    are kept because those are the ones a current run is likely to still need.
    Returns the paths that were removed.
    """
    if keep < 0:
        raise ValueError("keep must not be negative")

    entries = list_workspaces(root)
    if keep == 0:
        stale = entries
    elif len(entries) <= keep:
        return []
    else:
        ordered = sorted(entries, key=lambda path: path.stat().st_mtime, reverse=True)
        stale = ordered[keep:]

    removed: list[Path] = []
    for path in stale:
        shutil.rmtree(path)
        removed.append(path)
    return removed


def cleanup_workspace(
    root: Path | str,
    repo_full_name: str,
    issue_number: int,
) -> bool:
    """Alias for :func:`remove_workspace`, used after a run finishes.

    Named separately because the call sites read differently: one is an explicit
    operator action, the other is post-run tidying.
    """
    return remove_workspace(root, repo_full_name, issue_number)
