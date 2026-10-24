"""Tests for validation persistence (SPEC sections 7 and 10).

Covers the validation_results and validation_exceptions tables, the redaction
of stored output, and the two enforcement queries the approval gate relies on.
"""

from __future__ import annotations

import pytest

from forgeflow import storage
from forgeflow.models import (
    CheckKind,
    CheckStatus,
    ValidationCheck,
    ValidationReport,
)
from forgeflow.patch import compute_patch_sha256
from forgeflow.security import REDACTED

REPO = "acme/widget"
HASH_A = compute_patch_sha256("a")
HASH_B = compute_patch_sha256("b")


@pytest.fixture
def conn(tmp_path):
    connection = storage.connect(tmp_path / "t.db")
    storage.init_db(connection)
    from forgeflow.models import Issue, Repository

    storage.upsert_repository(connection, Repository(full_name=REPO, stars=1))
    storage.upsert_issue(connection, Issue(repo_full_name=REPO, number=7, title="t", body="b"))
    yield connection
    connection.close()


def report(
    *,
    statuses: list[CheckStatus] | None = None,
    patch_sha256: str = HASH_A,
    output: str = "ok",
    isolated: bool = True,
) -> ValidationReport:
    checks = []
    for index, status in enumerate(statuses or [CheckStatus.PASSED]):
        checks.append(
            ValidationCheck(
                kind=CheckKind.TESTS,
                name=f"check{index}",
                command=["python", "-m", "pytest"],
                status=status,
                exit_code=0 if status is CheckStatus.PASSED else 1,
                output=output,
                duration_seconds=0.5,
                skip_reason="no isolation" if status is CheckStatus.SKIPPED else None,
            )
        )
    return ValidationReport(
        repo_full_name=REPO,
        issue_number=7,
        patch_sha256=patch_sha256,
        isolated=isolated,
        checks=checks,
    )


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def test_report_is_persisted(conn) -> None:
    rows = storage.record_validation_report(conn, report())
    assert rows == 1
    assert len(storage.list_validation_results(conn, REPO, 7)) == 1


def test_one_row_per_check(conn) -> None:
    storage.record_validation_report(
        conn, report(statuses=[CheckStatus.PASSED, CheckStatus.FAILED, CheckStatus.SKIPPED])
    )
    assert len(storage.list_validation_results(conn, REPO, 7)) == 3


def test_command_is_stored_as_json(conn) -> None:
    storage.record_validation_report(conn, report())
    row = storage.list_validation_results(conn, REPO, 7)[0]
    assert row["command_json"] == '["python", "-m", "pytest"]'


def test_exit_status_is_stored(conn) -> None:
    storage.record_validation_report(conn, report(statuses=[CheckStatus.FAILED]))
    row = storage.list_validation_results(conn, REPO, 7)[0]
    assert row["status"] == "failed"
    assert row["exit_code"] == 1


def test_skipped_checks_store_the_reason(conn) -> None:
    storage.record_validation_report(conn, report(statuses=[CheckStatus.SKIPPED]))
    row = storage.list_validation_results(conn, REPO, 7)[0]
    assert row["status"] == "skipped"
    assert row["skip_reason"] == "no isolation"


def test_patch_hash_is_stored(conn) -> None:
    storage.record_validation_report(conn, report())
    assert storage.list_validation_results(conn, REPO, 7)[0]["patch_sha256"] == HASH_A


def test_isolated_flag_is_stored(conn) -> None:
    storage.record_validation_report(conn, report(isolated=False))
    assert storage.list_validation_results(conn, REPO, 7)[0]["isolated"] == 0


def test_results_can_be_filtered_by_patch(conn) -> None:
    storage.record_validation_report(conn, report(patch_sha256=HASH_A))
    storage.record_validation_report(conn, report(patch_sha256=HASH_B))
    only_a = storage.list_validation_results(conn, REPO, 7, patch_sha256=HASH_A)
    assert len(only_a) == 1
    assert only_a[0]["patch_sha256"] == HASH_A


def test_repeated_runs_append_rather_than_overwrite(conn) -> None:
    storage.record_validation_report(conn, report())
    storage.record_validation_report(conn, report())
    assert len(storage.list_validation_results(conn, REPO, 7)) == 2


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


