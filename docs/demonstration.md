# Offline dry-run demonstration

This is a single-command walkthrough of the whole ForgeFlow pipeline, from a
discovered issue to a publish that is planned in full and then not performed. It
runs in one process, makes no network call, and writes nothing to GitHub.

```bash
uv run python -m demo
```

## What it does

It runs the real ForgeFlow functions in order, against a controlled fixture
repository and a controlled workspace on disk:

| Stage | Function | Result |
| --- | --- | --- |
| discover | `storage.upsert_issue` | issue seeded at `discovered` |
| rank | `ranking.rank_issues` | weighted total over the six factors |
| context | `pipeline.run_context` | README, CONTRIBUTING, and test layout read from the workspace |
| analyze | `pipeline.run_analysis` | validated plan; task moves to `awaiting_plan_approval` |
| implement | `pipeline.run_implement` | `MockAgent` diff captured and hashed; task moves to `validating` |
| validate | `pipeline.run_validation` | every check recorded `skipped` (no container runtime); task moves to `failed_validation` |
| exception | `storage.record_validation_exception` | a documented exception bound to the exact patch hash |
| approve | `approval.apply_action` | approval bound to the patch hash; task moves to `awaiting_publish_approval` |
| publish (dry run) | `publisher.publish` | full plan and PR body built, attempt recorded, **no write** |
| safety | `publisher.plan_publish` | a non-allowlisted repository is refused |

The final line the demo prints is `Write requests attempted: 0`. The GitHub
clients are in-process fakes that record every request, so "no write" is
*counted*, not asserted by comment.

## What is substituted, and why

Two things are fakes, and both are named in the output:

- The coding agent is `MockAgent`, which produces a fixed unified diff. There is
  no model key in this environment, and a real agent call would be a network
  call the demo must not make.
- The GitHub clients are `RecordingHTTP` and `FakeReadClient`. They answer the
  reads the pre-publish checks need (branch state, permission scope, issue
  existence) and record every write, so the dry run can be proven.

Everything else — the state machine, the patch hashing, the approval binding, the
validation records, the publish checks, the PR body — is the real code.

## What it deliberately does not claim

- **No benchmark performance.** The patch is a docs-only change; no test is run
  and none could be.
- **No passing validation.** With no container runtime, every check is recorded
  `skipped`, never `passed`. The publish is authorized by a *recorded exception*,
  and the PR body says so in those words rather than implying a pass.
- **No real publish.** The task ends at `awaiting_publish_approval`. The demo
  records a dry run and stops.

## Running it against a real dry run

To see the same dry run through the CLI against the stored database, use
`forgeflow publish` **without** `--confirm-real-publish`. The demo here is
self-contained and leaves nothing behind: it uses an in-memory database and a
temporary workspace directory that is removed when it finishes.

## Tests

`tests/test_demo.py` asserts the stage sequence, that zero write requests are
made, that a dry run (not a publish) is recorded, that the third-party repository
is refused, and that the PR body states the exception rather than a pass.
