# SPEC audit

A requirement-by-requirement audit of `docs/SPEC.md`, sections 1 through 16.
Each row states **DONE**, **PARTIAL**, or **MISSING**, with the evidence that
supports it: a source file, a test name, or a command whose output is quoted.

The rule this document follows: a requirement is **DONE** only when there is a
file, a passing test, or a reproducible command behind it. A requirement whose
capability is blocked by this environment (no container runtime, no model keys)
is marked **PARTIAL** even when the code for it is complete, because it has not
been exercised end to end here. Nothing in this file is inferred.

Environment facts this audit assumes (measured, not assumed):

- Python 3.13 via `uv`; the test suite is offline.
- No container runtime: `/usr/bin/docker` exists but `/var/run/docker.sock` does
  not, and `podman` is absent. `sandbox.detect_backend()` returns
  `IsolationKind.NONE`.
- No `GEMINI_API_KEY` and no `JULES_API_KEY` are present.

Gate state at the time of writing: `ruff check` clean, `ruff format --check`
clean, `923 passed` (see section 14 below).

---

## Section 1 — Project vision

> A modular Python application that automates repetitive contribution work while
> keeping the human in control of code review and publication.

**Status: DONE.**

- Modular package under `src/forgeflow/` with 28 focused modules plus `cli.py`;
  `docs/architecture.md` lists each module's single responsibility.
- The human stays in control: publishing requires a recorded approval bound to
  the patch hash (`approval.py`, `publisher.py`), and the README states it.
- Evidence: `docs/architecture.md` (module table), `tests/test_approval.py`.

## Section 2 — Core workflow (A–J)

| Step | Requirement | Status | Evidence |
| --- | --- | --- | --- |
| A | Discover public issues from a configurable allowlist | DONE | `discovery.py`, `config/repositories.yaml` `repositories:`; `tests/test_discovery.py` |
| B | Filter duplicates, closed issues, competing fixes, contribution-rule conflicts | DONE | `dedupe.py` (`LINKED_OPEN_PR`, `opt_out_labels`), `tests/test_dedupe.py`; `opt_out_labels` in config |
| C | Rank by relevance, clarity, feasibility, testability, health, conflict risk | DONE | `ranking.py`, six weights in config; `tests/test_ranking.py` |
| D | Build a context bundle (issue, instructions, files, tests, guidelines) | DONE | `repository_context.py`, `prompts.py`; `tests/test_repository_context.py` |
| E | Ask a model to explain, propose a minimal fix, list risks, plan tests | DONE (code) / PARTIAL (no key here) | `issue_analyzer.py` produces a validated `AnalysisResult`; `tests/test_issue_analyzer.py` uses `FakeBackend`. A real model call is not possible here — `analyze` fails closed without `GEMINI_API_KEY`. |
| F | After plan approval, invoke a coding agent in an isolated workspace | PARTIAL | `pipeline.run_implement` + `agent_runner.py` (`CodingAgent`, `JulesAgent`, `MockAgent`). The workspace is isolated only with a container runtime, which is absent; a real provider call needs `JULES_API_KEY`, also absent. Exercised with `MockAgent` in `tests/test_pipeline_phase2.py`. |
| G | Run tests, lint, type checks, diff analysis, security checks | PARTIAL | `validation.py` implements all of it; with no container runtime every check is recorded `skipped`, never `passed`. `tests/test_validation.py::test_no_isolation_never_reports_passed`. |
| H | Produce a review bundle (URL, fix, diff, commands, real results, limits, PR text) | DONE | `review.py`, `report.py`; `tests/test_report.py`, `tests/test_pipeline_phase2.py` |
| I | Wait for explicit approval; verify patch hash, repo, branch, scope; fresh approval if the patch changes | DONE | `approval.py`, `publisher.plan_publish` (8 ordered checks); `tests/test_approval.py`, `tests/test_approval_gate.py`, `tests/test_publisher.py` |
| J | Track PR feedback, changes requested, merge status, failures, cost | DONE | `outcome_tracker.py` (`track_pull_request`, `record_reviews`, `cost_for_task`); `tests/test_outcome_tracker.py` |

## Section 3 — Technology stack

