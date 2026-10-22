# Phase 2 implementation notes and limitations

This file records what Phase 2 actually does, what it deliberately refuses to
do, and what could not be tested here. Nothing below is aspirational: every
statement was checked by running the code in this environment on 2026-10-22.

---

## 1. The honest summary

Phase 2 delivers the full machinery for repository context, agent driving, issue
analysis, isolation, patch capture, and local review reports. It **cannot run a
real coding agent end-to-end in this environment**, for two independent reasons:

1. No `JULES_API_KEY` is present, and one cannot be created without an
   interactive browser session on a Google account with a connected repository.
2. No container runtime is available, so there is nowhere safe to execute
   untrusted repository code.

Neither is worked around by pretending. The first fails closed with a named
remedy; the second refuses outright.

---

## 2. Container isolation: measured state

`uv run python -c "from forgeflow.sandbox import build_sandbox; print(build_sandbox().explain())"` prints, verbatim:

```
No container isolation is available, so ForgeFlow will NOT execute repository code.
Reason: No usable container runtime: refusing to execute untrusted repository code on the host.
  docker: failed to connect to the docker API at unix:///var/run/docker.sock; check if the path is correct and if the daemon is running: dial unix /var/run/docker.sock: connect: no such file or directory
  podman: podman is not installed
Remedy: start a Docker daemon (or install podman) and re-run. Until then, only MockAgent results are produced, and no repository code is run.
```

The Docker **client** is installed at `/usr/bin/docker`. The daemon is not
running: `/var/run/docker.sock` does not exist. `DOCKER_HOST` is unset, and
`docker context ls` shows only the default context pointing at that missing
socket. `podman` is absent.

The distinction matters. `shutil.which("docker")` returns a path, so a naive
check would conclude "Docker is available" and then run untrusted code with no
isolation at all. `detect_backend` therefore contacts the daemon (`docker info`)
rather than trusting the binary's presence.

### Why the fallback is refusal, not best effort

Implementing an issue means executing code from a repository ForgeFlow does not
control: its test suite, its build scripts, its `conftest.py` imports. Any of
those can be hostile. Running them directly on the host would hand that code the
operator's files, credentials, and network. There is no safe partial version of
this, so `Sandbox.run` raises `IsolationError` and there is **no code path that
falls back to the host**. A test asserts that a harmless command (`echo`) is
refused too, so the refusal cannot depend on judging whether a command looks
dangerous.

### What the limits do when a container *is* available

`build_container_command` produces a run with: `--rm`, the workspace mounted at
`/workspace` and nothing else from the host, `--cpus`, `--memory`,
`--pids-limit`, `--network none` by default, and `--read-only` root with a small
writable `/tmp`. No `--env` or `--env-file` is ever passed, so no credential
reaches the container. These are asserted in `tests/test_sandbox.py` without
needing a daemon.

---

## 3. Coding agent: Jules

The API was verified from official documentation, not assumed. See
`docs/agent-evaluation.md` for the sourced detail. In short:

- The API exists and is documented, at `https://jules.googleapis.com/v1alpha`,
  authenticated with an `X-Goog-Api-Key` header.
- Activities return a `changeSet` artifact whose `gitPatch.unidiffPatch` is the
  change in unified diff format, plus `bashOutput` with `command`, `output`, and
  `exitCode`. That is exactly what the patch hash and review report need.
- `automationMode: "AUTO_CREATE_PR"` would make Jules open a pull request. That
  field is **never sent**, and a test asserts it is absent from the request body,
  because it is the single property that keeps Phase 2 read-only.

**Blocker:** no `JULES_API_KEY`. `JulesAgent.is_available()` returns `False` and
`run_task` raises `AgentUnavailableError` *before* making any request, naming the
remedy. It does not fail later with a confusing network error.

**What was tested without a key:** the request construction, the activity
parsing (diff, commands, terminal states, malformed input), the timeout path, and
the HTTP error path. The HTTP layer was replaced by a fake replaying the
documented response shapes. The `run_task` control flow is therefore exercised
for real; only the actual network call is not.

---

## 4. Issue analysis

The analyzer is provider-swappable behind `LLMBackend`. The Gemini backend is
implemented against the documented `generateContent` endpoint and reads
`GEMINI_API_KEY` from the environment only.

**Blocker:** no `GEMINI_API_KEY`. `GeminiBackend.is_available()` returns `False`,
and calling `complete` raises `ModelUnavailableError` without retrying, since a
missing key is a configuration fault rather than a transient error.

**What was tested without a key:** everything except the HTTP call itself. Triage
and analysis both run against `FakeBackend`, covering validated output, cost
recording, the spending cap, retry bounds, and prompt construction.

Cost control specifics worth knowing:

- `check_budget()` runs **before** every request. A test asserts that when the
  cap is already reached, zero requests are made — the check is not a
  post-hoc accounting step.
- Failed calls are recorded and counted toward spend. Under-reporting spend is
  the unsafe direction for a cap.
- If no budget query is wired in, the analyzer **refuses to call a paid model**
  rather than treating unknown spend as zero.