def test_stored_output_is_redacted(conn) -> None:
    """A failing test can print a credential; the table must not hold it."""
    storage.record_validation_report(
        conn, report(output="error: ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    )
    row = storage.list_validation_results(conn, REPO, 7)[0]
    assert "ghp_" not in row["output"]
    assert REDACTED in row["output"]


def test_stored_output_keeps_the_hash(conn) -> None:
    """Redaction must not destroy the patch hash that is evidence."""
    storage.record_validation_report(conn, report(output=f"patch {HASH_A} verified"))
    row = storage.list_validation_results(conn, REPO, 7)[0]
    assert HASH_A in row["output"]


# --------------------------------------------------------------------------
# The enforcement queries
# --------------------------------------------------------------------------


def test_passed_query_true_only_for_a_passed_row(conn) -> None:
    storage.record_validation_report(conn, report(statuses=[CheckStatus.PASSED]))
    assert storage.validation_passed_for_patch(conn, REPO, 7, HASH_A) is True


def test_passed_query_false_when_all_skipped(conn) -> None:
    """The critical case: nothing executed must never read as a pass."""
    storage.record_validation_report(
        conn, report(statuses=[CheckStatus.SKIPPED, CheckStatus.SKIPPED])
    )
    assert storage.validation_passed_for_patch(conn, REPO, 7, HASH_A) is False


def test_passed_query_false_when_no_rows(conn) -> None:
    assert storage.validation_passed_for_patch(conn, REPO, 7, HASH_A) is False


def test_passed_query_false_for_a_different_patch(conn) -> None:
    """Evidence for one patch must not authorize another."""
    storage.record_validation_report(conn, report(patch_sha256=HASH_A))
    assert storage.validation_passed_for_patch(conn, REPO, 7, HASH_B) is False


def test_passed_query_false_when_only_failures(conn) -> None:
    storage.record_validation_report(conn, report(statuses=[CheckStatus.FAILED]))
    assert storage.validation_passed_for_patch(conn, REPO, 7, HASH_A) is False


def test_failed_query_true_for_failed_row(conn) -> None:
    storage.record_validation_report(conn, report(statuses=[CheckStatus.FAILED]))
    assert storage.validation_failed_for_patch(conn, REPO, 7, HASH_A) is True


def test_failed_query_true_for_errored_row(conn) -> None:
    storage.record_validation_report(conn, report(statuses=[CheckStatus.ERRORED]))
    assert storage.validation_failed_for_patch(conn, REPO, 7, HASH_A) is True


def test_failed_query_false_for_skipped_only(conn) -> None:
    storage.record_validation_report(conn, report(statuses=[CheckStatus.SKIPPED]))
    assert storage.validation_failed_for_patch(conn, REPO, 7, HASH_A) is False


def test_mixed_report_still_has_a_passed_row(conn) -> None:
    """One passing check is evidence, even alongside a skip."""
    storage.record_validation_report(
        conn, report(statuses=[CheckStatus.PASSED, CheckStatus.SKIPPED])
    )
    assert storage.validation_passed_for_patch(conn, REPO, 7, HASH_A) is True


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------


def test_exception_is_recorded(conn) -> None:
    row_id = storage.record_validation_exception(
        conn,
        REPO,
        7,
        patch_sha256=HASH_A,
        authorized_by="manyashre",
        reason="flaky upstream test, tracked in #12",
    )
    assert row_id > 0


def test_exception_is_read_back(conn) -> None:
    storage.record_validation_exception(
        conn, REPO, 7, patch_sha256=HASH_A, authorized_by="manyashre", reason="flaky"
    )
    found = storage.validation_exception_for_patch(conn, REPO, 7, HASH_A)
    assert found is not None
    assert found["authorized_by"] == "manyashre"
    assert found["reason"] == "flaky"


def test_exception_is_bound_to_the_patch(conn) -> None:
    """An exception for one patch must not cover a different patch."""
    storage.record_validation_exception(
        conn, REPO, 7, patch_sha256=HASH_A, authorized_by="x", reason="y"
    )
    assert storage.validation_exception_for_patch(conn, REPO, 7, HASH_B) is None


def test_no_exception_returns_none(conn) -> None:
    assert storage.validation_exception_for_patch(conn, REPO, 7, HASH_A) is None


def test_latest_exception_wins(conn) -> None:
    storage.record_validation_exception(
        conn, REPO, 7, patch_sha256=HASH_A, authorized_by="first", reason="one"
    )
    storage.record_validation_exception(
        conn, REPO, 7, patch_sha256=HASH_A, authorized_by="second", reason="two"
    )
    found = storage.validation_exception_for_patch(conn, REPO, 7, HASH_A)
    assert found is not None
    assert found["authorized_by"] == "second"


def test_exception_does_not_imply_validation_passed(conn) -> None:
    """An exception is a separate record; it must not fake a passing check."""
    storage.record_validation_exception(
        conn, REPO, 7, patch_sha256=HASH_A, authorized_by="x", reason="y"
    )
    assert storage.validation_passed_for_patch(conn, REPO, 7, HASH_A) is False