| Required | Status | Evidence |
| --- | --- | --- |
| Python 3.12+ | DONE | `pyproject.toml` `requires-python = ">=3.13"` |
| Git | DONE | `repository_context.py` shallow clone |
| GitHub REST API | DONE | `github_client.py` uses `httpx` against the REST API |
| PyGithub *or* direct HTTP | DONE | Direct HTTP via `httpx` (the SPEC allows either) |
| Pydantic for validated models | DONE | `models.py`, `issue_analyzer.py` (`extra="forbid"`) |
| SQLite for state | DONE | `storage.py`; `tests/test_storage.py` |
| pytest for tests | DONE | `tests/`, 923 passing |
| Ruff for linting | DONE | `pyproject.toml` `[tool.ruff]`; CI runs it |
| Docker or equivalent isolated environment | PARTIAL | `sandbox.py` implements Docker/Podman isolation **and an explicit refusal**; no runtime is present here, so it has not executed. `tests/test_sandbox.py`. |
| Capable LLM + cheaper triage model behind an adapter | DONE (code) / PARTIAL (no key) | `config/repositories.yaml` sets `triage_model` and `analysis_model`; `issue_analyzer.py` is the adapter. Not called here (no key). |

**Gap fixed in this audit:** `PyGithub` was declared in `pyproject.toml`
dependencies but the code uses direct HTTP (`httpx`). The SPEC permits "PyGithub
**or** direct HTTP requests", so either is compliant, but an unused dependency is
noise. It was removed from `pyproject.toml` and `uv.lock` (`uv lock` reported
"Removed pygithub v2.10.0"), and the full suite still passes.

## Section 4 — AI coding agent integration

> Evaluate Jules and Gemini CLI from official docs; implement
> `CodingAgent.run_task(...)`; keep providers isolated; explain limitations
> rather than fabricate; start with one provider.

**Status: DONE (evaluation) / PARTIAL (no live run).**

- Evaluation: `docs/agent-evaluation.md` records Jules and Gemini CLI capabilities
  with inline source URLs, dated 2026-10-22.
- Interface: `agent_runner.CodingAgent` is a `Protocol` exposing only
  `run_task(context, plan, *, timeout)` (`agent_runner.py:89–101`). No `publish`,
  `push`, or credential method exists — `tests/test_approval.py::test_agent_runner_has_no_github_credential`.
- Providers isolated: `JulesAgent` and `MockAgent` are separate classes behind the
  protocol; `tests/test_agent_runner.py`.
- Limitation stated, not faked: without `JULES_API_KEY`,
  `test_jules_without_key_refuses_before_any_request` proves the provider refuses
  before any request.

## Section 5 — Issue discovery and ranking

> Search + metadata; small configurable allowlist; six weighted factors summing
> to 1.0; show reasons for every ranking; let the operator select; do not claim
> suitability without checking current state and repository instructions.

**Status: DONE.**

- Weights exactly as specified (25/20/20/15/10/10) in
  `config/repositories.yaml`, validated to sum to 1.0 (`tests/test_config.py`).
- Reasons for every factor: `ranking.py` attaches a human-readable reason;
  `tests/test_ranking.py`.
- The caveat is stated verbatim in the README ("not proven predictors of PR
  acceptance").
- Current state and contribution rules are checked: `dedupe.py` filters closed,
  assigned, linked-open-PR, and opt-out-labelled issues; `discovery.py` reads
  CONTRIBUTING presence.

## Section 6 — Repository structure

**Status: DONE.**

Every path the SPEC lists exists (verified with a file check):

```
src/forgeflow/{discovery,ranking,repository_context,issue_analyzer,
  agent_runner,sandbox,validation,approval,publisher,outcome_tracker,
  models,cli}.py     all present
README.md pyproject.toml .env.example config/repositories.yaml
docs/{architecture,threat-model,evaluation}.md
data/.gitkeep
```

- "Keep secrets, databases, logs, and agent workspaces out of version control":
  `.gitignore` ignores `.env`, `data/*.db`, `data/reports/`, `data/workspaces/`.
  Confirmed: `git ls-files data/` returns only `data/.gitkeep`.
- Additional modules beyond the SPEC's list (e.g. `dedupe.py`, `killswitch.py`,
  `audit.py`) are justified by later SPEC sections; the SPEC permits adapting the
  structure.

