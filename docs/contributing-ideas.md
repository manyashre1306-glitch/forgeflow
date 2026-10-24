# Contribution ideas

This is a menu of concrete, bounded tasks. Each one names the files involved and
the test that would prove it. Nothing here requires a credential, a container
runtime, or network access, so every item can be done and verified offline.

If you want to work on one, open an issue first and say which item, so two people
do not do the same thing.

## Good first issues

### 1. Add a fixture case to the evaluation harness

**Files:** `evaluation/`, `tests/test_evaluation.py`

The harness runs a small set of deterministic patch-application cases and prints
the SPEC section 11 metrics. Add another case that exercises a path the current
fixtures miss (for example, a patch that renames a file). The metric code must
still report "not enough data" for a metric with no denominator, so add a test
that checks that too.

### 2. Test a ranking factor that is only asserted in a comment

**Files:** `src/forgeflow/ranking.py`, `tests/test_ranking.py`

The six factors each have a weight and a reason string. Pick one and add a test
that fixes its behaviour at the boundary: a value that should score zero, a value
that should score the maximum, and one in between. Assert the reason string is
non-empty.

### 3. Document a module that has no module docstring

**Files:** anywhere under `src/forgeflow/`

Run `uv run python -m ruff check .` and `grep -L '"""' src/forgeflow/*.py`. Pick a
module without a clear module docstring and write one that says what it does and
what it deliberately does not do. No code change; keep the tests green.

## Medium

### 4. Add an outcome-tracker metric with an honest denominator

**Files:** `src/forgeflow/outcome_tracker.py`, `tests/test_outcome_tracker.py`

`metrics_report()` returns every key in `SPEC_METRICS`. Add a new metric that
needs a denominator (for example, median time from open to first review). It must
print "not enough data" when the denominator is zero, never `0.0`. `time_saved_hours`
is the model to follow: it is `0.0` on purpose because it is not measured.

### 5. Strengthen the injection-defence test

**Files:** `tests/test_security_phase3.py`, `src/forgeflow/prompts.py`

Add a test that a hostile string in an issue title cannot escape its data fence.
The assertion should check the rendered prompt, not a helper. See the existing
`UntrustedText` tests for the shape.

### 6. A website page for the evaluation harness

**Files:** `web/views/`, `web/service.py`, `tests/test_web_service.py`

Add a page that runs `evaluation/` in-process and shows the metrics table, with
the "not enough data" cells visible rather than hidden. Keep the computation in
`web/service.py` and the drawing in the view, so the logic is testable without
Streamlit.

## Larger

### 7. A provider behind the existing `CodingAgent` adapter

**Files:** `src/forgeflow/agent_runner.py`, `tests/test_agent_runner.py`

Add a second coding-agent provider that implements the `CodingAgent` protocol
(`run_task` and nothing else). The protocol is deliberately narrow: there is no
`publish`, `push`, or token method for a prompt injection to reach. Add a test
that the new provider has no attribute containing `publish`, `push`, or `token`.

### 8. Make the sandbox work without a container runtime, safely

**Files:** `src/forgeflow/sandbox.py`, `tests/test_sandbox.py`

This is the hard one, and the current answer is "do not". The environment has no
container runtime, and `sandbox.py` refuses rather than executing repository code
on the host. A real solution would need an isolation primitive that does not
require a daemon (for example, a syscall filter or a separate user namespace). If
you propose one, the test must show that repository code still cannot read the
filesystem outside its workspace, cannot reach the network, and cannot outlive its
time limit. Do not add a host-execution fallback to make a test pass.

### 9. An append-only audit sink

**Files:** `src/forgeflow/audit.py`, `tests/test_audit.py`

The append-only guarantee is currently local to the SQLite file: anyone who can
write the file can rewrite history. Add a sink that appends to a second,
independent store and verify that tampering with the SQLite file is detectable.

## What not to work on

- **A host-execution fallback for the sandbox.** It is a deliberate omission. See
  `docs/threat-model.md`.
- **Auto-merge or force-push.** ForgeFlow opens draft PRs and stops. The human
  boundary is the point.
- **A headline acceptance rate over the one synthetic publish.** One data point is
  not a rate. See the Contributions section of the README.
- **Filling the prices in `config/repositories.yaml` from memory.** They are `0.0`
  on purpose until they are filled from vendor pricing. See `docs/cost-controls.md`.
