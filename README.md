# ForgeFlow

An AI-powered open-source contribution automation platform. ForgeFlow finds
suitable GitHub issues, analyzes them, plans fixes, implements them with a coding
agent, runs tests and quality checks, and prepares pull requests for **explicit
human approval**.

**Phase 1 (current) is read-only and heuristic.** It discovers issues from a
configurable allowlist, deduplicates them, ranks them with deterministic
heuristics, stores everything in SQLite, and writes a Markdown report. It cannot
write to GitHub, and it does not use an LLM.

## Status

Phase 1 is complete: discovery, deduplication, ranking, persistence, reporting,
and the CLI. Bug analysis, planning, implementation, validation, and publishing
are later phases and are not implemented.

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
- `data/reports/` — generated Markdown reports (gitignored)

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

Tests never touch the network. The GitHub client is injected, so the suite uses a
fake client and needs no `GITHUB_TOKEN`.

## Project layout

```
src/forgeflow/
  cli.py            Typer commands: discover, rank, report, shortlist
  config.py         allowlist + weights loading; token from the environment
  models.py         Pydantic models and the SPEC section 10 state machine
  github_client.py  the only module that performs HTTP; read-only
  discovery.py      repository metadata and issue search
  dedupe.py         identity dedupe and eligibility filtering, with reasons
  ranking.py        the six weighted factors; pure functions, no I/O
  storage.py        SQLite schema, idempotent upserts, state transitions
  report.py         Markdown report generation
config/repositories.yaml   allowlist, weights, skill keywords
docs/SPEC.md               the project specification
```

## License

No license has been chosen yet.
