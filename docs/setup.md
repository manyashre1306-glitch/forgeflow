# Reproducible local setup

This is the full walkthrough for getting ForgeFlow running locally. It assumes
only a shell, Python, and git. Every command here was run in the environment this
project was developed in; where a step needs something that is absent (a model
key, a container runtime), the text says so.

## 1. Prerequisites

- **Python 3.13 or newer.** `uv` installs it for you if needed.
- **[uv](https://docs.astral.sh/uv/)** for dependency management.
- **git**, for the shallow clone that context extraction uses.
- **A GitHub token** (read-only) for live discovery. Optional: everything
  offline — tests, the evaluation harness — works without one.

### Windows

The core is portable to Windows. `uv sync`, the three gates, the evaluation
harness, and the demo all run in PowerShell unchanged. A few notes:

- **Use PowerShell or Windows Terminal, not `cmd.exe`.** The commands below use
  `&&`, which `cmd.exe` does not understand. In PowerShell, `;` also works.
- **Setting an environment variable differs.** Instead of
  `VAR=value command`, use one of:

  ```powershell
  $env:FORGEFLOW_WEB_MODE = "local"     # this session only
  $env:GITHUB_TOKEN = "..."             # this session only
  ```

  or set it persistently with `setx FORGEFLOW_WEB_MODE local` (a new shell is
  needed for `setx` to take effect).
- **The `.env` file still works.** `uv run` loads `.env` the same way on Windows,
  so `cp .env.example .env` becomes `Copy-Item .env.example .env` and everything
  else is unchanged.
- **The website needs the extra on every platform:** `uv sync --extra web`.

The context-extraction tests cover Windows path handling explicitly
(`tests/test_repository_context.py`), because POSIX and Windows path separators
differ and a bug there is silent.

## 2. Clone and sync

```bash
git clone <your-fork-url> forgeflow
cd forgeflow
uv sync
```

`uv sync` creates `.venv` from `uv.lock` and installs the project plus dev tools
(`pytest`, `ruff`). If you change `pyproject.toml`, run `uv lock` then `uv sync`.

## 3. Configure the environment

```bash
cp .env.example .env
```

`.env` is gitignored. Fill in only what you need:

| Variable | Needed for | Notes |
| --- | --- | --- |
| `GITHUB_TOKEN` | live `discover`, `track` | read-only is enough |
| `GEMINI_API_KEY` | `analyze` | absent ⇒ `analyze` fails closed, no request |
| `JULES_API_KEY` | `implement --provider jules` | only mintable in the Jules web app |
| `FORGEFLOW_PUBLISH_TOKEN` | `publish --confirm-real-publish` | separate write token; never falls back to `GITHUB_TOKEN` |
| `FORGEFLOW_KILL_SWITCH` | emergency stop | set to `1` to halt everything |

No secret is ever written to a tracked file. The example file ships every value
empty on purpose.

## 4. Configure the repository allowlist

Edit `config/repositories.yaml`:

- `repositories` — the allowlist. Nothing outside it is ever queried.
- `weights` — the six ranking factors; they must sum to 1.0.
- `skill_keywords` — what counts as a relevant skill.
- `llm.pricing` — **enter real prices** from the vendor's current page before
  trusting a cost report; they ship as `0.0` on purpose (see
  `docs/cost-controls.md`).

## 5. Run the gates

```bash
make check
```

This runs the same three gates CI runs:

```bash
uv run python -m ruff check .
uv run python -m ruff format --check .
uv run python -m pytest -q
```

No secrets are needed: the test suite is offline and mocks every GitHub and model
call.

## 6. First run (offline)

```bash
# Compute the SPEC section 11 metrics over controlled fixtures. No network.
uv run python -m evaluation
```

## 7. First run (live, read-only)

With `GITHUB_TOKEN` set:

```bash
uv run forgeflow discover --limit 5
uv run forgeflow rank --top 10
uv run forgeflow report
```

The report is written under `data/reports/`. `data/` and `data/*.db` are
gitignored.

## 8. Run the website (optional)

```bash
uv sync --extra web
uv run --extra web streamlit run web/app.py
```

The website defaults to **hosted** mode, where the database-backed and publishing
pages are locked. For the full app on your own machine:

```bash
FORGEFLOW_WEB_MODE=local uv run --extra web streamlit run web/app.py
```

Windows (PowerShell):

```powershell
$env:FORGEFLOW_WEB_MODE = "local"
uv run --extra web streamlit run web/app.py
```

Full run and deployment guide: [`deploy-website.md`](deploy-website.md).

## 9. What will not run here, and why

- **`analyze`** needs `GEMINI_API_KEY`; without it, it refuses rather than
  guessing.
- **`implement --provider jules`** needs `JULES_API_KEY`; without it, it refuses.
- **Sandbox execution** needs a container runtime. This environment has the
  Docker client but no daemon socket, and no `podman`, so the sandbox refuses to
  run repository code and records the check as `SKIPPED`, never `PASSED`. There
  is deliberately no host-execution fallback.
- **`publish --confirm-real-publish`** needs `FORGEFLOW_PUBLISH_TOKEN` and writes
  to GitHub. Use `publish` without that flag for a dry run.

## 10. Make targets

| Target | Command |
| --- | --- |
| `make setup` | `uv sync` |
| `make test` | `uv run python -m pytest -q` |
| `make lint` | `uv run python -m ruff check .` |
| `make format` | `uv run python -m ruff format .` |
| `make check` | lint + format check + tests |
| `make discover` | `uv run forgeflow discover` |
| `make rank` | `uv run forgeflow rank` |
| `make report` | `uv run forgeflow report` |
| `make eval` | `uv run python -m evaluation` |
| `make web` | `uv run --extra web streamlit run web/app.py` |
| `make test-web` | `uv run --extra web python -m pytest -q` |
| `make clean` | remove caches and the local database |
