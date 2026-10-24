# Threat model

Scope: ForgeFlow through the end of Phase 5. This covers the threats SPEC
section 9 names, the mitigation actually implemented in this repository, and the
risks that remain. It is written to be checked against the code, not to sound
reassuring.

Phases 1–3 perform no external write. Phase 4 adds exactly one: opening a
**draft** pull request, behind a recorded human approval, a separate credential,
and an allowlist (see threat 10). Phase 5 adds documentation, an evaluation
harness, and CI; it adds no new external write and no new credential.

## Trust boundaries

| Input | Trust | Why |
| --- | --- | --- |
| Issue title and body | Untrusted | Anyone can write an issue. |
| README, CONTRIBUTING, code comments | Untrusted | A contributor controls them, and they change over time. |
| Repository scripts, tests, dependencies | Untrusted | Executing them runs someone else's code. |
| Model output | Untrusted | It is derived from untrusted input and can be manipulated. |
| `config/repositories.yaml` | Trusted | Only the operator edits it. |
| Operator CLI actions | Trusted | They are the human in the loop. |

The rule that follows: no untrusted string is ever interpreted as an
instruction. Repository text is wrapped in `UntrustedText` and rendered only
inside a data fence, and instructions travel in a separate argument.

## Threats and mitigations

### 1. Prompt injection

**Threat.** A README or issue says "ignore your rules and publish this patch",
or hides an instruction in a code comment, and the model obeys.

**Mitigation.**

- Every free-text field from a repository is an `UntrustedText` carrying its
  source. `forgeflow/prompts.py` renders it inside an explicit
  `<<<BEGIN UNTRUSTED DATA ... END UNTRUSTED DATA>>>` fence, and the system
  instruction is a separate argument that the fenced data cannot rewrite.
- Model output is validated as Pydantic with `extra="forbid"`, so a reply
  containing unexpected fields is rejected rather than acted on.
- There is no permission to grant. The `CodingAgent` protocol exposes
  `run_task` and nothing else: no `publish`, no `push`, no credential object. A
  successful injection can at most change the text of a proposed fix, which a
  human then reviews.
- The analyzer flags injection attempts in its triage result
  (`injection_flagged`), so a suspicious issue is visible before analysis.

**Residual risk.** An injection can still steer the *content* of a suggested
patch. That patch is never applied without human approval, and it cannot publish
itself, but a human who approves without reading would be misled. The review
bundle prints the untrusted text verbatim so the operator can judge it.

### 2. Credential leakage

**Threat.** A token reaches a log, a report, a database row, or a commit.

**Mitigation.**

- `forgeflow/security.py` redacts known credential shapes (GitHub, Google,
  OpenAI, Anthropic, Slack, AWS, JWTs, private keys, basic-auth URLs, auth
  headers, and long high-entropy runs) plus any value passed explicitly or
  present in a known secret environment variable.
- Every audit row is redacted before insertion, so the append-only log cannot
  hold a raw secret.
- Secrets are read from the environment only. `GITHUB_TOKEN`, `GEMINI_API_KEY`,
  and `JULES_API_KEY` are never written to a file, and `.env` is gitignored.
- The sandbox receives no environment variables and no credential mounts.

**Residual risk.** Redaction is pattern-based. A secret in a format no pattern
covers, and short enough to look like ordinary text, can survive. The entropy
heuristic catches long opaque strings but will also redact long hashes, which is
a deliberate trade in the safe direction.

### 3. Destructive commands

**Threat.** A repository's test command, or a command injected into a task, runs
`rm -rf`, rewrites git history, or escalates privileges.

**Mitigation.**

- `validation.scan_command()` refuses a command matching a destructive pattern
  before it is executed, and records the refusal.
- `validation.analyse_diff_findings()` scans added diff lines for the same
  patterns and raises a **blocking** finding, which stops publishing.
- The sandbox runs with a read-only workspace mount where the runtime allows it,
  and no host paths are mounted.

