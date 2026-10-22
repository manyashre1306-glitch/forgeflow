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
| 3 | Validation, task states, security checks, approval-boundary tests | Not started |
| 4 | Controlled publishing (draft PRs), feedback tracking | Not started |
| 5 | Documentation, threat model, evaluation, portfolio README | Not started |

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

ForgeFlow only needs public read access. A classic token with no scopes, or a
fine-grained token with public read-only access, is sufficient. **Phase 1 never
writes to GitHub.** The token is read from the environment (or `.env`) and is
never printed or logged.

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

> **Caveat.** These scores are initial heuristics, **not proven predictors of PR
> acceptance**. A high score means an issue matched the configured signals; it
> does not mean the issue is suitable. Always check the issue's current state and
> the repository's contribution instructions before starting work.

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
  cli.py                 Typer commands: discover, rank, report, shortlist,
                         context, analyze, implement, review
  config.py              allowlist, weights, LLM config; secrets from env only
  models.py              Pydantic models, state machine, untrusted-text model
  github_client.py       the only module that performs GitHub HTTP; read-only
  discovery.py           repository metadata and issue search
  dedupe.py              identity dedupe and eligibility filtering, with reasons
  ranking.py             the six weighted factors; pure functions, no I/O
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
config/repositories.yaml   allowlist, weights, skill keywords, LLM settings
docs/SPEC.md               the project specification
docs/agent-evaluation.md   verified Jules and Gemini CLI capabilities
docs/phase2-notes.md       Phase 2 limitations and untested items
```

## License

No license has been chosen yet.
