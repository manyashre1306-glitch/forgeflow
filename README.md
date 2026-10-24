# ForgeFlow

An AI-powered open-source contribution automation platform. ForgeFlow finds
suitable GitHub issues, analyzes them, plans fixes, implements them with a coding
agent, runs tests and quality checks, and prepares pull requests for **explicit
human approval**.

**Phases 1 and 2 are read-only.** Nothing in this project writes to GitHub: no
pull requests, comments, branches, or issue edits. Phase 1 discovers issues from
a configurable allowlist, deduplicates them, ranks them with deterministic
heuristics, stores everything in SQLite, and writes a Markdown report. Phase 2
extracts repository context, analyses an issue with an LLM, drives a coding agent
behind a provider interface, captures the resulting patch with a SHA-256 hash,
and writes a local review bundle.

## Status

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Discovery, dedupe, ranking, SQLite, reports, CLI | Complete |
| 2 | Repo context, agent adapter, analysis, sandbox, patch + review | Complete, with a documented limitation |
| 3 | Validation, task states, security checks, approval-boundary tests | Complete |
| 4 | Controlled publishing (draft PRs), feedback tracking, discovery-only scheduling | Complete, with a documented limitation |
| 5 | Documentation, threat model, evaluation, portfolio README | Not started |

**Phase 4 real run.** Publishing was exercised end to end once, against the
sandbox repository on the publish allowlist: draft PR
[`manyashre1306-glitch/forgeflow-sandbox#2`](https://github.com/manyashre1306-glitch/forgeflow-sandbox/pull/2).
Its state is tracked with `forgeflow track`, which records it as **open** and
**draft** (not merged). No other repository has been written to.

**Phase 2 limitation, stated plainly.** ForgeFlow cannot run a real coding agent
end-to-end in the development environment used so far, for two independent
reasons: no `JULES_API_KEY` is present (and Jules keys can only be minted in the
Jules web app), and no container runtime is available (the Docker client is
installed but the daemon is not running, and `podman` is absent). Rather than
pretend, the code fails closed: `analyze` refuses to call a model without
`GEMINI_API_KEY`, and the sandbox refuses to execute untrusted repository code
with no isolation. See `docs/phase2-notes.md` for the measured evidence and an
explicit list of what could not be tested.

## Requirements

- Python 3.13 or newer
- [uv](https://docs.astral.sh/uv/) for dependency management
- A GitHub token (read-only is enough) for live discovery

## Setup

```bash
git clone <your-fork-url> forgeflow
cd forgeflow
uv sync
```

Copy the example environment file and add a token:

```bash
cp .env.example .env
# then edit .env and set GITHUB_TOKEN=...
```

ForgeFlow only needs public read access for discovery, ranking, analysis, and
outcome tracking. A classic token with no scopes, or a fine-grained token with
public read-only access, is sufficient. **Publishing is the only write path** and
uses a separate `FORGEFLOW_PUBLISH_TOKEN` (see "Publishing"). Tokens are read
from the environment (or `.env`) and are never printed or logged.

Common commands are wrapped in a Makefile (`make help` lists them):

```bash
make setup      # uv sync
make check      # lint, format check, and the full test suite
make test       # just the tests
make eval       # the offline evaluation harness
make demo       # the offline dry-run end-to-end demonstration
```

`make demo` runs the whole pipeline offline, from a discovered issue to a publish
that is planned in full and then not performed. It makes no network call and
writes nothing. See `docs/demonstration.md`.

See `docs/setup.md` for a full walkthrough and `Makefile` for every target.

Edit `config/repositories.yaml` to change the repository allowlist, the ranking
weights, and the skill keywords. Nothing outside the allowlist is ever queried.

## Usage

```bash
# 1. Discover open issues from the allowlist and store them.
uv run forgeflow discover

# 2. Rank the stored issues with the six weighted factors.
uv run forgeflow rank --top 10

# 3. Write a Markdown report to data/reports/.
uv run forgeflow report

# 4. Shortlist an issue you want to work on.
uv run forgeflow shortlist pydantic/pydantic#1234 --reason "clear bug, good scope"
```

Phase 2 commands:

```bash
# Extract repository context (shallow clone into data/workspaces/).
uv run forgeflow context pydantic/pydantic#1234

# Analyse the issue, then move it to awaiting_plan_approval.
# Needs GEMINI_API_KEY; refuses to run without it.
uv run forgeflow analyze pydantic/pydantic#1234

# Run a coding agent on the approved plan and record a hashed patch.
# Refuses unless the task is at awaiting_plan_approval.
uv run forgeflow implement pydantic/pydantic#1234 --plan "the approved plan"

# Write a local review bundle under data/reports/.
# Loads the stored analysis and the latest patch automatically.
uv run forgeflow review pydantic/pydantic#1234
```

Manage cloned workspaces (clones are large and disposable):

```bash
uv run forgeflow workspace list
uv run forgeflow workspace prune --keep 5 --dry-run
uv run forgeflow workspace remove --issue pydantic/pydantic#1234
```

Emergency stop. Either of these halts all model calls and sandbox execution:

```bash
FORGEFLOW_KILL_SWITCH=1 uv run forgeflow analyze pydantic/pydantic#1234
# or set kill_switch: true in config/repositories.yaml
```

`implement --provider mock` uses the deterministic in-process agent, which needs
no credentials. `--provider jules` additionally requires `--allow-external`, and
fails closed without `JULES_API_KEY`.

Useful options:

```bash
uv run forgeflow discover --limit 50 --no-linked-prs
uv run forgeflow discover --db data/forgeflow.db --config config/repositories.yaml
uv run forgeflow report --output data/reports/my-report.md
```

Run `uv run forgeflow --help` for the full list.

## How ranking works

Six factors from SPEC section 5, each scored 0.0 to 1.0 with a human-readable
reason, combined into a weighted total on a 0 to 100 scale:

| Factor | Default weight |
| --- | ---: |
| skill relevance | 25% |
| issue clarity | 20% |
| feasible scope | 20% |
| testability | 15% |
| repository health | 10% |
| duplicate/conflict risk | 10% |

Weights are configurable in `config/repositories.yaml` and must sum to 1.0.

### Outcome-driven adjustment (off by default)

SPEC section 11 asks tracked PR results to improve future ranking. ForgeFlow
adds a **small, capped** adjustment on top of the weighted total, controlled by
`outcome_ranking` in `config/repositories.yaml`:

- It is **off by default** (`enabled: false`). With it off, ranking is exactly
  the six factors above.
- It can move an issue by at most `max_adjustment_points` (default 3.0 points on
  the 0-100 scale), far less than any single factor's weight.
- It rewards maintainer **engagement**, not contribution count. A merge weighs
  most, an approval less, and a maintainer requesting changes still counts
  positively, because that is a real signal the task was worth doing. A PR closed
  with no engagement lowers the score slightly.
- Only the most recent `lookback` outcomes per repository count.

Track outcomes with `uv run forgeflow track <owner/repo#N>` and see the SPEC
section 11 metrics with `uv run forgeflow metrics`. `time_saved_hours` is
reported as 0.0 on purpose: it is not measured, and a constant estimate would be
a fabricated benchmark.

> **Caveat.** These scores are initial heuristics, **not proven predictors of PR
> acceptance**. A high score means an issue matched the configured signals; it
> does not mean the issue is suitable. Always check the issue's current state and
> the repository's contribution instructions before starting work.

## Publishing

Publishing is the only part of ForgeFlow that writes to GitHub, and it is
guarded. It needs a **separate** token, `FORGEFLOW_PUBLISH_TOKEN`, that never
falls back to `GITHUB_TOKEN`; the read path and the write path use different
credentials on purpose (SPEC sections 8 and 9).

```bash
# Preview what would be published. No write happens (dry run is the default).
uv run forgeflow publish pydantic/pydantic#1234

# Approve the latest patch, then publish for real. The approval binds to the
# SHA-256 of that exact diff.
uv run forgeflow approve pydantic/pydantic#1234 --reason "looks correct"
uv run forgeflow publish pydantic/pydantic#1234 --confirm-real-publish
```

What the real-mode publish does, in order, and how it fails:

- **Approval.** It requires a recorded approval bound to the SHA-256 of the
  exact diff. If the patch changed, the approval no longer matches and publishing
  refuses. There is no force flag.
- **Validation.** A failed or errored check blocks publishing unless a documented
  `validation-exception` authorizes that exact patch hash.
- **Issue verification.** It checks the issue exists using the **read-only**
  client, never the publishing token. Only a verified issue lets the PR body
  claim `Fixes #N`; an unverified reference (deleted, renamed, private, or a
  denied read) is labelled unverified in the body instead of being asserted. A
  closing keyword that points nowhere would be a false claim, so it is not made.
- **Missing token.** A missing `FORGEFLOW_PUBLISH_TOKEN` fails closed with a
  clean one-line error, never a traceback and never a fallback to `GITHUB_TOKEN`.
- **Draft by default.** The PR is opened as a draft where the API supports it.
  ForgeFlow never auto-merges and never force-pushes.

## Scheduling (discovery only, off by default)

SPEC section 13 says to add scheduling only after manual runs are reliable.
ForgeFlow therefore ships an **optional, discovery-only** schedule:

```bash
# Show the schedule state (disabled by default).
uv run forgeflow schedule

# Run one discovery cycle now, if it is due.
uv run forgeflow schedule --run
```

Rules that make this safe:

- **Discovery only.** The scheduler can run exactly one command, `discover`.
  There is no code path from the scheduler to publishing, validation, or a model
  call, and a test asserts it imports neither the publisher nor the token module.
- **Off by default.** Set `schedule.enabled: true` in `config/repositories.yaml`
  to turn it on. Nothing runs on a timer until you do.
- **No daemon.** There is no background thread or loop. A cycle runs only when a
  caller invokes `forgeflow schedule --run` and the interval has elapsed, so
  behaviour stays observable.
- **Kill switch honoured.** An engaged kill switch refuses the cycle before any
  GitHub client is built.

> **Turn scheduling on only after manual runs are reliable.** Run discovery,
> ranking, analysis, validation, and publishing by hand until each is dependable,
> then enable the schedule. Never schedule publishing: every external write needs
> explicit human approval (SPEC section 8), which a timer cannot give.

## Data

- `data/forgeflow.db` — SQLite database (gitignored)
- `data/reports/` — generated Markdown reports and review bundles (gitignored)
- `data/workspaces/` — shallow repository clones (gitignored, disposable)

SQLite needs no server. Delete the database file to start over.

## Development

```bash
# Lint
uv run python -m ruff check .

# Check formatting
uv run python -m ruff format --check .

# Run the test suite
uv run python -m pytest -q

# Apply formatting
uv run python -m ruff format .
```

Tests never touch the network. The GitHub client is injected, the LLM backend is
replaced by a scripted fake, the coding agent by `MockAgent`, and repository
clones come from a local fixture. The suite needs no `GITHUB_TOKEN`, no
`GEMINI_API_KEY`, and no Docker.

## Project layout

```
src/forgeflow/
  cli.py                 Typer commands: discover, schedule, rank, report,
                         shortlist, context, analyze, implement, review,
                         workspace, kill-switch, audit-log, track, metrics,
                         approve, request-changes, reject, discard, validate,
                         validation-exception, publish
  config.py              allowlist, weights, LLM config; secrets from env only
  models.py              Pydantic models, state machine, untrusted-text model
  github_client.py       the only module that performs GitHub HTTP; read-only
  discovery.py           repository metadata and issue search
  dedupe.py              identity dedupe and eligibility filtering, with reasons
  ranking.py             the six weighted factors + capped outcome adjustment
  storage.py             SQLite schema, upserts, state transitions, costs, patches
  report.py              Phase 1 Markdown report generation
  repository_context.py  shallow clone + size-limited context extraction
  prompts.py             prompt construction and the injection-defence boundary
  agent_runner.py        CodingAgent interface, JulesAgent, MockAgent
  issue_analyzer.py      LLM adapter, validated output, cost cap, retries
  sandbox.py             container isolation, or an explicit refusal
  patch.py               diff analysis and SHA-256 patch hashing
  pipeline.py            context -> analyze -> implement -> review orchestration
  review.py              local review bundle generation
  validation.py          runs the target repo's tests/lint/type checks
  approval.py            approve / request-changes / reject / discard boundary
  publisher.py           pre-publish checks, gated branch push, draft PR
  publish_token.py       the publishing credential; no fallback to GITHUB_TOKEN
  diff_apply.py          strict unified-diff applier for the branch push
  outcome_tracker.py     tracks PR state and aggregates outcomes per repository
  scheduler.py           optional discovery-only cycle (never publishes)
  audit.py               append-only audit log
  killswitch.py          persistent kill switch and per-task guardrails
  security.py            secret scrubbing for recorded output
evaluation/              SPEC section 11 metrics harness (offline fixtures)
demo/                    offline dry-run end-to-end demonstration (no writes)
config/repositories.yaml   allowlist, weights, skill keywords, LLM settings
docs/SPEC.md               the project specification
docs/agent-evaluation.md   verified Jules and Gemini CLI capabilities
docs/phase2-notes.md       Phase 2 limitations and untested items
docs/threat-model.md       threats and mitigations, including the publish path
docs/demonstration.md      what the dry-run demo shows, and what it does not
```

## License

No license has been chosen yet.
