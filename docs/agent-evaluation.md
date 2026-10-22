# Coding-agent evaluation for ForgeFlow Phase 2

This document records what ForgeFlow can and cannot automate. Every factual claim
below was read from the vendor's own documentation on **2026-10-22**; the source
URL is given inline. Nothing here is inferred from a blog summary, a third-party
tutorial, or an assumption that an API "probably" exists.

Two constraints shape the whole evaluation:

1. **No external write actions.** ForgeFlow must not create pull requests,
   comments, branches, or issues during Phase 2. Any provider feature that
   publishes automatically is therefore unusable, not merely undesirable.
2. **No usable Docker daemon.** The Docker *client* is installed at
   `/usr/bin/docker`, but the daemon is not running. `docker info` exits 1 with:

   ```
   failed to connect to the docker API at unix:///var/run/docker.sock;
   check if the path is correct and if the daemon is running:
   dial unix /var/run/docker.sock: connect: no such file or directory
   ```

   `docker context ls` shows only the default context pointed at
   `unix:///var/run/docker.sock`, `DOCKER_HOST` is unset, and `podman` is
   absent. So no container isolation of any kind is available, and any provider
   needing local isolation cannot be exercised here.

---

## 1. Google Jules

### 1.1 Verified facts

**What it is.** Jules is Google's asynchronous coding agent. It connects to
GitHub, clones the target repository into a Google Cloud VM, produces a plan,
executes it, and can open a pull request for review.
Source: <https://jules.google/docs>

**The API exists, and is documented.** This was the central question, and the
answer is yes. It is explicitly **alpha / experimental**:

> "The Jules API is in an alpha release, which means it is experimental. Be aware
> that we may change specifications, API keys, and definitions as we work toward
> stabilization."

Source: <https://developers.google.com/jules/api>

**Base URL and version:** `https://jules.googleapis.com/v1alpha`
Source: <https://jules.google/docs/api/reference/overview>

**Authentication:** a Jules API key sent in the `X-Goog-Api-Key` header. Keys
are generated in the Jules web app at `jules.google.com/settings`. The docs note
a maximum of three active keys per user, and that keys are auto-disabled if
leaked. Source: <https://jules.google/docs/api/reference/overview>,
<https://developers.google.com/jules/api>

**Resources** are Google-style hierarchical names:
`Sources`, `Sessions`, `Activities` (per-session events).
Source: <https://jules.google/docs/api/reference/overview>

**Session creation** is `POST /v1alpha/sessions` with a JSON body containing
`prompt` (required), optional `title`, optional `sourceContext` (the repository
and starting branch), `requirePlanApproval`, and `automationMode`.
Source: <https://jules.google/docs/api/reference/sessions>

**Plan approval can be required.** `requirePlanApproval: true` makes a plan wait
for explicit approval. The documentation states that when it is *not* set,
"plans are auto-approved". Source: <https://developers.google.com/jules/api>

**Automatic PR creation is opt-in.** `automationMode: "AUTO_CREATE_PR"` creates
a pull request automatically; the docs state "By default, no PR will be
automatically created." Source: <https://developers.google.com/jules/api>

**The diff is retrievable as data.** This is the decisive capability. Activities
carry artifacts, and a `changeSet` artifact contains a `gitPatch` with:

- `baseCommitId` — the commit the patch applies to
- `unidiffPatch` — "The patch in unified diff format"
- `suggestedCommitMessage`

Source: <https://jules.google/docs/api/reference/types>,
<https://jules.google/docs/api/reference/activities>

Bash output is also returned as structured artifacts, with `command`, `output`,
and `exitCode` fields. Source: <https://jules.google/docs/api/reference/activities>

**Pagination:** list endpoints accept `pageSize` and `pageToken`.
Source: <https://jules.google/docs/api/reference/overview>

**Sessions can be deleted:** `DELETE /v1alpha/sessions/{sessionId}`.
Source: <https://jules.google/docs/api/reference/sessions>

**Error handling:** standard HTTP status codes (200, 400, 401, 403, 404, 429,
500) with a JSON error body containing `code`, `message`, and `status`.
Source: <https://jules.google/docs/api/reference/overview>

**Pricing and limits.** Jules is available in three tiers, quoted from the
official limits page:

| Plan | Daily tasks (rolling 24h) | Concurrent tasks | Model access |
| --- | ---: | ---: | --- |
| Jules | 15 | 3 | Gemini 2.5 Pro |
| Jules in Pro | 100 | 15 | Higher access, starting with Gemini 3 Pro |
| Jules in Ultra | 300 | 60 | Priority access, starting with Gemini 3 Pro |

Source: <https://jules.google/docs/usage-limits>

Paid plans are reached through a Google AI Plans subscription, which the docs
note is "currently available only for individual Google Accounts (ending in
@gmail.com)". Source: <https://jules.google/docs/usage-limits>

**Jules Tools CLI** is a separate surface: `npm install -g @google/jules`,
giving a `jules` command. Authentication is `jules login`, which "will open a
browser window to guide you through the Google authentication process".
Source: <https://jules.google/docs/cli/reference>

### 1.2 What can be automated

- **Creating a session** from an issue description, against a connected repo and
  branch — `POST /v1alpha/sessions`.
- **Requiring human plan approval** rather than auto-approval.
- **Avoiding PR creation** by not setting `automationMode`.
- **Polling progress** and reading plans, messages, and status as structured
  activities.
- **Retrieving the unified diff** (`unidiffPatch`) and the base commit, which is
  exactly what a patch hash and a local review report need.
- **Reading command output** (`bashOutput`) for the "commands run" section of a
  review report.
- **Deleting the session** afterwards.

### 1.3 What cannot be automated, and why

- **No local execution.** Jules clones and runs inside a Google Cloud VM. It
  cannot be pointed at a workspace on this machine, so it cannot replace the
  local sandbox described in step 2.5.
- **Prerequisite: the repo must already be connected.** A source has the form
  `sources/github-<owner>-<repo>` and the docs require installing the Jules
  GitHub app first. Connecting a repository is a one-time manual, browser-based
  action that cannot be performed from this environment.
- **It cannot be used on a repository the account cannot access.** For a
  third-party open-source repo, the account would need a fork with the app
  installed — a remote write, which Phase 2 forbids.
- **API keys cannot be minted programmatically.** They are created in the web
  app. There is no documented endpoint to create one.
- **`jules login` for the CLI is interactive.** It opens a browser, so the CLI
  surface cannot be driven headlessly without an already-authenticated profile.
- **No verified statement about whether API sessions share the task quota.** The
  limits page describes task limits per plan but does not say whether API-driven
  sessions draw from the same pool. This is **unresolved**, so ForgeFlow must not
  assume an API call is free of quota.
- **Alpha stability.** The docs warn that specifications and keys may change, so
  an integration built today can break without notice.

### 1.4 Verdict for ForgeFlow

Jules is a **real, documented, API-drivable** provider, and its `unidiffPatch`
artifact fits ForgeFlow's patch-hash and review-report requirements precisely.
It is nonetheless **not usable end-to-end in this environment**, for one
concrete reason: no `JULES_API_KEY` is present, and one cannot be created
without an interactive browser session on a Google account with a connected
repository.

The integration is therefore implemented against the documented REST API, but
it **fails closed**: without a key it refuses to run rather than pretending to
have done work. See step 2.3.

---

## 2. Gemini CLI

### 2.1 Verified facts

**What it is.** An open-source (Apache 2.0) terminal agent from Google.
Source: <https://github.com/google-gemini/gemini-cli>

**Installation:** `npm install -g @google/gemini-cli`, or `npx
@google/gemini-cli`, or Homebrew (`brew install gemini-cli`).
Source: <https://github.com/google-gemini/gemini-cli>

**Authentication** has three documented options:
1. Sign in with Google (OAuth, browser-based).
2. `GEMINI_API_KEY` — the docs list a free tier of "1000 requests/day with
   Gemini 3 (mix of flash and pro)".
3. Vertex AI, via `GOOGLE_API_KEY` plus `GOOGLE_GENAI_USE_VERTEXAI=true`.

Source: <https://github.com/google-gemini/gemini-cli>

**Headless mode is documented and real.** Headless mode triggers "when the CLI
is run in a non-TTY environment or when providing a query with the `-p` (or
`--prompt`) flag". `--output-format json` returns a single JSON object with
`response`, `stats` (token usage and latency), and an optional `error`. A
streaming JSON (JSONL) mode emits `init`, `message`, `tool_use`, `tool_result`,
`error`, and `result` events.
Source: <https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/cli/headless.md>

