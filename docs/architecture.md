# Architecture

ForgeFlow is a local-first Python tool. It discovers open issues, ranks them,
analyses the strongest candidates, implements a patch in isolation, validates it,
and — only behind an explicit human approval and a separate credential — opens a
draft pull request. Everything is recorded in SQLite, and every step that could
write to GitHub is gated.

This document shows the system as it is built, not as it is hoped to be. Where a
capability is refused in this environment (for example, running untrusted code
with no container runtime), the diagram says so.

## System overview

```mermaid
flowchart TD
    subgraph local["Local process (no server, no daemon)"]
        CLI["forgeflow CLI<br/>(Typer)"] --> DISC["discovery.py<br/>read-only GitHub search"]
        CLI --> RANK["ranking.py<br/>six factors + capped<br/>outcome adjustment"]
        CLI --> CTX["repository_context.py<br/>shallow clone, size-limited"]
        CLI --> ANA["issue_analyzer.py<br/>LLM adapter, validated JSON"]
        CLI --> PIPE["pipeline.py<br/>orchestration"]
        CLI --> VAL["validation.py<br/>tests / lint / types"]
        CLI --> APPROVE["approval.py<br/>human boundary"]
        CLI --> PUB["publisher.py<br/>gated draft PR"]
        CLI --> TRACK["outcome_tracker.py<br/>read-only PR state"]
        CLI --> SCHED["scheduler.py<br/>discovery only"]
    end

    DISC --> GH[(GitHub API)]
    CTX --> GH
    TRACK --> GH
    PUB -- "writes (draft PR only)" --> GH

    DISC --> DB[(SQLite<br/>data/forgeflow.db)]
    RANK --> DB
    ANA --> DB
    VAL --> DB
    APPROVE --> DB
    PUB --> DB
    TRACK --> DB
    SCHED --> DB

    ANA -. "needs GEMINI_API_KEY<br/>(absent here: fails closed)" .-> LLM[["Model provider"]]
    PIPE -. "coding agent" .-> AGENT[["Jules / Mock agent"]]
    PIPE -. "needs a container runtime<br/>(absent here: refuses)" .-> SBX[["sandbox.py"]] 

    CRED["GITHUB_TOKEN<br/>(read-only)"] -.-> DISC
    CRED -.-> TRACK
    WCRED["FORGEFLOW_PUBLISH_TOKEN<br/>(separate, never falls back)"] -.-> PUB

    KS{{"kill switch +<br/>budget check"}} -. "checked before every<br/>paid call and every write" .-> CLI
```

The read path and the write path use different credentials on purpose. Discovery,
context, analysis, and outcome tracking use the read-only `GITHUB_TOKEN`;
publishing is the only thing that uses `FORGEFLOW_PUBLISH_TOKEN`, and that token
never falls back to `GITHUB_TOKEN` (a test asserts it).

## Task state machine

SPEC section 10 names the states. Transitions are declared in
`models.LEGAL_TRANSITIONS` and enforced by `storage.transition_task_state`, so an
illegal jump raises rather than silently moving a task.

```mermaid
stateDiagram-v2
    [*] --> discovered
    discovered --> shortlisted
    discovered --> discarded
    discovered --> closed
    shortlisted --> analyzed
    shortlisted --> discarded
    shortlisted --> closed
    analyzed --> awaiting_plan_approval
    analyzed --> discarded
    analyzed --> closed
    awaiting_plan_approval --> implementing
    awaiting_plan_approval --> discarded
    awaiting_plan_approval --> closed
    implementing --> validating
    implementing --> failed_validation
    implementing --> discarded
    validating --> awaiting_publish_approval
    validating --> failed_validation
    validating --> implementing : reviewer requests another attempt
    validating --> discarded
    failed_validation --> implementing : repair attempt
    failed_validation --> awaiting_publish_approval : documented exception
    failed_validation --> discarded
    awaiting_publish_approval --> published
    awaiting_publish_approval --> discarded
    published --> merged
    published --> closed
    merged --> [*]
    closed --> [*]
    discarded --> [*]
```

Terminal states are `merged`, `closed`, and `discarded`. Every non-terminal state
can be discarded or closed, because a human may abandon a task at any point.

## Approval and publishing flow

This is the guarded part. Publishing is the only external write, and it happens
only after a recorded approval bound to the SHA-256 of the exact diff bytes.

```mermaid
flowchart TD
    START["forgeflow publish &lt;ref&gt;"] --> KS{"kill switch<br/>engaged?"}
    KS -- yes --> REFUSE1["refuse: no model calls,<br/>no write"]
    KS -- no --> BUDGET{"budget available?"}
    BUDGET -- no --> REFUSE2["refuse: over the cap"]
    BUDGET -- yes --> MODE{"--confirm-real-publish?"}

    MODE -- no --> DRYRUN["dry run:<br/>plan_publish, print, no write"]
    MODE -- yes --> PLAN["plan_publish:<br/>ordered pre-publish checks"]

    PLAN --> C1{"repo on<br/>allowlist?"}
    C1 -- no --> R1["refuse"]
    C1 -- yes --> C2{"task in<br/>awaiting_publish_approval?"}
    C2 -- no --> R2["refuse"]
    C2 -- yes --> C3{"approval recorded<br/>and hash matches<br/>this exact diff?"}
    C3 -- no --> R3["refuse: approval does<br/>not bind this patch"]
    C3 -- yes --> C4{"validation passed, or a<br/>documented exception<br/>covers this hash?"}
    C4 -- no --> R4["refuse: validation failed"]
    C4 -- yes --> TOK{"FORGEFLOW_PUBLISH_TOKEN<br/>present?"}
    TOK -- no --> R5["fail closed, one clean line,<br/>never a traceback"]
    TOK -- yes --> WRITE["push branch (force refused),<br/>open DRAFT PR,<br/>verify issue with read client"]
    WRITE --> RECORD["record publication +<br/>audit event"]
```