## Section 7 — Validation and quality gates

> Run the repo's documented tests and lint/type checks; record command, exit
> status, output, skipped checks, environment limits; never claim a pass without
> execution evidence; inspect the full diff; cap repairs at two; failed checks
> block publishing unless an explicit documented exception.

**Status: DONE (code) / PARTIAL (execution blocked here).**

- Command detection: `validation.detect_commands` never invents a check
  (`tests/test_validation.py::test_detect_commands_never_invents_checks`).
- Evidence discipline: a pass comes only from exit 0
  (`test_passed_only_from_exit_zero`); a timeout is `ERRORED`, not passed
  (`test_timeout_is_errored_not_passed`); no isolation ⇒ every check `skipped`
  (`test_no_isolation_never_reports_passed`).
- Diff inspection: `analyse_diff_findings` flags generated files, dependency
  changes, test weakening, and destructive lines, with blocking severity
  (`tests/test_validation.py`).
- Repair cap: `MAX_REPAIR_ATTEMPTS = 2` (`validation.py:35`);
  `tests/test_phase3_end_to_end.py::test_repair_loop_is_capped_by_spec`.
- Blocking + exception: `tests/test_approval_gate.py` (blocked on failure, on
  error, and when nothing ran; allowed with a documented exception).
- PARTIAL because no repository check has actually executed here (no container
  runtime); every recorded check in this environment is `skipped`.

## Section 8 — Human approval and publishing

> A real approval boundary; discovery/coding hold no publishing credentials;
> verify approval, patch hash, destination repo, target branch, current patch;
> provide approve / request-changes / reject / discard; no external write without
> explicit authorization; prefer draft PRs; never auto-merge.

**Status: DONE.**

- Boundary: the write path is `publisher.py` only; `PUBLISH_ALLOWLIST` names the
  one permitted repository. `tests/test_approval.py::test_approval_module_holds_no_credential`,
  `test_no_module_in_the_package_creates_a_pull_request`.
- Verification: `plan_publish` runs eight ordered checks (allowlist, idempotence,
  task state, explicit approval, approval binding, patch hash, target branch,
  permission scope). `tests/test_publisher.py`.
- Four actions: `approval.apply_action` (approve / request-changes / reject /
  discard). `tests/test_approval.py`.
- No unauthorized write: `test_third_party_repo_is_refused_and_no_write_is_attempted`,
  `test_dry_run_makes_no_write`.
- Draft only, no force, no merge: `test_draft_flag_cannot_be_turned_off`,
  `test_push_branch_never_updates_an_existing_ref`, and the PR body states it is
  not auto-merged (`test_pr_body_states_it_is_not_auto_merged`).

## Section 9 — Security and safeguards

| Safeguard | Status | Evidence |
| --- | --- | --- |
| Treat repo text as untrusted; no instruction override | DONE | `prompts.py` fences untrusted text; `tests/test_issue_analyzer.py::test_injected_issue_text_is_fenced_in_the_prompt`, `test_system_prompt_states_the_fenced_content_is_data` |
| Run untrusted code in isolated, resource-limited env | PARTIAL | `sandbox.py` (CPU/memory/time/network limits, read-only mount); refuses with no runtime (`tests/test_sandbox.py`). Not exercised here. |
| Do not expose files/keys/tokens to the sandbox | DONE | `sandbox.py` passes no env and no mounts; `tests/test_sandbox.py` |
| Least-privilege, separate execution vs publishing | DONE | Separate `FORGEFLOW_PUBLISH_TOKEN`; `tests/test_publish_isolation.py` |
| Protect against prompt injection | DONE | Fencing + `extra="forbid"` + `injection_flagged`; `tests/test_issue_analyzer.py` |
| Protect against credential leakage | DONE | `security.py` redaction; `tests/test_validation.py` (redaction) |
| Protect against destructive commands | DONE | `validation.scan_command` + diff findings; `tests/test_validation.py` |
| Protect against infinite loops | DONE | Sandbox timeout, `max_retries`, `MAX_REPAIR_ATTEMPTS`; `tests/test_validation.py`, `tests/test_phase3_end_to_end.py` |
| Protect against excessive API calls / runaway spend | DONE | `check_budget()` before every paid call; `tests/test_issue_analyzer.py` |
| Per-task timeouts, retry limits, budgets, audit logs, kill switch | DONE | `killswitch.TaskGuard`, `audit.py`, `llm_usage`; `tests/test_security_phase3.py`, `tests/test_phase2_completion.py` |
| Do not run arbitrary repo scripts on the host | DONE | No host-execution path exists; `tests/test_sandbox.py` asserts `echo` is refused without isolation |

