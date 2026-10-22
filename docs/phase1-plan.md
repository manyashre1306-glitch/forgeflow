# ForgeFlow — Phase 1 Implementation Plan

Status: DRAFT for approval. No application code written yet.

This plan follows `docs/SPEC.md` (committed, sha256
`09cbc3a23f6f6583cae8c1db0c0b4456b35e08a0f363b9e69fd16a825176f098`).
It supersedes the earlier plan that was based on a guessed project.

Phase 1 scope, from SPEC section 13: "Build GitHub issue discovery, repository
allowlisting, deduplication, ranking, SQLite persistence, and Markdown reports.
No writes to external repositories."

The stack is taken from SPEC section 3: Python, Pydantic, SQLite, pytest, Ruff,
and Typer for the CLI. FastAPI is explicitly not used.

---

## 1. Environment inspection (measured, not assumed)

| Item | Value | How verified |
|---|---|---|
| OS | Debian GNU/Linux 13 (trixie), x86_64 | `/etc/os-release` |
| CPUs / RAM | 4 / 15 GiB (about 13 GiB available) | `nproc`, `free -h` |
| Python | 3.13.15 at `/usr/local/bin/python3` | `python3 --version` |
| pip | 26.2.1 | `python3 -m pip --version` |
| uv | 0.12.15 | `uv --version` |
| git | 2.47.3 | `git --version` |
| ruff | 0.17.1 (Python module only; no PATH shim) | `python3 -m ruff --version` |
| pytest | NOT installed | `python3 -m pytest` -> No module named pytest |
| Docker CLI | 29.9.1 present | `docker --version` |
| Docker daemon | NOT running | `docker ps` -> `dial unix /var/run/docker.sock: no such file or directory` |
| PyPI reachable | yes, HTTP 200 | `urllib.request.urlopen('https://pypi.org/simple/')` |

GitHub API limits, measured live against the API with the environment's
`GITHUB_TOKEN` (primary source, `/rate_limit` endpoint):

| Resource | Limit | Notes |
|---|---|---|
| `core` (authenticated) | 5000 / hour | 4908 remaining at inspection time |
| `search` (authenticated) | 30 / minute | the binding limit for discovery |
| `search` (unauthenticated) | 10 / minute | fallback only |
| `code_search` (authenticated) | 10 / minute | only if code search is ever added |

Token scopes reported by the API: `x-oauth-scopes:` is empty, meaning a
fine-grained token. Phase 1 needs only read access, so no scope change is
required. No token value is written to any file (AGENTS.md rule 5).

**Consequences for the plan**

1. Docker is unavailable, but **Phase 1 does not execute untrusted code**, so no
   sandbox is needed. Docker matters in Phase 2 and later (SPEC section 9). It is
   not a Phase 1 blocker.
2. `search` at 30 requests/minute is the real constraint. Discovery must batch
   and cache, not fire one request per repository per run.
3. pytest must be added as a dev dependency; ruff must be invoked as
   `python3 -m ruff` (or via `uv run`).
4. Python 3.13 is the target. SPEC section 3 says "Python 3.12 or a currently
   supported version"; 3.13 satisfies that.

---

## 2. Architecture

Phase 1 is a read-only pipeline that turns a repository allowlist into ranked,
de-duplicated issues persisted in SQLite, plus Markdown reports. There is no
publishing path in Phase 1 at all — no code that can write to GitHub.

```
config/repositories.yaml
        |
        v
+-------------------+     +------------------+
| discovery.py      |---->| models.py        |  Pydantic: Repo, Issue,
| GitHub search +   |     | (validated data) |  IssueScore, RankedIssue
| repo metadata     |     +------------------+
+-------------------+              |
        |                          v
        |                 +------------------+
        |                 | dedupe.py        |  drop duplicates, closed,
        |                 |                  |  already-fixed, opt-out labels
        |                 +------------------+
        |                          |
        |                          v
        |                 +------------------+
        |                 | ranking.py       |  weighted score + reasons
        |                 +------------------+
        |                          |
        v                          v
+---------------------------------------------------+
| storage.py    SQLite: repositories, issues,       |
|               issue_scores, runs                  |
+---------------------------------------------------+
        |
        v
+-------------------+
| report.py         |  Markdown report of ranked issues
+-------------------+

cli.py (Typer) drives: discover -> rank -> report
```

Design rules:

- `github_client.py` is the only module that performs HTTP. It is injected into
  `discovery.py`, so tests use a fake client and never touch the network.
- `models.py` holds Pydantic models with strict validation. Data crossing a
  module boundary is a model, not a dict.
