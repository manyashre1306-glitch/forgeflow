# Phase 1 Plan — forgeflow

Status: DRAFT for approval. No application code written yet.

This document is a plan only. Nothing here has been implemented.

---

## 0. Important context: there is no spec

`docs/SPEC.md` does not exist. Verified:

```
$ test -f docs/SPEC.md && echo EXISTS || echo MISSING
MISSING
$ find . -iname '*SPEC*' -not -path './.git/*'
(no results)
```

The repo currently contains only `README.md` (one line: `# forgeflow`) and `AGENTS.md`.
No spec content was ever provided in this conversation, so no spec file was created.

Because there is no spec, the domain of `forgeflow` is **unknown**. The name suggests a
workflow / pipeline orchestration tool, but that is a guess. All architecture below rests
on one stated assumption (see section 8). If the assumption is wrong, this plan changes.

---

## 1. Environment inspection (real output)

Inspected on this machine. These are measured facts, not assumptions.

| Item | Value | How verified |
|---|---|---|
| OS | Debian GNU/Linux 13 (trixie), x86_64 | `/etc/os-release`, `uname -a` |
| Kernel | 6.8.0-1055-gke | `uname -a` |
| CPUs | 4 | `nproc` |
| RAM | 15 GiB total, ~13 GiB available | `free -h` |
| Python | 3.13.15 at `/usr/local/bin/python3` | `python3 --version` |
| pip | 26.2.1 | `python3 -m pip --version` |
| uv | 0.12.15 | `uv --version` |
| git | 2.47.3 | `git --version` |
| Node | v24.21.0 | `node --version` |
| npm | 11.21.0 | `npm --version` |
| Docker client | 29.9.1 (daemon NOT reachable) | `docker --version`, `docker ps` |
| gcc | 14.2.0 | `gcc --version` |
| make | present at `/usr/bin/make` | `which make` |
| poetry | not installed | `poetry --version` -> command not found |
| ruff | 0.17.1 (installed as a Python module; no PATH shim) | `python3 -m ruff --version` |
| pytest | NOT installed | `python3 -m pytest` -> No module named pytest |
| PyPI reachable | yes, HTTP 200 | `urllib.request.urlopen('https://pypi.org/simple/')` |

Notable findings that affect the plan:

1. **Docker daemon is not running.**
   ```
   $ docker ps
   failed to connect to the docker API at unix:///var/run/docker.sock;
   check if the path is correct and if the daemon is running:
   dial unix /var/run/docker.sock: connect: no such file or directory
   ```
   The CLI exists but there is no daemon. Anything requiring containers
   (test databases, integration tests) will not work until a daemon is started.

2. **ruff has no PATH entry.** `ruff` on PATH fails (`command not found`), but
   `python3 -m ruff` works. All commands in this plan use the module form.

3. **pytest is not installed.** Phase 2 must add it as a dev dependency.

4. **`.ruff_cache/` is untracked and self-ignored.** ruff writes a nested
   `.ruff_cache/.gitignore` containing `*`, so the cache never pollutes git.
   No repo `.gitignore` exists yet; Phase 2 should add one.

5. **Git remote uses an embedded token.** `git remote -v` shows
   `https://<secret-hidden>@github.com/manyashre1306-glitch/forgeflow.git`.
   Per AGENTS.md rule 5, no secret value is written into any file in this repo.

Current git state: branch `chore/add-agents-md`, one commit ahead of `main`
(`17d7ff6 docs: add AGENTS.md with permanent repo rules`).

---

## 2. Stated assumption

**Assumption A1:** `forgeflow` is a Python 3.13+ service with two entry points:
a command-line interface and an HTTP API, backed by a SQL database, plus
outbound HTTP calls to third-party services.

Rationale: this is the most common shape for a tool with this kind of name, and
it exercises every environment capability above. If the real target is different
(e.g. a static site, a data pipeline, a library only, or a different language),
say so and I will rewrite sections 3 to 6.

---

## 3. Proposed architecture

Layered, single package, dependencies pointing inward.

```
        +-------------------------------+
        |  cli/         api/            |   entry points (thin adapters)
        |  (Typer)      (FastAPI)       |
        +---------------+---------------+
                        |
        +---------------v---------------+
        |  services/                    |   business logic, no I/O frameworks
        +---------------+---------------+
                        |
        +---------------v---------------+
        |  repositories/  clients/      |   persistence + outbound HTTP
        |  (SQLModel)     (HTTPX)       |
        +---------------+---------------+
                        |
        +---------------v---------------+
        |  config.py      models.py     |   settings + shared schemas
        +-------------------------------+
```