- Prices live in `config/repositories.yaml`, not in code, because vendor pricing
  changes and a stale constant would silently corrupt every figure. A model with
  no configured price is reported by `unknown_models()` so the gap is visible.

---

## 5. Prompt-injection defence

The defence is structural, and its limits are stated plainly.

What it does:

- Instructions and data travel in **separate arguments**. Repository text is
  rendered only inside `UntrustedText` fences that name their source.
- The system prompts state that fenced content is data, that it cannot grant
  permissions or change scope, and that attempts to do so should be reported in
  the `risks` field.
- Tests assert that hostile text never appears in the instruction section of a
  prompt, that the fence markers surround it, and that no API key ever enters a
  prompt.
- Model replies are parsed into Pydantic objects with `extra="forbid"`, so a
  reply that invents fields fails validation.

What it does **not** do, stated honestly:

- It cannot make injection impossible. A sufficiently capable model may still be
  influenced by text inside a fence. The defence removes the failure mode where
  repository prose *is* an instruction by construction; what remains is a
  model-behaviour question.
- Because Phase 2 performs no external writes and no privileged action, the
  worst realistic outcome of a successful injection is a misleading analysis
  that a human then reviews. The blast radius is bounded by the approval gate,
  not by the prompt.

---

## 6. What could not be tested, and why

| Item | Why not tested |
| --- | --- |
| Real Jules session (create, poll, retrieve diff) | No `JULES_API_KEY`; keys are minted only in the Jules web app. |
| Real Gemini API call | No `GEMINI_API_KEY`. |
| Real container execution of repository code | No Docker daemon; `podman` absent. |
| Sandbox limits enforced at runtime by a kernel | No container to enforce them. The generated flags are asserted instead. |
| `git clone` against a real GitHub remote | Tests use a local fixture repository over `file://`, so no network is touched. The clone code path itself is real. |
| End-to-end patch from a real agent | Blocked by both the missing key and the missing container. |

Everything in that table is blocked by an absent credential or an absent
runtime, not by unimplemented code. The surrounding machinery is implemented and
covered by tests; what is missing is the environment to point it at.

---

## 7. A note on scope

Phase 2 does not publish anything. No pull request, comment, branch, issue edit,
or push is performed by any code path added in this phase. The `CodingAgent`
interface has no publish method, `automationMode` is never sent to Jules, and the
sandbox refuses to run at all. Publication remains Phase 4 work behind the
approval gate.

---

## 8. End-of-phase verification (real output)

Run against SPEC section 13 (Phase 2 deliverables) and section 14 (testing
requirements). Output reproduced verbatim.

```
=== SPEC 13: Phase 2 deliverables ===
  repository context extraction : PASS  repository_context.py
  agent-provider adapter        : PASS  agent_runner.py (CodingAgent, JulesAgent, MockAgent)
  issue analysis                : PASS  issue_analyzer.py
  isolated workspaces           : PASS  sandbox.py (refuses without a runtime)
  patch generation              : PASS  patch.py
  local review reports          : PASS  review.py

=== SPEC 14: required tests, verified by behaviour ===
  patch-hash validation         : PASS
  modified patch invalidates    : PASS
  cost limits configured        : PASS
  missing JULES key fails safe  : PASS
  missing GEMINI key fails safe : PASS
  injection text stays fenced   : PASS
  agent has no publish method   : PASS
  no isolation -> refusal       : PASS (isolated=False)

=== no external write actions in Phase 2 code ===
  grep for write actions        : 0 match(es)
```

The write-action grep covers `requests.post/put/patch/delete`, `urlopen` with a
write method, `git push`, `git commit`, `create_pull_request`, and a literal
`automationMode` key. It returns zero matches across `src/forgeflow/`.

Gate results at the end of the phase:

```
$ uv run python -m ruff check .
All checks passed!

$ uv run python -m ruff format --check .
41 files already formatted

$ uv run python -m pytest -q
469 passed in 5.12s
```

### Test growth across the phase

| Step | Commit | Tests |
| --- | --- | --- |
| 2.1 repository context | `0ba0202` | 304 |
| 2.2 agent evaluation | `f02bf90` | 304 |
| 2.3 agent adapter | `f6b68c1` | 336 |
| 2.4 issue analysis | `b5e4c84` | 388 |
| 2.5 sandbox | `a195ddc` | 419 |
| 2.6 patch + review + CLI | `3a3c9d2` | 469 |

### What was verified by running the CLI, not just unit tests

Against a local fixture repository at `/tmp`, with no network:

- `context` extracted the contribution rules and reported
  `AI policy: unknown`, `requires tests: True`, `requires signoff: True`, and
  moved the task to `shortlisted`.
- `analyze` failed closed with
  `model unavailable: GEMINI_API_KEY is not set; refusing to call the model API.`
  and exit code 1.
- `implement --provider mock` warned that no isolation was available, captured a
  patch (`files: 1`, `+4 -1`), printed the SHA-256, and moved the task to
  `validating`.
- `review` wrote a bundle whose diff section contains the full patch and whose
  header states plainly that the patch was not executed or tested.

Both the `analyze` refusal and the `implement` warning are real output from this
environment, not descriptions of intended behaviour.

