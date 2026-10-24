# Contributing to ForgeFlow

Thanks for looking. ForgeFlow is a local-first, human-in-the-loop assistant for
open-source contribution. This file explains how to run it, how to change it, and
the rules that keep it safe.

## Before you start

Read [`AGENTS.md`](AGENTS.md) and [`docs/SPEC.md`](docs/SPEC.md). `AGENTS.md`
holds the working rules for this repository; `docs/SPEC.md` is the specification
ForgeFlow is built against.

## Setup

```bash
git clone <your-fork-url> forgeflow
cd forgeflow
uv sync
```

For the website, install the optional extra:

```bash
uv sync --extra web
```

`uv.lock` is committed. Use `uv sync --frozen` when you want exactly the locked
versions, which is what CI does.

## The three gates

Run these after every change. CI runs the same three, plus the website tests.

```bash
uv run python -m ruff check .
uv run python -m ruff format --check .
uv run python -m pytest -q
```

Or in one step:

```bash
make check
```

The full suite is offline. It needs no `GITHUB_TOKEN`, no `GEMINI_API_KEY`, and no
container runtime. The GitHub client is injected, the model backend is a scripted
fake, the coding agent is `MockAgent`, and repository clones come from a local
fixture.

To include the Streamlit render tests, which need the extra:

```bash
make test-web
```

## What to change, and what not to

Good first contributions:

- a bug fix with a test that fails before it and passes after;
- a new test for a behaviour that is currently only asserted in a comment;
- documentation that is wrong, missing, or unclear;
- a new offline fixture case for the evaluation harness.

Please open an issue before starting anything large, so we can agree on the shape
first.

## Rules the code must keep

These are not style preferences. Tests fail if they are broken.

1. **Honesty over polish.** A check that could not run is `skipped`, never
   `passed`. A metric with no data says "not enough data", never `0.0`. Do not
   claim a benchmark, a result, or a number that was not measured.
2. **Fail closed.** A missing credential, a missing container runtime, a changed
   patch, or an over-budget month stops the run. Never add a fallback to a less
   safe path, in particular never add host execution when the sandbox refuses.
3. **Untrusted text stays fenced.** Every free-text field that comes from a
   repository is an `UntrustedText` and is rendered only inside its data fence.
   Instructions and data travel in separate arguments.
4. **Validate model output.** Analysis output is a Pydantic model with
   `extra="forbid"`.
5. **Budget before spend.** `check_budget()` runs before every paid model call.
   With no budget query wired in, it refuses to call.
6. **Approval binds to bytes.** An approval is bound to the SHA-256 of the exact
   diff. There is no force flag.
7. **Two credentials, never crossing.** Reads use `GITHUB_TOKEN`; the one write
   path uses `FORGEFLOW_PUBLISH_TOKEN`. Neither falls back to the other.
8. **No secrets in files.** Secrets come from the environment. Never write a
   token, key, or password into source, config, a test, or a doc.

## Website changes

The website (`web/`) is a thin presentation layer. It contains no discovery,
ranking, analysis, or publishing logic of its own; it calls the same functions the
CLI calls. If you need new behaviour, add it to `src/forgeflow/` and call it.

Two run modes, selected by `FORGEFLOW_WEB_MODE`:

- **`hosted`** (the default) runs on a shared server. It never reads the local
  database, never imports the publisher or the publishing token, and never
  executes repository code. Every secret is the visitor's own and lives only in
  the session.
- **`local`** runs on your own machine. It may use the real database and the real
  publishing token, but only through the existing modules and their guards.

The mode is a hard gate in code, not a label. `tests/test_web_isolation.py` fails
if a hosted-reachable module reaches the publisher or the token, and if the
hosted refusals stop refusing.

Streamlit must be imported lazily (inside a function), so the core install and CI
work without the extra. `web/ui.py` is the only module that imports it.

## Commits and pull requests

- Keep the project runnable after every commit.
- Write a commit message that says what changed and why.
- One pull request per issue.
- In the PR description, state what you ran and paste the real output. If a check
  could not run, say so; do not imply it passed.

## Reporting a security issue

ForgeFlow handles credentials and untrusted repository content. If you find a way
to reach the publishing token from the read path, or to make a hosted deployment
read a local file, please report it privately rather than in a public issue.

## License

MIT. See [`LICENSE`](LICENSE).