PARTIAL on the isolation row only, because no runtime exists here. The refusal
itself is fully tested.

## Section 10 — Storage and data models

> SQLite tracking repositories, issues, scores, runs, plans, patch hashes, test
> results, approvals, PR URLs, feedback, status changes, costs; explicit states;
> enforced transitions; idempotent operations.

**Status: DONE.**

- Schema: `storage.py` plus per-module tables (`validation_results`,
  `approvals`, `publications`, `pull_request_outcomes`, `outcome_metrics`,
  `llm_usage`, `audit_log`, `kill_switch`, `task_guard`, `schedule_state`). ER
  diagram in `docs/architecture.md`.
- All twelve SPEC states exist (`models.TaskState`).
- Transitions enforced: `models.LEGAL_TRANSITIONS` +
  `storage.transition_task_state` raises `IllegalTransitionError`;
  `tests/test_models.py`, `tests/test_approval.py::test_illegal_transition_is_refused`.
- Idempotence: `test_init_db_is_idempotent`, `test_issue_score_upsert_is_idempotent`,
  `test_pipeline_is_idempotent_end_to_end`, `test_duplicate_publish_is_prevented`,
  `test_idempotence_is_per_patch_not_per_issue`.

## Section 11 — Metrics and evaluation

> Measure the nine named metrics; do not optimise only for count or merge rate;
> include usefulness, test quality, complexity, maintainer feedback; build a
> harness with known issues or a benchmark; do not claim benchmark performance
> without reproducible measurement; use repositories you own for end-to-end tests.

**Status: DONE (harness) / PARTIAL (one real, synthetic publish).**

- All nine metrics implemented (`evaluation/harness.py`); `time_saved_hours`
  reports **not measured**; empty denominators report **not enough data**.
  `tests/test_evaluation.py`.
- Per-task fields present: `patch_usefulness`, `test_quality`, `complexity`,
  `maintainer_feedback`.
- Reproducible harness: `uv run python -m evaluation`; output quoted in
  `docs/evaluation.md`.
- Benchmark honesty: SWE-bench is **not** run; three deterministic offline patch
  cases stand in, and the output says so.
- PARTIAL: the only real publish is a synthetic patch to the author-owned sandbox
  repository (`forgeflow-sandbox#2`), so no real acceptance rate exists.

## Section 12 — Cost strategy

> Local-first; deterministic filters before any LLM; limit context and retries;
> record usage and cost; configurable monthly cap; verify pricing from official
> docs; do not promise a fixed tasks-per-budget.

**Status: DONE.**

- Local-first: no hosting; SQLite + one provider.
- Deterministic filters first: discovery/dedupe/ranking are free.
- Caps: `monthly_cap_usd`, `max_retries`, `max_output_tokens`, `timeout_seconds`,
  `TaskGuard` (see `docs/cost-controls.md`).
- Usage recorded: `llm_usage`; `tests/test_issue_analyzer.py`.
- Pricing honesty: every rate is `0.0` on purpose with a comment to fill from the
  vendor; `unknown_models()` flags unpriced models.
- No tasks-per-budget promise: stated in `docs/cost-controls.md`.

## Section 13 — Implementation roadmap (Phases 1–5)

| Phase | Status | Evidence |
| --- | --- | --- |
| 1 Discovery/ranking/SQLite/reports, no external writes | DONE | `discovery.py`, `ranking.py`, `storage.py`, `report.py`; `tests/` |
| 2 Context, agent adapter, analysis, isolation, patch, review | DONE (code) / PARTIAL (no key, no runtime) | `repository_context.py`, `agent_runner.py`, `issue_analyzer.py`, `sandbox.py`, `patch.py`, `review.py` |
| 3 Validation, logs, states, security, approval boundary, tests | DONE | `validation.py`, `approval.py`, `audit.py`, `killswitch.py`; `tests/test_phase3_end_to_end.py` |
| 4 Controlled publishing (draft PRs), feedback tracking, scheduling | DONE | `publisher.py`, `outcome_tracker.py`, `scheduler.py`; one real draft PR (`forgeflow-sandbox#2`) |
| 5 Docs, architecture, threat model, evaluation, demo, portfolio README | DONE | `docs/architecture.md`, `docs/threat-model.md`, `docs/evaluation.md`, `docs/cost-controls.md`, `docs/demonstration.md`, `demo/`, README |