- `ranking.py` is pure: given a list of `Issue` plus config weights, it returns
  `RankedIssue` objects each carrying per-factor scores and human-readable
  reasons. No I/O, so it is fully unit-testable.
- `dedupe.py` is pure for the same reason.
- `storage.py` owns all SQL. It exposes idempotent upserts keyed on
  `(repo_full_name, issue_number)`, satisfying SPEC section 10's idempotency
  requirement: running discovery twice must not create duplicate rows.
- `report.py` writes Markdown only under `data/reports/`, which is gitignored.
- There is deliberately **no** `publisher.py`, `approval.py`, or `sandbox.py` in
  Phase 1. Those belong to Phases 2 to 4. Creating empty stubs would be
  speculative, so I am not creating them.

### Ranking model (SPEC section 5)

Weights are configurable and default to the spec's values:

| Factor | Default weight |
|---|---|
| skill relevance | 25% |
| issue clarity | 20% |
| feasible scope | 20% |
| testability | 15% |
| repository health | 10% |
| duplicate/conflict risk | 10% |

Each factor returns a 0.0 to 1.0 sub-score; the total is the weighted sum scaled
to 0 to 100. Every factor records a reason string, because SPEC section 5
requires "the reasons for every ranking". The spec calls these "initial
heuristics, not proven predictors of PR acceptance", and the report will say so
explicitly.

Sub-score heuristics, all deterministic (no LLM in Phase 1, per the cost
strategy in section 12):

- skill relevance: keyword and label match against a configurable skill list
  (python, ai, ml, data-science, devtools) over title, body, and labels.
- issue clarity: body length bands, presence of reproduction steps, expected
  vs actual behaviour, and error output.
- feasible scope: penalty for `good first issue`-sized or enormous tasks;
  prefers issues whose body suggests a bounded change; penalises `epic`,
  `design`, or `discussion` labels.
- testability: presence of a reproduction, a linked test file path, or a
  `bug` label raises the score.
- repository health: stars, recent commit recency, open issue count, and whether
  the repo has CONTRIBUTING and tests, all from repo metadata already fetched.
- duplicate/conflict risk: inverted signal. Open PRs referencing the issue,
  `duplicate` label, or many comments suggesting an in-flight fix lower it.
  This is the factor that most needs real data, so its heuristics are the most
  likely to be revised.

### Deduplication (SPEC section 2B)

Two distinct operations, kept separate because they answer different questions:

1. **Identity dedupe** — the same issue seen twice across runs or via two search
   queries collapses to one row via the SQLite unique key. This is idempotency.
2. **Eligibility filtering** — drop closed issues, issues with a linked merged
   PR, `duplicate`/`wontfix`/`invalid` labels, and repositories whose
   contribution rules opt out (for example a `no-bot` or `do-not-contribute`
   marker in the allowlist config). Each drop is recorded with a reason so the
   report can show what was excluded and why.

---

## 3. Dependency list, with reasons

Quotes are verbatim from official sources, fetched during this phase. Where a
source could not be fetched, the row says UNVERIFIED instead of quoting from
memory.

### Runtime

| Package | Reason | Official description | Source |
|---|---|---|---|
| `pydantic` | Validated models for issues, repos, scores | "Pydantic is the most widely used data validation library for Python." | https://docs.pydantic.dev/latest/ |
| `pydantic-settings` | Load config and token from environment | "Pydantic Settings provides optional Pydantic features for loading a settings or config class from environment variables or secrets files." | https://docs.pydantic.dev/latest/concepts/pydantic_settings/ |
| `PyGithub` | GitHub REST access (SPEC names PyGithub or direct HTTP) | "PyGitHub is a Python library to access the [GitHub REST API]. This library enables you to manage [GitHub] resources such as repositories, user profiles, and organizations in your Python applications." | https://github.com/PyGithub/PyGithub (repo README) |
| `httpx` | Direct HTTP where PyGithub is awkward (for example the search endpoint and rate-limit headers) | "HTTPX is a fully featured HTTP client for Python 3, which provides sync and async APIs, and support for both HTTP/1.1 and HTTP/2." | https://www.python-httpx.org/ |
| `PyYAML` | Parse `config/repositories.yaml` | "PyYAML is a YAML parser and emitter for Python." | https://pyyaml.org/wiki/PyYAMLDocumentation |
| `typer` | CLI (SPEC section 3 names Typer) | "Typer is a library for building CLI applications that users will love using and developers will love creating. Based on Python type hints." | https://typer.tiangolo.com/ |
| `rich` | Readable CLI tables for ranked results (also a Typer dependency) | UNVERIFIED — official docs landing page returned no descriptive line | https://rich.readthedocs.io/en/stable/ |

