"""Apply a unified diff to file contents, strictly (SPEC section 8).

Publishing a branch means turning a stored diff back into real file contents. If
the diff does not apply cleanly to the base tree, ForgeFlow must refuse rather
than write a file that does not match the patch that was reviewed and approved.
Every failure here raises; nothing is guessed and nothing is partially applied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


class DiffApplyError(RuntimeError):
    """Raised when a diff cannot be applied. Never partially applies."""


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DEV_NULL = "/dev/null"

# Git metadata lines that end a hunk and carry no file content. Recognizing them
# is what makes a multi-file diff parse: without it, the "diff --git" line that
# starts the next file looks like a corrupt hunk line.
_GIT_METADATA = (
    "diff --git ",
    "index ",
    "new file mode ",
    "deleted file mode ",
    "old mode ",
    "new mode ",
    "similarity index ",
    "dissimilarity index ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "Binary files ",
    "GIT binary patch",
)


@dataclass
class FileChange:
    """One file's worth of changes from a unified diff."""

    old_path: str
    new_path: str
    hunks: list[tuple[int, int, list[str]]] = field(default_factory=list)
    is_new: bool = False
    is_deleted: bool = False

    @property
    def path(self) -> str:
        """The path to write, relative to the repository root."""
        return (
            self.new_path if self.is_new else (self.old_path if self.is_deleted else self.new_path)
        )


def _strip_prefix(path: str) -> str:
    """Turn ``a/foo.py`` or ``b/foo.py`` into ``foo.py``."""
    if path in (_DEV_NULL, ""):
        return path
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def parse_unified_diff(diff: str) -> list[FileChange]:
    """Parse a unified diff into per-file changes.

    Only the ``---``/``+++``/``@@`` form is understood, which is what git
    produces and what ForgeFlow stores. A malformed diff raises.
    """
    if not diff.strip():
        return []

    changes: list[FileChange] = []
    current: FileChange | None = None
    current_hunk: tuple[int, int, list[str]] | None = None
    lines = diff.splitlines()

    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(_GIT_METADATA):
            current_hunk = None
            index += 1
            continue
        if line.startswith("--- "):
            old_raw = line[4:].split("\t")[0].strip()
            if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
                raise DiffApplyError(f"diff has a '---' line with no '+++' line: {line!r}")
            new_raw = lines[index + 1][4:].split("\t")[0].strip()
            old_path = _strip_prefix(old_raw)
            new_path = _strip_prefix(new_raw)
            current = FileChange(
                old_path=old_path,
                new_path=new_path,
                is_new=old_raw == _DEV_NULL,
                is_deleted=new_raw == _DEV_NULL,
            )
            changes.append(current)
            current_hunk = None
            index += 2
            continue

        match = _HUNK_HEADER.match(line)
        if match:
            if current is None:
                raise DiffApplyError("hunk header before any file header")
            old_start = int(match.group(1))
            body: list[str] = []
            current_hunk = (old_start, 0, body)
            current.hunks.append(current_hunk)
            index += 1
            continue

        if current_hunk is not None:
            body = current_hunk[2]
            if line.startswith("+") or line.startswith("-") or line.startswith(" "):
                body.append(line)
            elif line.startswith("\\"):
                # "\ No newline at end of file" carries no content.
                pass
            elif line == "":
                # A trailing empty line is context for a final blank line.
                body.append(" ")
            else:
                raise DiffApplyError(f"unexpected line inside a hunk: {line!r}")
        index += 1

    return changes


def apply_to_file(original: str, change: FileChange) -> str:
    """Apply one file's hunks to its original content.

    Strict: the context and removed lines must match exactly, at the offset the
    hunk header states. A mismatch raises rather than writing something wrong.
    """
    if change.is_deleted:
        return ""
    if change.is_new:
        original = ""

    source = original.splitlines()
    # A file with no trailing newline still splits predictably; track that.
    had_trailing_newline = original.endswith("\n") or original == ""
    result: list[str] = []
    cursor = 0

    for old_start, _unused, body in change.hunks:
        # Hunk line numbers are 1-based. A new file is expressed as "-0,0",
        # which means "the old file is empty, insert at the start", so the
        # target is 0 rather than -1.
        target = max(old_start - 1, 0)
        if target < cursor:
            raise DiffApplyError(f"hunks for {change.path!r} are out of order or overlap")
        if target > len(source):
            raise DiffApplyError(f"hunk for {change.path!r} starts past the end of the file")
        result.extend(source[cursor:target])
        cursor = target

        for raw in body:
            marker, text = raw[0], raw[1:]
            if marker in (" ", "-"):
                found = source[cursor] if cursor < len(source) else None
                if found != text:
                    kind = "context" if marker == " " else "removal"
                    where = (
                        f"at line {cursor + 1}" if found is not None else "but the file ended early"
                    )
                    raise DiffApplyError(
                        f"{kind} mismatch in {change.path!r} {where}: "
                        f"expected {text!r}, found {found!r}"
                    )
                if marker == " ":
                    result.append(text)
                cursor += 1
            elif marker == "+":
                result.append(text)

    result.extend(source[cursor:])
    joined = "\n".join(result)
    if had_trailing_newline and joined:
        joined += "\n"
    return joined


def apply_diff(diff: str, base_files: dict[str, str]) -> dict[str, str]:
    """Apply a diff to a mapping of path to content.

    ``base_files`` holds the current content of every file the diff touches.
    Returns the full new content for every touched file. Raises if a file the
    diff modifies is absent from ``base_files``.
    """
    changes = parse_unified_diff(diff)
    if not changes:
        raise DiffApplyError("the diff contains no file changes")

    result: dict[str, str] = {}
    for change in changes:
        source_path = change.old_path if not change.is_new else change.new_path
        if change.is_new:
            original = ""
        else:
            if source_path not in base_files:
                raise DiffApplyError(
                    f"the diff modifies {source_path!r}, which is not present in the "
                    "base tree; refusing to guess its contents"
                )
            original = base_files[source_path]
        result[change.path] = apply_to_file(original, change)
    return result