Scheduling is present and off by default, discovery-only
(`tests/test_scheduler.py`), as the SPEC requires.

## Section 14 — Testing requirements

> Unit tests for scoring, dedup, transitions, patch-hash validation, cost limits,
> permission checks; mock GitHub and model APIs; integration tests against a repo
> I own; test that failed validation blocks publishing, modified patches
> invalidate approval, malicious instructions cannot grant permission, repeated
> runs do not duplicate, missing credentials fail safe; clear local setup and
> test command.

**Status: DONE / PARTIAL.**

| Required test | Status | Evidence |
| --- | --- | --- |
| Issue scoring | DONE | `tests/test_ranking.py` |
| Deduplication | DONE | `tests/test_dedupe.py` |
| State transitions | DONE | `tests/test_models.py`, `tests/test_storage.py` |
| Patch-hash validation | DONE | `tests/test_approval.py` (modified/single-byte/branch/repo mismatch) |
| Cost limits | DONE | `tests/test_issue_analyzer.py` (budget blocks, cap reached, no query) |
| Permission checks | DONE | `tests/test_publish_isolation.py`, `tests/test_publisher.py` (scope check) |
| Mocked GitHub and model APIs | DONE | `tests/conftest.py` `FakeGitHubClient`; `FakeBackend`; `httpx.MockTransport` |
| Failed validation blocks publishing | DONE | `tests/test_approval_gate.py` |
| Modified patch invalidates approval | DONE | `tests/test_approval.py` |
| Malicious instructions cannot grant permission | DONE | `tests/test_approval.py::test_agent_runner_has_no_github_credential`, `test_no_module_in_the_package_creates_a_pull_request` |
| Repeated runs do not duplicate | DONE | `tests/test_publisher.py`, `tests/test_pipeline.py` |
| Missing credentials fail safe | DONE | `tests/test_publish_isolation.py`, `tests/test_issue_analyzer.py::test_gemini_backend_refuses_before_network`, `tests/test_agent_runner.py::test_jules_without_key_refuses_before_any_request` |
| Integration test against a repo I own | PARTIAL | The one real run (draft PR to `forgeflow-sandbox`) was performed **manually**, not as an automated test, because automating it needs a write-scope token in CI, which the project deliberately avoids. Recorded in `docs/threat-model.md`. |

Clear setup and test command: `docs/setup.md`, README "Setup" and "Test command".

## Section 15 — Portfolio deliverables

> Clean documented source; reproducible local setup; sample config; `.env.example`
> without real secrets; unit and integration tests; architecture and threat-model
> docs; evaluation methodology and results; cost controls; a demo using a repo I
> own; record real upstream PR links with accurate status; never fabricate.

**Status: DONE.**

- Source documented: module docstrings + `docs/architecture.md`.
- Reproducible setup: `docs/setup.md`; `make setup`; `uv.lock` committed.
- Sample config: `config/repositories.yaml`.
- `.env.example` with **empty** values only (verified: no `=` with a value).
- Tests: unit + end-to-end (`tests/test_phase3_end_to_end.py`).
- Architecture: `docs/architecture.md`. Threat model: `docs/threat-model.md`.
- Evaluation methodology and results: `docs/evaluation.md`.
- Cost controls: `docs/cost-controls.md`.
- Demo using a repo I own: `demo/` (offline dry run) and the manual real run
  against `forgeflow-sandbox`.
- Real PR links with accurate status: README "Contributions", checked read-only
  against the API — `forgeflow-sandbox#2` is **open, draft, not merged**; upstream
  repositories: **none yet**.
- No fabrication: metrics are labelled fixture arithmetic; SWE-bench is not
  claimed; the one publish is labelled synthetic.

