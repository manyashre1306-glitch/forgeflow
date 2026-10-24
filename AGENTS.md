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

## Phase 5 state (2026-10-24)

Phase 5 is complete on branch `feat/phase5-docs-and-evaluation` (not merged, no
PR opened). Deliverables: architecture diagrams (`docs/architecture.md`), the
threat model (`docs/threat-model.md`), the evaluation harness and results
(`evaluation/`, `docs/evaluation.md`), cost controls (`docs/cost-controls.md`),
reproducible setup (`docs/setup.md`), the offline dry-run demo (`demo/`,
`docs/demonstration.md`), read-only CI (`.github/workflows/ci.yml`), a Makefile,
the portfolio README, and the SPEC audit (`docs/spec-audit.md`).

Environment facts confirmed this phase (re-measured, do not assume otherwise):

- **No container runtime.** `sandbox.detect_backend()` returns
  `IsolationKind.NONE`. The sandbox refuses; there is no host fallback.
- **No `GEMINI_API_KEY` and no `JULES_API_KEY`.** Both providers fail closed.
- **One real publish only.** `forgeflow-sandbox#2` (open, draft, not merged,
  0 reviews, 0 comments as of 2026-10-24). No upstream contributions.
- **Phase 5 adds no external write and no new credential.**

Facts future work must not undo:

- **`demo/` is offline and dry-run only.** It uses `MockAgent` and in-process
  fake GitHub clients that count write requests; `tests/test_demo.py` asserts
  zero writes. Keep it that way.
- **Evaluation is fixture arithmetic, not a benchmark.** SWE-bench is not run;
  `time_saved_hours` reports "not measured"; empty denominators report "not
  enough data". Do not add headline rates over the single synthetic publish.
- **The README Contributions section is checked against the GitHub API, not
  recalled.** Only list PRs that actually exist, with their true status.
- **`docs/spec-audit.md` marks PARTIAL for anything this environment cannot
  exercise** (real model calls, real validation, automated integration test,
  SWE-bench). Do not upgrade a PARTIAL to DONE without new evidence.

Conventions preserved: every repository free-text field is an `UntrustedText`
rendered only inside its data fence; analysis output is `extra="forbid"`;
`check_budget()` runs before every paid call; approval binds to the SHA-256 of
the exact diff bytes; gates after every step are `uv run python -m ruff check .`,
`uv run python -m ruff format --check .`, `uv run python -m pytest -q`.

Current gate state: 923 tests pass, ruff check and format clean.

## Phase 2 AI-policy detection and enforcement (2026-10-24)

Branch `feat/fix-models-and-policy` (not merged). Three fixes:

1. **Reachable models and correct pricing** (`e8d7238`). `triage_model` is
   `gemini-3.5-flash-lite`, `analysis_model` is `gemini-3.6-flash`. The previous
   `gemini-2.5-flash` / `gemini-2.5-pro` return HTTP 404 ("no longer available to
   new users"). Prices in `config/repositories.yaml` are real, not 0.0.
2. **Model-API timeouts map to a clean error** (`893cb82`). A timeout is recorded
   in `llm_usage` with `success = 0` and a readable message, not a raw traceback.
3. **AI-policy detection gap closed.** `detect_contribution_rules` returned
   `ai_policy=unknown` for scikit-learn even though its contributing guide bans
   AI-generated PRs. The cause was the pattern list not matching the real wording
   and only matching per line. Fixes:
   - `repository_context.py`: new prohibition and disclosure patterns; a
     whitespace-normalised second pass so a rule split across a hard wrap still
     matches. `AI_DISCLOSURE_PATTERNS` is separate, so a disclosure duty is not
     mislabelled as a ban.
   - `ContributionRules.requires_ai_disclosure` and `.is_ai_restricted`
     (`models.py`). A disclosure requirement counts as restricted.
   - `run_context` persists the rules (`contribution_rules` table);
     `run_review` loads them when no context object is supplied, so a standalone
     review still warns.
   - `review.py` prints a bold WARNING when the policy restricts AI use.
   - `publisher.plan_publish` gained an "ai policy" check. A detected restriction
     **refuses** the publish unless a documented override bound to the exact patch
     hash exists (`ai_policy_overrides` table). The override unlocks only this
     check, never the patch-integrity guards.
   - `forgeflow ai-policy-override <ref> --reason ...` records the override and
     writes a `ai_policy.override_recorded` audit event. `--reason` is required.

Environment facts unchanged: no container runtime, no `JULES_API_KEY`. Phase 2
still performs no external writes.

Gates: 959 tests pass, ruff check and format clean. New test files:
`tests/test_ai_policy_override_cli.py`; additions to
`tests/test_repository_context.py`, `tests/test_pipeline_phase2.py`,
`tests/test_publisher.py`.