SQLite is used through the Python standard library `sqlite3` module, so it is
not a dependency. There is no need for SQLAlchemy in Phase 1; raw SQL with a
small, well-tested data-access layer is simpler and easier to review. If the
schema grows in later phases, that decision can be revisited.

### Development

| Package | Reason | Official description | Source |
|---|---|---|---|
| `pytest` | Test runner (SPEC section 3) | "The `pytest` framework makes it easy to write small, readable tests, and can scale to support complex functional testing for applications and libraries." | https://docs.pytest.org/en/stable/ |
| `ruff` | Lint and format (SPEC section 3) | "An extremely fast Python linter and code formatter, written in Rust." | https://docs.astral.sh/ruff/ |
| `uv` | Environment and dependency management (already installed) | "A single tool to replace `pip`, `pip-tools`, `pipx`, `poetry`, `pyenv`, `twine`, `virtualenv`, and more." | https://docs.astral.sh/uv/ |
| `pyyaml` types or `types-PyYAML` | Type hints for PyYAML under strict checking | UNVERIFIED — not yet checked | https://pypi.org/project/types-PyYAML/ |

### Deliberately NOT included

- **FastAPI / uvicorn** — Phase 1 has no HTTP surface. The spec's Phase 1 is a
  CLI and a local report.
- **An LLM SDK** — Phase 1 ranking is deterministic by design (SPEC section 12:
  "Use deterministic filters before calling an LLM"). Model calls begin in
  Phase 2.
- **SQLAlchemy / Alembic** — the Phase 1 schema is small and fixed; migrations
  are not yet justified.
- **pytest-httpx or responses** — useful later for HTTP-level tests, but the
  Phase 1 client boundary is a Python object that tests replace directly, which
  is simpler and avoids mocking the network library itself.
- **Docker tooling** — the daemon is not running and Phase 1 needs no sandbox.

Version pinning: lower bounds in Phase 2, then freeze with `uv lock`. Exact
versions are deliberately not guessed here.

---

## 4. Initial file tree

Files marked `(p2)` are created in Phase 2 (the MVP build). This phase (Phase 1)
creates only this document.

```
forgeflow/
├── AGENTS.md                       # exists
├── README.md                       # exists; expand in a later phase
├── pyproject.toml                  # (p2) metadata, deps, ruff + pytest config
├── uv.lock                         # (p2) generated
├── .gitignore                      # (p2) .venv, __pycache__, .ruff_cache,
│                                   #      data/*.db, data/reports/, .env
├── .env.example                    # (p2) GITHUB_TOKEN= (name only, no value)
├── config/
│   └── repositories.yaml           # (p2) allowlist + weights + skill keywords
├── src/
│   └── forgeflow/
│       ├── __init__.py             # (p2)
│       ├── config.py               # (p2) pydantic-settings + YAML loading
│       ├── models.py               # (p2) Repository, Issue, IssueScore, RankedIssue
│       ├── github_client.py        # (p2) the only module doing HTTP
│       ├── discovery.py            # (p2) search + metadata fetch
│       ├── dedupe.py               # (p2) identity dedupe + eligibility filters
│       ├── ranking.py              # (p2) weighted scoring + reasons
│       ├── storage.py              # (p2) SQLite schema and idempotent upserts
│       ├── report.py               # (p2) Markdown report writer
│       └── cli.py                  # (p2) Typer commands
├── tests/
│   ├── __init__.py                 # (p2)
│   ├── conftest.py                 # (p2) fake GitHub client, temp SQLite
│   ├── test_ranking.py             # (p2) scoring, weights, reasons
│   ├── test_dedupe.py              # (p2) duplicates and eligibility filters
│   ├── test_storage.py             # (p2) idempotency, no duplicate rows
│   ├── test_discovery.py           # (p2) against a fake client
│   └── test_cli.py                 # (p2) CLI smoke test
├── docs/
│   ├── SPEC.md                     # exists (committed)
│   ├── phase1-plan.md              # this file
│   ├── architecture.md             # later phase (SPEC section 6)
│   ├── threat-model.md             # later phase
│   └── evaluation.md               # later phase
└── data/
    └── .gitkeep                    # (p2) keeps the dir; DB and reports ignored
```

Deviations from the SPEC section 6 tree, and why:

- The spec lists `repository_context.py`, `issue_analyzer.py`, `agent_runner.py`,
  `sandbox.py`, `validation.py`, `approval.py`, `publisher.py`, and
  `outcome_tracker.py`. All of those serve Phases 2 to 4. Creating them now
  would be empty placeholders, so they are deferred.