## Section 16 — Development rules

> Inspect the environment and plan first; verify third-party features from
> official docs; implement in small reviewable steps; state files before each
> step; run tests and report actual results after each step; no untested dumps;
> no invented commands; no secret exposure; keep it runnable; inspect existing
> files; request permission before destructive actions.

**Status: DONE.**

- Environment inspected and constraints recorded: `docs/agent-evaluation.md`
  (dated), `docs/phase2-notes.md`, `AGENTS.md`.
- Third-party claims sourced from official docs: `docs/agent-evaluation.md` cites
  vendor URLs inline.
- Small steps with real gate output: the git history is a sequence of focused
  commits, each gated on ruff + pytest.
- No secret exposure: `.env` gitignored; no real secret in the tree or history
  (scan below).
- Runnable after each step: the suite passes at 923 tests.

---

## Secret scan (working tree and full git history)

Run as part of this audit. Commands and their real output:

**1. Tracked files that look like secrets, databases, or workspaces**

```
$ git ls-files | grep -Ei '(^|/)\.env($|\.)|\.db$|\.sqlite$|workspaces/|\.pem$|id_rsa|credentials|\.key$'
.env.example
```

Only `.env.example`, which is expected. No `.db`, no workspace, no key file is
tracked. `git ls-files data/` returns only `data/.gitkeep`.

**2. `.env.example` contains no values**

```
$ grep -nE '=[^[:space:]]' .env.example
(none)
```

Every variable is empty. No local `.env` exists in the tree.

**3. Token-like patterns in tracked files**

```
$ git grep -nEI '(ghp_...|github_pat_...|gho_...|AIza...|sk-...|xox...|AKIA...|-----BEGIN ... PRIVATE KEY-----)'
tests/test_security_phase3.py, tests/test_validation.py,
tests/test_validation_exception_cli.py, tests/test_validation_storage.py
```

All matches are the **synthetic fixtures the redactor tests use** (for example
`ghp_abcdefghijklmnopqrstuvwxyz0123456789`), which exist precisely to prove
redaction removes them. No real credential.

**4. Files ever committed across all history**

```
$ git log --all --pretty=format: --name-only --diff-filter=A | sort -u \
    | grep -Ei '\.env|\.db$|\.sqlite$|workspaces/|\.pem$|id_rsa|\.key$|credentials'
.env.example
```

No `.env`, database, workspace, or key file has **ever** been committed on any
branch.

**5. Every blob in history, scanned for token patterns**

```
$ git rev-list --all --objects | awk '{print $1}' | sort -u > objs.txt   # 405 objects
$ while read o; do git cat-file -p "$o" | grep -aEIn '(ghp_...|AIza...|PRIVATE KEY...)'; done
```

Four blobs match, all four being the same synthetic redactor fixtures named in
step 3 (`tests/test_validation.py`, `tests/test_security_phase3.py`,
`tests/test_validation_storage.py`, `tests/test_validation_exception_cli.py`). No
real token appears in any commit in history.

**Conclusion.** No secret, database, or workspace is present in the working tree
or in the full git history. The only matches are deliberate test fixtures.

---

## Remaining gaps

Every requirement above is DONE or PARTIAL. No requirement is MISSING. The
PARTIAL items are all blocked by the environment, not by missing code:

1. **A real model call (SPEC 2E, 3, 4).** Needs `GEMINI_API_KEY` / `JULES_API_KEY`.
   `analyze` and `implement --provider jules` fail closed without them. Code and
   tests are complete; the live call cannot be made here.
2. **Real validation execution (SPEC 2G, 3, 7, 9).** Needs a container runtime.
   `sandbox.py` refuses rather than falling back to the host. Every recorded check
   in this environment is `skipped`.
3. **An automated integration test against an owned repository (SPEC 14).** The
   single real publish was manual. Automating it needs a write-scope token in CI,
   which this project deliberately avoids.
4. **No SWE-bench run (SPEC 11).** Needs a dataset, network, and a container
   runtime. Three offline patch cases stand in, and the output says so.

## Gate state

```
$ uv run python -m ruff check .
All checks passed!

$ uv run python -m ruff format --check .
82 files already formatted

$ uv run python -m pytest -q
923 passed
```
