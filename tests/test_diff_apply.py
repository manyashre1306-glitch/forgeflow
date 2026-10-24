"""Tests for strict diff application (SPEC section 8).

Applying a diff is how a reviewed patch becomes real file content on a branch.
A silent misapplication would publish code nobody reviewed, so every failure
mode here is required to raise rather than guess.
"""

from __future__ import annotations

import pytest

from forgeflow.diff_apply import (
    DiffApplyError,
    apply_diff,
    apply_to_file,
    parse_unified_diff,
)

BASE = "def f():\n    return 1\n\ndef g():\n    return 2\n"


def modify_diff() -> str:
    return (
        "diff --git a/m.py b/m.py\n"
        "--- a/m.py\n"
        "+++ b/m.py\n"
        "@@ -1,5 +1,6 @@\n"
        " def f():\n"
        "-    return 1\n"
        "+    return 10\n"
        " \n"
        " def g():\n"
        "     return 2\n"
        "+# trailing comment\n"
    )


def test_modifies_an_existing_file() -> None:
    result = apply_diff(modify_diff(), {"m.py": BASE})
    assert result["m.py"] == (
        "def f():\n    return 10\n\ndef g():\n    return 2\n# trailing comment\n"
    )


def test_creates_a_new_file() -> None:
    diff = (
        "diff --git a/n.py b/n.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/n.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+x = 1\n"
        "+y = 2\n"
    )
    assert apply_diff(diff, {})["n.py"] == "x = 1\ny = 2\n"


def test_deletes_a_file() -> None:
    diff = "--- a/d.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-gone\n-gone2\n"
    assert apply_diff(diff, {"d.py": "gone\ngone2\n"})["d.py"] == ""


def test_multiple_files_in_one_diff() -> None:
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n"
        " a = 1\n"
        "+a2 = 2\n"
        "diff --git a/b.py b/b.py\n"
        "--- a/b.py\n+++ b/b.py\n@@ -1 +1,2 @@\n"
        " b = 1\n"
        "+b2 = 2\n"
    )
    result = apply_diff(diff, {"a.py": "a = 1\n", "b.py": "b = 1\n"})
    assert result == {"a.py": "a = 1\na2 = 2\n", "b.py": "b = 1\nb2 = 2\n"}


def test_context_mismatch_raises() -> None:
    with pytest.raises(DiffApplyError, match="context mismatch"):
        apply_diff(modify_diff(), {"m.py": "def other():\n    return 1\n"})


def test_removal_mismatch_raises() -> None:
    """A line the diff removes must be present, or the patch is stale."""
    diff = "--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-return 1\n+return 10\n"
    with pytest.raises(DiffApplyError, match="removal mismatch"):
        apply_diff(diff, {"m.py": "return 2\n"})


def test_missing_base_file_raises() -> None:
    with pytest.raises(DiffApplyError, match="not present in the base tree"):
        apply_diff(modify_diff(), {})


def test_file_ending_early_raises() -> None:
    diff = "--- a/m.py\n+++ b/m.py\n@@ -1,3 +1,3 @@\n a\n b\n c\n"
    with pytest.raises(DiffApplyError):
        apply_diff(diff, {"m.py": "a\n"})


def test_empty_diff_raises() -> None:
    with pytest.raises(DiffApplyError, match="no file changes"):
        apply_diff("", {})


def test_malformed_diff_raises() -> None:
    with pytest.raises(DiffApplyError):
        parse_unified_diff("--- a/x.py\n@@ -1 +1 @@\n-a\n+b\n")


def test_hunk_before_file_header_raises() -> None:
    with pytest.raises(DiffApplyError, match="hunk header before any file header"):
        parse_unified_diff("@@ -1 +1 @@\n-a\n+b\n")


def test_no_newline_marker_is_ignored() -> None:
    diff = "--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-old\n+new\n\\ No newline at end of file\n"
    assert apply_to_file("old", parse_unified_diff(diff)[0]) == "new"


def test_trailing_newline_is_preserved() -> None:
    diff = "--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert apply_to_file("a\n", parse_unified_diff(diff)[0]) == "b\n"


def test_no_trailing_newline_is_preserved() -> None:
    diff = "--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert apply_to_file("a", parse_unified_diff(diff)[0]) == "b"


def test_two_hunks_apply_in_order() -> None:
    base = "\n".join(f"line{i}" for i in range(1, 11)) + "\n"
    diff = (
        "--- a/m.py\n+++ b/m.py\n"
        "@@ -1,3 +1,3 @@\n line1\n-line2\n+LINE2\n line3\n"
        "@@ -8,3 +8,3 @@\n line8\n-line9\n+LINE9\n line10\n"
    )
    result = apply_to_file(base, parse_unified_diff(diff)[0])
    assert "LINE2" in result
    assert "LINE9" in result
    assert "line2" not in result


def test_overlapping_hunks_raise() -> None:
    diff = (
        "--- a/m.py\n+++ b/m.py\n@@ -1,3 +1,3 @@\n a\n-b\n+c\n d\n@@ -1,3 +1,3 @@\n a\n-b\n+c\n d\n"
    )
    with pytest.raises(DiffApplyError, match="out of order or overlap"):
        apply_to_file("a\nb\nd\n", parse_unified_diff(diff)[0])
