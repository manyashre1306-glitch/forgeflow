"""End-to-end Phase 1 pipeline test using the injected fake client.

Exercises the full path: discover -> deduplicate -> filter -> store -> rank ->
report -> shortlist. No network, no real token, real SQLite on a temp path.
"""

from __future__ import annotations

from forgeflow import storage
from forgeflow.cli import run_discover, run_rank, run_report, run_shortlist
from forgeflow.config import AppConfig
from forgeflow.models import DEFAULT_WEIGHTS, TaskState

REPO = "pydantic/pydantic"
REPO2 = "psf/requests"


def issue_payload(number: int, **overrides) -> dict:
    data = {
        "number": number,
        "title": f"Bug: python typing fails {number}",
        "body": "Steps to reproduce:\nExpected x\nActual y\nTraceback: Error\n" + "x" * 250,
        "state": "open",
        "labels": [{"name": "bug"}],
        "html_url": f"https://github.com/{REPO}/issues/{number}",
        "comments": 1,
        "assignees": [],
        "created_at": "2026-10-01T08:00:00Z",
        "updated_at": "2026-10-19T09:30:00Z",
    }
    data.update(overrides)
    return data


def build_config() -> AppConfig:
    return AppConfig(
        repositories=[REPO, REPO2],
        weights=dict(DEFAULT_WEIGHTS),
        skill_keywords={"python": ["python", "typing"]},
        exclude_labels=["duplicate", "wontfix", "invalid"],
    )


def build_client(fake_client):
    fake_client.repositories[REPO] = {
        "full_name": REPO,
        "stargazers_count": 21000,
        "open_issues_count": 300,
        "pushed_at": "2026-10-20T12:00:00Z",
    }
    fake_client.repositories[REPO2] = {
        "full_name": REPO2,
        "stargazers_count": 52000,
        "open_issues_count": 200,
        "pushed_at": "2026-10-21T12:00:00Z",
    }
    fake_client.files[REPO] = {"CONTRIBUTING.md", "tests"}
    fake_client.files[REPO2] = {"CONTRIBUTING.md"}
    fake_client.issues[REPO] = [
        issue_payload(101),  # eligible
        issue_payload(102, labels=[{"name": "wontfix"}]),  # excluded label
        issue_payload(103, state="closed"),  # excluded closed
        issue_payload(104, assignees=[{"login": "dev"}]),  # excluded assigned
        issue_payload(101),  # duplicate of 101
    ]
    fake_client.issues[REPO2] = [
        issue_payload(
            201,
            title="Bug: python typing problem 201",
            html_url=f"https://github.com/{REPO2}/issues/201",
        ),
    ]
    return fake_client


def test_full_pipeline_end_to_end(fake_client, db_path, tmp_path) -> None:
    client = build_client(fake_client)
    config = build_config()

    # 1. discover + dedupe + filter + store
    run_discover(client, config, db_path)

    conn = storage.connect(db_path)
    try:
        stored = storage.count_issues(conn)
        excluded = storage.count_excluded_issues(conn)
    finally:
        conn.close()

    assert stored == 2, "one eligible issue per repository"
    assert excluded == 4, "duplicate, wontfix, closed, and assigned are all recorded"

    # 2. rank
    ranked = run_rank(db_path, config)
    assert len(ranked) == 2
    assert [entry.rank for entry in ranked] == [1, 2]
    for entry in ranked:
        assert entry.score.reasons
        for value in entry.score.factor_values().values():
            assert 0.0 <= value <= 1.0

    # 3. report
    target = tmp_path / "pipeline-report.md"
    written = run_report(db_path, config, output=target)
    text = written.read_text(encoding="utf-8")
    assert "ForgeFlow Phase 1 Report" in text
    assert f"{REPO}#101" in text
    assert f"{REPO2}#201" in text
    assert "heuristics" in text and "not proven predictors" in text
    for reason in ("closed", "assigned", "excluded_label", "duplicate"):
        assert reason in text, f"report must record the '{reason}' exclusion"

    # 4. shortlist
    updated = run_shortlist(db_path, f"{REPO}#101")
    assert updated.task_state is TaskState.SHORTLISTED

    conn = storage.connect(db_path)
    try:
        history = storage.list_status_changes(conn, REPO, 101)
    finally:
        conn.close()
    assert history[-1]["to_state"] == "shortlisted"


def test_pipeline_is_idempotent_end_to_end(fake_client, db_path, tmp_path) -> None:
    client = build_client(fake_client)
    config = build_config()

    run_discover(client, config, db_path)
    run_rank(db_path, config)
    run_report(db_path, config, output=tmp_path / "first.md")

    conn = storage.connect(db_path)
    try:
        rows_after_first = storage.count_issues(conn)
        excluded_after_first = storage.count_excluded_issues(conn)
        scores_after_first = len(storage.list_issue_scores(conn))
    finally:
        conn.close()

    # Run the whole discover-and-store path a second time.
    run_discover(client, config, db_path)
    run_rank(db_path, config)
    run_report(db_path, config, output=tmp_path / "second.md")

    conn = storage.connect(db_path)
    try:
        assert storage.count_issues(conn) == rows_after_first
        assert storage.count_excluded_issues(conn) == excluded_after_first
        assert len(storage.list_issue_scores(conn)) == scores_after_first
    finally:
        conn.close()


def test_pipeline_database_is_created_under_data_dir(fake_client, tmp_path) -> None:
    """Acceptance criterion 8: SQLite works with no server; the file lands in data/."""
    data_dir = tmp_path / "data"
    db_path = data_dir / "forgeflow.db"
    client = build_client(fake_client)
    run_discover(client, build_config(), db_path)
    assert db_path.is_file()
    assert db_path.parent == data_dir
