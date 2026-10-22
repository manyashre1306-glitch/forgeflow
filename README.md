# ForgeFlow

AI-powered open-source contribution automation platform. ForgeFlow helps find
suitable GitHub issues, analyze them, plan and validate fixes, and prepare pull
requests for explicit human approval. It is designed to support genuine
contributions, never to farm commits or contribution squares.

See `docs/SPEC.md` for the full project specification and
`docs/phase1-plan.md` for the current implementation plan.

## Status

Phase 1, scaffold step. The package, configuration, and CLI shell exist. Issue
discovery, deduplication, ranking, storage, and reporting are not implemented
yet. Nothing in this project writes to an external repository.

## Requirements

- Python 3.13 or newer
- [uv](https://docs.astral.sh/uv/) for environment and dependency management
- A GitHub token with read-only public access (for later discovery steps)

## Setup

```bash
uv venv --python 3.13
uv sync
cp .env.example .env    # then set GITHUB_TOKEN in .env
```

The `.env` file is gitignored. Never commit real credentials.

## Usage

```bash
uv run python -m forgeflow.cli --help
```

## Quality gates

```bash
uv run python -m ruff check .
uv run python -m ruff format --check .
uv run python -m pytest -q
```

## Layout

```
config/repositories.yaml   repository allowlist, ranking weights, skills
src/forgeflow/             application package
tests/                     test suite
data/                      local SQLite database and generated reports (ignored)
docs/                      specification and plans
```

## License

No license has been chosen yet.
