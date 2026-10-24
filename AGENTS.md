# AGENTS.md

Permanent rules for this repository. Follow these in every future step.

1. Work in small steps. Before each step, list the files you will create or modify.
2. After each step, run the relevant tests and ruff, and report the REAL output. Never claim success without running commands.
3. Keep the project runnable after every step.
4. Never fabricate APIs, test results, benchmarks, PRs, or feedback. Verify third-party features in official docs.
5. Never write secrets into files. Never perform external write actions (PRs, comments, pushes to other repos).
6. Commit to a branch with a clear message at the end of each step.
7. If something is ambiguous, ask me only if it blocks progress. Otherwise state your assumption.

## Phase 2 state (as of 2026-10-22)

Phase 2 is complete on branch `feat/phase2-context-and-analysis` (not merged, no
PR opened). Modules added: `repository_context.py`, `prompts.py`,
`agent_runner.py`, `issue_analyzer.py`, `sandbox.py`, `patch.py`, `pipeline.py`,
`review.py`. Docs: `docs/agent-evaluation.md`, `docs/phase2-notes.md`.

Environment facts that constrain future work. Do not assume otherwise without
re-measuring:

- **No container runtime.** `/usr/bin/docker` exists but the daemon socket
  `/var/run/docker.sock` does not, and `podman` is absent. `sandbox.py` therefore
  refuses to execute repository code. Never add a host-execution fallback.
- **No `JULES_API_KEY` and no `GEMINI_API_KEY`.** Both providers fail closed.
  `JULES_API_KEY` cannot be created non-interactively (Jules web app only).
- **Phase 2 performs no external writes.** `automationMode` is never sent to
  Jules; a test asserts this. Keep it that way.
- **Prices in `config/repositories.yaml` are 0.0 on purpose.** Fill them from
  vendor pricing before trusting a cost report.

Conventions that must be preserved:

- Every free-text field from a repository is an `UntrustedText` and is rendered
  only inside its data fence. Instructions and data travel in separate arguments.
- Analysis output is validated Pydantic with `extra="forbid"`.
- `check_budget()` runs before every paid model call; no budget query means no
  calls.
- Patch approval binds to the SHA-256 of the exact diff bytes.
- Gates after every step: `uv run python -m ruff check .`,
  `uv run python -m ruff format --check .`, `uv run python -m pytest -q`.

## Phase 2 completion pass (2026-10-22)

Four Phase 2 gaps were closed after the first pass, all in
`tests/test_phase2_completion.py`:

1. **Kill switch** (SPEC 9): `kill_switch: true` in the YAML or
   `FORGEFLOW_KILL_SWITCH=1` in the environment. Checked before the budget check
   and before the isolation check. Note: pydantic-settings maps the field to
   `KILL_SWITCH` by default, so `validation_alias` is required for the
   namespaced name to work. Do not remove it.
2. **Analyses are persisted** in the `analyses` table as JSON. `run_review`
   loads the latest when none is passed, so a standalone `review` is complete.
   A stale payload returns None rather than raising.
3. **Analyze idempotence**: re-analysing a task at or past
   `awaiting_plan_approval` refuses unless `force=True`, and the refusal happens
   before any model call.
4. **Workspace lifecycle**: `list_workspaces`, `remove_workspace`,
   `prune_workspaces`, and `forgeflow workspace list|prune|remove`. Removal
   rebuilds the path internally, so a crafted repo name cannot escape the root.

Current gate state: 508 tests pass, ruff check and format clean.


## Phase 4 step 4.3: issue verification fix and outcome tracking (2026-10-24)

Two changes on branch `feat/phase4-controlled-publishing`.

1. **Issue verification uses the read-only token.** The real-mode publish body
   reported issue #1 as unconfirmed because `publisher._issue_verified` read the
   issue through the *publishing* client. The publishing token is narrow and the
   issues API read returned 401, so a real issue looked missing. Fix: reads use
   the read-only client (`GITHUB_TOKEN`), writes use the write client
   (`FORGEFLOW_PUBLISH_TOKEN`); the two never cross.
   - `plan_publish`/`publish` take `read_client: GitHubReadClient | None`; when
     omitted it falls back to `client`, so single-scope callers are unchanged.
   - The CLI builds both clients in real mode and closes both.
   - Regression tests: `tests/test_publisher.py`
     (`test_real_publish_verifies_the_issue_with_the_read_client`, and the
     fallback / read-denies cases).

2. **`outcome_tracker.py`** (SPEC sections 2J, 11, 15). Read-only tracking of a
   published PR: status (open/merged/closed), reviews, requested changes,
   failures, and cost, in SQLite. It uses only `GITHUB_TOKEN`; it never imports
   or reads `FORGEFLOW_PUBLISH_TOKEN` and reads no environment itself. Tables
   `pull_request_outcomes`, `pr_reviews`, `outcome_metrics` are created by
   `storage.init_db`. CLI: `forgeflow track <ref>` and `forgeflow metrics`.
   - `time_saved_hours` is reported as `0.0` on purpose: it is not measured, and
     a constant estimate would be a fabricated benchmark.
   - `metrics_report()` always returns every key in `SPEC_METRICS`.

Current gate state: 863 tests pass, ruff check and format clean.

## Phase 4 ranking adjustment and scheduler (2026-10-24)

STEP 4.3 gap closed and STEP 4.4 added.

- **Outcome ranking adjustment.** `OutcomeRankingConfig` in `config.py`, wired
  through `ranking.outcome_adjustment()` and `ranking.adjusted_total()`. It is
  **off by default** (`enabled: false`), capped at `max_adjustment_points`
  (default 3.0 on the 0-100 scale), and rewards maintainer engagement, not merge
  count: merges weigh most, approvals less, changes-requested still positive,
  closed-unmerged negative. `outcome_tracker.outcome_stats_by_repo()` builds the
  per-repository stats from the DB; `cli.run_rank`/`run_report` pass them only
  when the config enables the adjustment. `RankedIssue.outcome_adjustment` is a
  separate field; the base six-factor score is untouched.
- **Scheduler (SPEC 13).** `scheduler.py`, `ScheduleConfig` (off by default).
  Discovery **only**: `ALLOWED_SCHEDULED_COMMANDS == {"discover"}`, and a test
  asserts the module imports neither the publisher nor the token module. No
  daemon or loop; `forgeflow schedule [--run]` performs at most one cycle when
  due. Kill switch is checked before any client is built. Publishing is never
  scheduled (SPEC 8).
- **Tests added.** `tests/test_ranking.py` (outcome adjustment),
  `tests/test_outcome_tracker.py` (stats aggregation + lookback),
  `tests/test_scheduler.py` (17 tests). `test_publish_isolation.py` now also
  covers `outcome_tracker` and `scheduler`.
- **Docs.** README gained "Publishing" and "Scheduling (discovery only, off by
  default)" sections plus an outcome-adjustment note. `docs/threat-model.md`
  gained threat 9 (gaming the outcome metric).

Current gate state: 898 tests pass, ruff check and format clean.