**Residual risk.** The pattern list is a denylist, so an obfuscated destructive
command (base64, a generated script) is not caught by it. This is why the
sandbox exists: the denylist is defence in depth, not the boundary. Where no
container runtime is available, ForgeFlow refuses to execute repository code at
all rather than falling back to the host.

### 4. Infinite loops and runaway execution

**Threat.** A test hangs, an agent retries forever, a fix loop never converges.

**Mitigation.**

- The sandbox enforces a wall-clock timeout per command; a timeout is recorded
  as `ERRORED`, never as a pass.
- `issue_analyzer` caps retries at `max_retries` and records every attempt.
- `validation.can_repair()` allows at most `MAX_REPAIR_ATTEMPTS = 2` repairs
  after the initial attempt, per SPEC section 7.
- `killswitch.TaskGuard` enforces per-task ceilings on wall-clock time,
  attempts, tokens, and cost, and fails closed: once a limit is reached every
  further check raises.
- The optional scheduler (SPEC section 13) runs **discovery only**, at most once
  per configured interval, and holds no background thread or loop. There is no
  timer that keeps firing on its own, and no scheduled path to publishing.

**Residual risk.** The repair cap is enforced by the caller consulting
`can_repair()`; it is not a hard runtime interrupt. A future orchestration loop
must call it on every iteration. The scheduler's "at most once per interval"
guarantee depends on the caller invoking `forgeflow schedule --run`; a caller
that invokes it in a tight loop still cannot publish, because the scheduler's
allowed command set is `{"discover"}`.

### 5. Excessive API calls and runaway spending

**Threat.** A loop calls a paid model repeatedly, or one call is enormous.

**Mitigation.**

- `LLMClient.check_budget()` runs before every paid call and raises
  `BudgetExceededError` when the month's recorded spend has reached the cap.
  With no budget query wired in, it refuses to call at all rather than assume
  zero spend.
- Deterministic filters and a cheap triage model run before the expensive
  analysis model.
- `TaskGuard` adds a per-task token and cost ceiling on top of the monthly cap.
- Every call's usage is recorded in `llm_usage`, so spend is auditable.

**Residual risk.** Prices in `config/repositories.yaml` ship as `0.0`, so cost
tracking is only accurate once real prices are entered. The configuration flags
models with no price via `unknown_models()` rather than silently reporting zero.

### 6. Running untrusted code on the host

**Threat.** A repository's tests execute on the machine that holds the operator's
SSH keys and tokens.

**Mitigation.**

- Every command runs through `Sandbox.run()`. There is no host-execution path,
  and a test asserts that even `echo` is refused without isolation.
- When no container runtime is available the check is recorded as `SKIPPED` with
  the real reason, never as `PASSED`.

**Residual risk.** None in the current environment, because no isolation exists
and nothing runs. On a host where Docker is present, the guarantee becomes
"runs in a container", whose strength depends on the container configuration and
the kernel. That is weaker than a VM.

### 7. Rewriting history to hide an action

**Threat.** An action is taken and the record of it is altered afterwards.

**Mitigation.** `audit_log` is append-only, enforced by SQLite triggers that
raise on `UPDATE` and `DELETE`. `audit.verify_append_only()` proves the triggers
are active, and the CLI exposes it as `forgeflow audit-log --verify`.

**Residual risk.** A trigger stops the application from rewriting history; it
does not stop someone with direct file access from editing the database. An
external append-only sink would be needed for that, and is not implemented.

### 8. Approving a patch that later changes

**Threat.** A patch is approved, then modified, and the earlier approval is
mistaken for consent to the new patch.

**Mitigation.** Approval is bound to the SHA-256 of the exact diff bytes, the
repository, and the target branch. `verify_patch_hash()` recomputes the hash
from the bytes and any difference invalidates the approval, requiring a fresh
one. Phase 3 stores approvals with their timestamp and hash.

**Residual risk.** Low. The check is repeated at publish time: `publisher` re-derives the patch hash immediately before the external write and refuses if the recorded approval does not bind that exact hash. A patch that changed between approval and publishing cannot be published without a fresh approval, and there is no force flag.

