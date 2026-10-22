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