**Exit codes:** `0` success, `1` general/API failure, `42` input error, `53`
turn limit exceeded. Source: same document.

**Sandboxing** exists but requires a container runtime: it uses Docker or Podman
images, configurable through `settings.json` or `GEMINI_SANDBOX_IMAGE`.
Source: <https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/cli/sandbox.md>

**Model selection:** `gemini -m gemini-2.5-flash`. The README also notes Gemini
CLI "is not optimized for the latest Gemini models".
Source: <https://github.com/google-gemini/gemini-cli>

### 2.2 What can be automated

- Non-interactive invocation with `-p`, giving scriptable text or JSON output.
- Token usage and latency from `stats`, which suits ForgeFlow's cost tracking.
- Model selection per call, which suits the cheap-triage / strong-analysis split.
- Distinct exit codes for success, API failure, bad input, and turn-limit
  exhaustion.

### 2.3 What cannot be automated here

- **No local isolation available.** Gemini CLI's own sandbox needs Docker or
  Podman. With no Docker daemon in this environment, running Gemini CLI on
  untrusted repository code would mean running it **directly on the host** —
  exactly what step 2.5 forbids.
- **OAuth is interactive**, though the API-key path is not, so this is not a
  hard blocker in principle.
- **The tool executes shell commands.** That is the point of an agent, and it is
  also why it cannot be pointed at untrusted repository content without a real
  sandbox.

### 2.4 Verdict

Gemini CLI is a good fit for ForgeFlow's **analysis** step, where the model only
reads text and returns JSON (step 2.4), because no untrusted code executes. It
is **not** a fit for the **implementation** step in this environment, because
that step requires executing repository code and no isolation is available.

---

## 3. Decision

| Step | Chosen approach | Reason |
| --- | --- | --- |
| 2.3 agent provider | **Jules, behind `CodingAgent`** | Documented REST API; returns `unidiffPatch`, which is what the patch hash and review report need. Fails closed without a key. |
| 2.3 tests | **`MockAgent`** | No network, no key, deterministic. |
| 2.4 analysis | **Provider-swappable LLM adapter** | Text in, validated JSON out. No code execution, so no sandbox needed. |
| 2.5 implementation | **Refuse to execute** unless real isolation exists | No Docker daemon here. Running untrusted repository scripts on the host is never acceptable. |

### The honest limitation

ForgeFlow Phase 2 **cannot run a real coding agent end-to-end in this
environment.** Two independent blockers:

1. No `JULES_API_KEY`, and no way to create one non-interactively.
2. No Docker daemon, so no isolated place to execute untrusted code.

What Phase 2 *does* deliver is the complete, tested machinery around those
blockers: the provider interface, the API client, the patch hashing, the review
report, and an explicit refusal when isolation is missing. The moment a key and
a container runtime exist, the same code paths become live without redesign.

This limitation is recorded in `docs/phase2-notes.md`.

---

## 4. Sources

All accessed 2026-10-22.

- <https://developers.google.com/jules/api> — Jules API introduction, authentication, session creation, `automationMode`, `requirePlanApproval`
- <https://jules.google/docs> — getting started, GitHub connection
- <https://jules.google/docs/api/reference/overview> — base URL, auth header, endpoints, pagination, error codes
- <https://jules.google/docs/api/reference/sessions> — session create/delete, request bodies
- <https://jules.google/docs/api/reference/activities> — activity types, `changeSet`, `bashOutput`
- <https://jules.google/docs/api/reference/types> — `ChangeSet`, `GitPatch`, `Artifact` field reference
- <https://jules.google/docs/cli/reference> — Jules Tools CLI installation, `jules login`, `jules remote`
- <https://jules.google/docs/usage-limits> — plan comparison, task limits
- <https://github.com/google-gemini/gemini-cli> — installation, authentication options, model selection
- <https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/cli/headless.md> — headless mode, output formats, exit codes
- <https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/cli/sandbox.md> — sandbox prerequisites and configuration
- <https://blog.google/innovation-and-ai/models-and-research/google-labs/jules-tools-jules-api> — Jules Tools and API announcement