- `dedupe.py`, `github_client.py`, `config.py`, `storage.py`, and `report.py`
  are added because Phase 1 explicitly requires deduplication, GitHub
  discovery, configurable allowlisting, SQLite persistence, and Markdown
  reports. SPEC section 6 permits adapting the structure.
- `docs/architecture.md`, `docs/threat-model.md`, and `docs/evaluation.md` are
  Phase 5 deliverables per the roadmap, so they are not created in Phase 1.

---

## 5. Setup commands

Not yet executed. Listed for review. `uv` is used because it is installed and
faster; the spec does not mandate a specific installer.

```bash
# 1. environment
uv venv --python 3.13
uv init --package --name forgeflow        # if pyproject.toml is absent

# 2. runtime dependencies
uv add pydantic pydantic-settings PyGithub httpx PyYAML typer rich

# 3. dev dependencies
uv add --dev pytest ruff

# 4. configure the token for read-only API access (never committed)
cp .env.example .env
# then set GITHUB_TOKEN in .env; the file is gitignored

# 5. quality gates (module form, because ruff has no PATH shim here)
uv run python -m ruff check .
uv run python -m ruff format --check .
uv run python -m pytest -q

# 6. Phase 1 usage, once the CLI exists
uv run python -m forgeflow.cli discover --config config/repositories.yaml
uv run python -m forgeflow.cli rank
uv run python -m forgeflow.cli report --out data/reports/phase1.md
```

The console-script name and exact CLI subcommands are provisional until the CLI
is written; they are not verified working.

---

## 6. Acceptance criteria for Phase 2 (the Phase 1 MVP build)

Phase 2 is complete only when each item below is demonstrated with real command
output pasted into the report (AGENTS.md rule 2, SPEC section 7). Anything not
met is reported as not met, with the actual error output.

1. `uv run python -m ruff check .` exits 0.
2. `uv run python -m ruff format --check .` exits 0.
3. `uv run python -m pytest -q` runs real tests and exits 0.
4. Ranking produces the spec's six weighted factors, and a unit test proves the
   weights sum to 1.0 and that changing a weight changes the ordering.
5. Every `RankedIssue` carries non-empty reason strings for each factor.
6. Deduplication: a test asserts the same issue returned by two queries yields
   one stored row, and that closed / duplicate-labelled / opted-out issues are
   excluded with a recorded reason.
7. Idempotency: running the discover-and-store path twice leaves the row count
   unchanged. This directly tests SPEC section 10's idempotency requirement.
8. SQLite persistence works with no server; the database file is created under
   `data/` and is gitignored.
9. A Markdown report is written containing, for each ranked issue: repo, issue
   number, title, URL, total score, per-factor scores, and reasons. The report
   states the spec's caveat that scores are heuristics, not predictors.
10. `git status` is clean after the commit, and no secret value appears in any
    committed file.
11. Tests never hit the live network: they use the injected fake GitHub client.
    A test asserts the CLI fails safely with a clear message when
    `GITHUB_TOKEN` is missing.
12. No module in Phase 1 can write to GitHub. This is verifiable by inspection:
    only read methods exist on the client wrapper.

---

## 7. Genuinely blocking questions

Only two, and both are about scope rather than environment.

1. **Which repositories go in the initial allowlist?** Phase 1 discovers from a
   configurable allowlist, and the spec says "repositories I want to contribute
   to". I need at least one real `owner/name` to make the MVP demonstrably work
   against live data. I can default to a small set of well-known Python/AI
   repositories if you prefer, but the choice affects ranking sanity.

2. **Is the search API surface acceptable?** The authenticated search limit is
   30 requests/minute, measured live. Phase 1 will batch queries per repository
   and cache results to stay well under it. If you intend to watch a large
   allowlist (say 50+ repos) on a tight schedule, the design needs a different
   caching strategy. Confirm the intended allowlist size so I can size this
   correctly.

Not blocking, stated as assumptions so they do not hold up work:

- **No LLM in Phase 1.** Ranking is deterministic, matching SPEC section 12's
  "use deterministic filters before calling an LLM". Model-backed analysis is
  Phase 2. Assumption stated per AGENTS.md rule 7.
- **SQLite via stdlib `sqlite3`,** not SQLAlchemy. Revisit if the schema grows.
- **Python 3.13** as the target version.
- **Reports under `data/reports/`,** gitignored, since SPEC section 6 says to
  keep local databases and logs out of version control.

---

## 8. What this phase did NOT do

- No application code was written.
- No dependencies were installed.
- The old, guess-based `docs/phase1-plan.md` was deleted and replaced by this
  spec-aligned version.
- No external write actions were performed: no PRs, no comments, no pushes to
  any other repository. All GitHub API calls made during this phase were reads.