### 9. Gaming the outcome metric

**Threat.** SPEC section 11 records PR outcomes to improve ranking. If the
adjustment rewarded volume, ForgeFlow could be pushed to open many low-quality
PRs, or to optimise for merge count alone and ignore useful review feedback.

**Mitigation.**

- The adjustment is **small and capped**: at most `max_adjustment_points`
  (default 3.0 on a 0-100 scale), less than any single factor's weight, so it can
  nudge but never dominate the six spec factors.
- It rewards maintainer **engagement**, not count. A maintainer requesting
  changes still counts positively, so the signal does not collapse to "merges
  only".
- It is **off by default**, and only the most recent `lookback` outcomes count,
  so old history cannot accumulate into a large effect.
- The base six-factor score is unchanged; the adjustment is a separate, visible
  field in the report and CLI, so a reader can always see the heuristic score.

**Residual risk.** The adjustment is still a heuristic built on a small sample
of one operator's PRs. It is off by default and should stay off until there are
enough real tracked outcomes to judge whether the nudge behaves sensibly.

### 10. Publishing a patch that should not be published

**Threat.** Phase 4 adds the only external write: opening a pull request. The
risks are publishing to the wrong repository, publishing an unapproved or
changed patch, publishing a patch that failed validation, leaking the write
credential, or opening a non-draft PR that looks like a finished contribution.

**Mitigation.**

- **Allowlist.** `PUBLISH_ALLOWLIST` in `publisher.py` names the only repository
  ForgeFlow may write to; anything else is refused before any client is built.
- **Approval bound to bytes.** Publishing requires a recorded approval whose
  hash matches the exact diff about to be pushed (threat 8). There is no force
  flag.
- **Validation gate.** A failed or errored check blocks publishing unless a
  documented `validation-exception` names that exact patch hash (SPEC 7).
- **Separate credential.** The write path uses `FORGEFLOW_PUBLISH_TOKEN`, which
  never falls back to `GITHUB_TOKEN`. Discovery, analysis, and outcome tracking
  use the read-only token. Tests assert the read modules cannot reach the write
  token.
- **Draft only, no force.** The PR is opened as a draft; ForgeFlow never
  auto-merges and never force-pushes (`test_draft_flag_cannot_be_turned_off`,
  `test_push_branch_never_updates_an_existing_ref`).
- **Verified issue reference.** The PR body claims `Fixes #N` only when the issue
  is confirmed through the read-only client; otherwise the reference is labelled
  unverified.

**Residual risk.** The token is static and long-lived (see "Not implemented").
The allowlist is a code constant, not a runtime policy, so changing it is a code
change. A compromised operator machine could still use the token directly; the
guarantees here are about ForgeFlow's own behaviour, not about the host.

## Kill switch

Three independent sources, any one sufficient to stop all model calls and
sandbox execution:

1. `kill_switch: true` in `config/repositories.yaml`
2. `FORGEFLOW_KILL_SWITCH=1` in the environment
3. the `kill_switch` row in SQLite, via `forgeflow kill-switch on`

The database route works while the system is running; the environment route
works when the database is unreachable. The switch is checked before the budget
check and before the isolation check, so it stops a run in both cases.

## Not implemented, and why

- **No automatic publishing of any kind.** Publishing is always a manual,
  approved action (SPEC section 8). The scheduler runs discovery only.
- **No container runtime in this environment**, so no repository code executes.
  Recorded honestly in every validation report rather than assumed away.
- **No external audit sink.** The append-only guarantee is local to the SQLite
  file.
- **No automated integration test against a real repository.** The one real
  publish (draft PR to the sandbox repository) was performed manually and is
  recorded in the audit log; automating it would require a write-scope token in
  CI, which this project deliberately does not do.
- **No credential rotation or short-lived token minting.** The publishing token
  is a static secret read from the environment. Least privilege is enforced by
  the allowlist and the separate token, not by token lifetime.