Principles:

- `services/` holds business logic and must not import FastAPI, Typer, or HTTPX.
  This keeps it unit-testable without a running server.
- `repositories/` and `clients/` are the only places that touch a database
  engine or the network, so both can be swapped in tests.
- `api/` and `cli/` are thin adapters that parse input and call `services/`.
- Configuration comes from the environment via pydantic-settings. No secrets in
  files (AGENTS.md rule 5).
- Async I/O throughout the API path, using a single event loop.

Explicitly out of scope for Phase 2 unless you say otherwise: authentication,
background workers, migrations tooling, and container packaging.

---

## 4. Dependency list, with reasons

Quotes below are verbatim from each project's official documentation, fetched
during this phase. Where a fetch did not return a usable descriptive line, the
row is explicitly marked UNVERIFIED rather than filled from memory.

### Runtime

| Package | Reason | Official description | Source |
|---|---|---|---|
| `fastapi` | HTTP API layer | UNVERIFIED — no descriptive line was returned by the fetch; tagline not quoted from memory | https://fastapi.tiangolo.com/ |
| `uvicorn` | ASGI server for FastAPI | UNVERIFIED — official page failed to fetch; no quote obtained | https://www.uvicorn.org/ |
| `pydantic` | Request/response validation | "Pydantic is the most widely used data validation library for Python." | https://docs.pydantic.dev/latest/ |
| `pydantic-settings` | Env-based configuration | "Pydantic Settings provides optional Pydantic features for loading a settings or config class from environment variables or secrets files." | https://docs.pydantic.dev/latest/concepts/pydantic_settings/ |
| `sqlmodel` | Database models | "SQLModel, SQL databases in Python, designed for simplicity, compatibility, and robustness." | https://sqlmodel.tiangolo.com/ |
| `httpx` | Outbound HTTP client | "HTTPX is a fully featured HTTP client for Python 3, which provides sync and async APIs, and support for both HTTP/1.1 and HTTP/2." | https://www.python-httpx.org/ |
| `typer` | CLI entry point | "Typer is a library for building CLI applications that users will love using and developers will love creating. Based on Python type hints." | https://typer.tiangolo.com/ |
| `rich` | CLI output formatting (also a Typer dependency) | UNVERIFIED — docs landing page returned no descriptive line; no quote obtained | https://rich.readthedocs.io/en/stable/ |

Note: the UNVERIFIED rows are still reasonable dependency choices, but the
claim "this is what the project says about itself" is not made for them. I will
verify them properly before Phase 2 installs anything.

### Development

| Package | Reason | Official description (verbatim) | Source |
|---|---|---|---|
| `pytest` | Test runner | "The `pytest` framework makes it easy to write small, readable tests, and can scale to support complex functional testing for applications and libraries." | https://docs.pytest.org/en/stable/ |
| `ruff` | Lint + format (one tool, already installed) | "An extremely fast Python linter and code formatter, written in Rust." | https://docs.astral.sh/ruff/ |
| `uv` | Env + dependency management (already installed) | "A single tool to replace `pip`, `pip-tools`, `pipx`, `poetry`, `pyenv`, `twine`, `virtualenv`, and more." | https://docs.astral.sh/uv/ |

### Deliberately NOT included (and why)

- **`alembic` / migrations** — no schema exists yet and the DB engine is not
  chosen. Adding it now would be speculative. Revisit in Phase 3.
- **`docker`-based test databases** — the daemon is not running (section 1).
- **`requests`** — `httpx` covers the same ground with async support.
- **`click`** — Typer supersedes it; its docs note "Typer used to depend on
  Click as well" (https://typer.tiangolo.com/).

Version pinning strategy: lower-bound pins only in Phase 2, then freeze with
`uv lock`. Exact versions are deliberately not guessed here.

---

## 5. Initial file tree

Proposed target after Phase 2. Files marked `(p2)` are created in Phase 2;
everything else is created in later phases.

