"""Repository context from the read-only GitHub API, with no git clone.

Hosted mode must not clone a repository: cloning writes to disk and, more to the
point, the code that walks a clone is the same code the sandbox feeds. This
module builds the same :class:`~forgeflow.models.RepoContext` the clone-based
builder produces, but every byte comes from the read-only contents API under a
strict size cap, and nothing is written to disk.

It reuses the real building blocks rather than re-implementing them:

* the same candidate lists (``README_CANDIDATES``, ``CONTRIBUTING_PATHS``),
* the same file-scoring heuristic (:func:`forgeflow.repository_context.score_path_candidate`),
* the same rule detector (:func:`forgeflow.repository_context.detect_contribution_rules`),
* the same untrusted-text wrapper, so prompts stay fenced exactly as before.

The tree walk is bounded by depth and entry count, so a huge repository cannot
turn one page load into thousands of API calls.
"""

from __future__ import annotations

from dataclasses import dataclass

from forgeflow.github_client import GitHubReader
from forgeflow.models import FileExcerpt, Issue, RepoContext, UntrustedText
from forgeflow.repository_context import (
    CONTRIBUTING_CANDIDATES,
    MAX_FILE_BYTES,
    MAX_RELEVANT_FILES,
    README_CANDIDATES,
    SKIP_SUFFIXES,
    detect_contribution_rules,
    issue_terms,
    score_path_candidate,
)
from web.limits import MAX_FILES_FETCHED

#: Depth and breadth caps for the tree walk. A repository with a pathological
#: directory layout costs a bounded number of requests, not an unbounded one.
MAX_TREE_DEPTH = 3
MAX_TREE_ENTRIES = 400
MAX_LISTING_CALLS = 40

#: Directories never worth walking for a context bundle.
_SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "build",
        "dist",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
    }
)


class ContextFetchError(RuntimeError):
    """Raised when a hosted context cannot be built for a repository."""


@dataclass
class ContextBudget:
    """Tracks how many API requests a hosted context build has made."""

    listing_calls: int = 0
    file_calls: int = 0

    @property
    def total(self) -> int:
        return self.listing_calls + self.file_calls


def _fetch_first(
    client: GitHubReader,
    full_name: str,
    names: tuple[str, ...],
) -> tuple[str, str] | None:
    """Return ``(name, text)`` for the first candidate that exists, else None."""
    for name in names:
        text = client.get_file_content(full_name, name, max_bytes=MAX_FILE_BYTES)
        if text is not None:
            return name, text
    return None


def _walk_tree(
    client: GitHubReader,
    full_name: str,
    budget: ContextBudget,
) -> list[str]:
    """List repository file paths (POSIX) up to the depth and breadth caps.

    Returns paths relative to the repository root. Directories that never hold
    useful context are skipped, and the walk stops once it has seen
    ``MAX_TREE_ENTRIES`` entries or spent ``MAX_LISTING_CALLS`` requests. The
    entry type comes from GitHub, so a directory named ``v1.2`` is not mistaken
    for a file.
    """
    found: list[str] = []
    frontier: list[tuple[str, int]] = [("", 0)]

    while frontier and len(found) < MAX_TREE_ENTRIES and budget.listing_calls < MAX_LISTING_CALLS:
        path, depth = frontier.pop(0)
        budget.listing_calls += 1
        try:
            entries = client.list_directory_entries(full_name, path)
        except Exception:
            continue
        for entry in entries:
            name = entry.get("name", "")
            kind = entry.get("type", "file")
            if not name or name in _SKIP_DIRS:
                continue
            child = f"{path}/{name}".strip("/")
            if kind == "dir":
                if depth + 1 <= MAX_TREE_DEPTH:
                    frontier.append((child, depth + 1))
            elif kind == "file":
                found.append(child)
    return found


def build_hosted_context(
    client: GitHubReader,
    issue: Issue,
    *,
    budget: ContextBudget | None = None,
    max_files: int = MAX_RELEVANT_FILES,
) -> RepoContext:
    """Build a :class:`RepoContext` for ``issue`` using only read-only API calls.

    Raises :class:`ContextFetchError` when the repository itself cannot be read.
    A missing README or CONTRIBUTING is not an error: the context simply carries
    no document for it, and the rule detector returns ``unknown`` rather than
    inventing permission.
    """
    tracker = budget if budget is not None else ContextBudget()
    full_name = issue.repo_full_name

    try:
        client.get_repository(full_name)
    except Exception as exc:
        raise ContextFetchError(
            f"could not read repository {full_name}: {type(exc).__name__}"
        ) from exc

    issue_text = UntrustedText(
        text=f"{issue.title}\n\n{issue.body}".strip(),
        source=f"issue {full_name}#{issue.number}",
    )

    readme: UntrustedText | None = None
    readme_hit = _fetch_first(client, full_name, README_CANDIDATES)
    if readme_hit is not None:
        name, text = readme_hit
        tracker.file_calls += 1
        readme = UntrustedText(text=text, source=name)

    contributing_docs: list[UntrustedText] = []
    for name in CONTRIBUTING_CANDIDATES:
        text = client.get_file_content(full_name, name, max_bytes=MAX_FILE_BYTES)
        tracker.file_calls += 1
        if text is not None:
            contributing_docs.append(UntrustedText(text=text, source=name))

    tree = _walk_tree(client, full_name, tracker)
    explicit_paths, terms = issue_terms(issue)
    scored = [
        (score_path_candidate(path, explicit_paths, terms), path)
        for path in tree
        if not path.lower().endswith(tuple(SKIP_SUFFIXES))
    ]
    scored = [(score, path) for score, path in scored if score > 0]
    scored.sort(key=lambda item: (-item[0], item[1]))

    excerpts: list[FileExcerpt] = []
    for _score, path in scored[: min(max_files, MAX_FILES_FETCHED)]:
        text = client.get_file_content(full_name, path, max_bytes=MAX_FILE_BYTES)
        tracker.file_calls += 1
        if text is None:
            continue
        excerpts.append(
            FileExcerpt(
                path=path,
                size_bytes=len(text.encode("utf-8")),
                content=UntrustedText(text=text, source=path),
            )
        )

    rules = detect_contribution_rules(contributing_docs, readme)
    total = len(issue_text.text.encode("utf-8"))
    if readme is not None:
        total += len(readme.text.encode("utf-8"))
    for doc in contributing_docs:
        total += len(doc.text.encode("utf-8"))
    for excerpt in excerpts:
        total += len(excerpt.content.text.encode("utf-8"))

    return RepoContext(
        repo_full_name=full_name,
        issue_number=issue.number,
        workspace_path="(hosted: read-only API, no workspace)",
        issue=issue_text,
        readme=readme,
        contributing=contributing_docs,
        test_layout=[],
        relevant_files=excerpts,
        contribution_rules=rules,
        total_bytes=total,
        truncated=tracker.total >= MAX_LISTING_CALLS,
    )