Rules the flow enforces:

- **No force flag.** If the approval no longer matches the patch hash, publishing
  refuses; there is no override.
- **Draft only.** ForgeFlow never auto-merges and never force-pushes.
- **Issue verification uses the read client.** Only a verified issue lets the PR
  body claim `Fixes #N`; otherwise the reference is labelled unverified.
- **Missing token fails closed** with one clean line, and never falls back to
  `GITHUB_TOKEN`.

## Data model

SQLite, one file at `data/forgeflow.db` (gitignored). Foreign keys are enforced.
The Phase 3 tables live in their own modules so each owns its schema, but they
join on `(repo_full_name, issue_number)`.

```mermaid
erDiagram
    repositories ||--o{ issues : "has"
    issues ||--o| issue_scores : "scored by"
    issues ||--o{ patches : "patched by"
    issues ||--o{ analyses : "analysed by"
    issues ||--o{ validation_results : "validated by"
    issues ||--o{ validation_exceptions : "overridden by"
    issues ||--o{ publications : "published by"
    issues ||--o{ approvals : "approved by"
    issues ||--o{ pull_request_outcomes : "tracked by"
    issues ||--o{ outcome_metrics : "measured by"
    pull_request_outcomes ||--o{ pr_reviews : "reviewed by"

    repositories {
        text full_name PK
        int stars
        int has_tests
    }
    issues {
        text repo_full_name PK
        int number PK
        text title
        text state
        text task_state
    }
    issue_scores {
        text repo_full_name PK
        int issue_number PK
        real skill_relevance
        real duplicate_conflict_risk
    }
    patches {
        int id PK
        text patch_sha256
        text diff
        int additions
        int deletions
    }
    validation_results {
        int id PK
        text patch_sha256
        text kind
        text status
    }
    approvals {
        int id PK
        text patch_sha256
        text actor
        text reason
    }
    publications {
        int id PK
        text mode
        int published
        int draft
        int pr_number
    }
    pull_request_outcomes {
        int id PK
        int pr_number
        text status
        int merged
        int approvals
        int requested_changes
    }
    outcome_metrics {
        int id PK
        real patch_usefulness
        real test_quality
        real complexity
        text maintainer_feedback
    }
```

Side tables not shown for space: `runs`, `excluded_issues`, `status_changes`,
`llm_usage`, `audit_log`, `kill_switch`, `task_guard`, `schedule_state`.

## Module responsibilities

| Module | Responsibility | Writes to GitHub? |
| --- | --- | --- |
| `github_client.py` | The only module that performs GitHub HTTP; read-only | no |
| `discovery.py` | Repository metadata and issue search | no |
| `dedupe.py` | Identity dedupe and eligibility filtering, with reasons | no |
| `ranking.py` | Six weighted factors plus a capped outcome adjustment | no |
| `repository_context.py` | Shallow clone and size-limited context extraction | no |
| `prompts.py` | Prompt construction and the injection-defence boundary | no |
| `agent_runner.py` | `CodingAgent` interface, `JulesAgent`, `MockAgent` | no |
| `issue_analyzer.py` | LLM adapter, validated output, cost cap, retries | no |
| `sandbox.py` | Container isolation, or an explicit refusal | no |
| `validation.py` | Runs the target repo's tests/lint/types | no |
| `approval.py` | Approve / request-changes / reject / discard boundary | no |
| `publisher.py` | Pre-publish checks, gated branch push, draft PR | **yes (draft only)** |
| `publish_token.py` | The publishing credential; no fallback | no |
| `diff_apply.py` | Strict unified-diff applier for the branch push | no |
| `outcome_tracker.py` | Tracks PR state; aggregates outcomes per repo | no |
| `scheduler.py` | Optional discovery-only cycle | no |
| `audit.py` | Append-only audit log | no |
| `killswitch.py` | Persistent kill switch and per-task guardrails | no |
| `security.py` | Secret scrubbing for recorded output | no |

## What is not built, and why

- **No container runtime here.** `/usr/bin/docker` exists but the daemon socket
  does not, and `podman` is absent. `sandbox.py` therefore refuses to execute
  repository code. There is deliberately **no** host-execution fallback.
- **No model keys here.** `JULES_API_KEY` and `GEMINI_API_KEY` are absent, so the
  analysis and coding-agent steps fail closed rather than pretend.
- **No background service.** The scheduler performs at most one cycle when a
  caller invokes it and the interval has elapsed; there is no daemon or timer
  thread.