```
forgeflow/
├── AGENTS.md                      # exists
├── README.md                      # exists (expand later)
├── pyproject.toml                 # (p2) project metadata, deps, tool config
├── uv.lock                        # (p2) generated lockfile
├── .gitignore                     # (p2) .venv, __pycache__, .ruff_cache, *.db
├── .env.example                   # (p2) names only, no secret values
├── docs/
│   └── phase1-plan.md             # this file
├── src/
│   └── forgeflow/
│       ├── __init__.py            # (p2)
│       ├── config.py              # (p2) pydantic-settings Settings
│       ├── models.py              # (p2) shared SQLModel/Pydantic schemas
│       ├── api/
│       │   ├── __init__.py
│       │   ├── app.py             # (p2) FastAPI factory
│       │   └── routes.py          # (p2) endpoints
│       ├── cli/
│       │   ├── __init__.py
│       │   └── main.py            # (p2) Typer app
│       ├── services/
│       │   ├── __init__.py
│       │   └── core.py            # (p2) business logic
│       ├── repositories/
│       │   ├── __init__.py
│       │   └── db.py              # (p2) engine + session helpers
│       └── clients/
│           ├── __init__.py
│           └── http.py            # (p2) httpx wrapper
└── tests/
    ├── __init__.py
    ├── test_config.py             # (p2) settings load from env
    ├── test_api.py                # (p2) endpoint smoke tests
    ├── test_cli.py                # (p2) CLI smoke tests
    └── test_services.py           # (p2) business-logic unit tests
```

Layout rationale: a `src/` layout prevents accidental imports of the working
directory and forces tests to run against the installed package.

---

## 6. Setup commands

These are the commands Phase 2 will run. They are listed here for review; they
have NOT been executed as part of Phase 1.

```bash
# 1. create and pin the environment
uv venv --python 3.13
uv init --package --name forgeflow        # if pyproject.toml is absent

# 2. add runtime dependencies
uv add fastapi uvicorn pydantic pydantic-settings sqlmodel httpx typer rich

# 3. add dev dependencies
uv add --dev pytest ruff

# 4. verify tooling (module form, because ruff has no PATH shim here)
uv run python -m ruff --version
uv run python -m ruff check .
uv run python -m ruff format --check .
uv run python -m pytest -q

# 5. run the app once it exists
uv run uvicorn forgeflow.api.app:app --reload
uv run python -m forgeflow.cli.main --help
```

Note on the last two commands: the `uvicorn` and CLI invocations are the
intended shape, but the exact import path and console-script name depend on the
answers to the blocking questions below. They are not verified working.

---

## 7. Acceptance criteria for Phase 2

Phase 2 is done only when all of the following are demonstrated with real
command output pasted into the report (AGENTS.md rule 2):

1. `uv run python -m ruff check .` exits 0.
2. `uv run python -m ruff format --check .` exits 0.
3. `uv run python -m pytest -q` runs at least one real test and exits 0.
   Tests must exercise real code paths, not mocks, except where a network or
   database boundary makes that impossible (each such mock justified in the PR).
4. `uv run uvicorn forgeflow.api.app:app` starts and `GET /health` returns
   HTTP 200 with a JSON body.
5. `uv run python -m forgeflow.cli.main --help` prints usage and exits 0.
6. Settings load from environment variables; a missing required value produces a
   clear error, and no secret value appears in any committed file.
7. The repo has a `.gitignore` and `git status` is clean after the commit.
8. Work is committed on a branch (not `main`) with a clear message.

Anything not met is reported as not met, with the actual error output.

---

## 8. Blocking questions

These block correct design. I have not guessed at them.

1. **What is `forgeflow` actually for?** There is no spec. One sentence of
   domain description changes sections 3 to 6 substantially. This is the single
   most important answer.
2. **Deliverable shape** — is the target a CLI, an HTTP API, a library, or all
   three? My assumption A1 says CLI plus API.
3. **Database** — which engine (SQLite, PostgreSQL, other), and is one needed at
   all? The Docker daemon is currently down, which rules out containerised
   databases unless you want me to start it.
4. **Async vs sync** — do you want an async I/O stack, or is synchronous fine?
   This decides whether `httpx` is used in async or sync mode.
5. **Python version floor** — is 3.13 (what is installed) acceptable, or do you
   need 3.11/3.12 compatibility for deployment?
6. **Where does `docs/SPEC.md` come from?** If you have the spec text, paste it
   and I will reconcile this plan against it before Phase 2.

Non-blocking items I will decide myself unless told otherwise: `src/` layout,
lower-bound version pins, and the `ruff` rule set.

---

## 9. What Phase 1 did NOT do

- No application code was written.
- No dependencies were installed.
- No `pyproject.toml`, `.gitignore`, or source tree was created.
- The only file added in Phase 1 is this document.
