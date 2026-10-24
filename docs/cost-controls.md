# Cost controls

ForgeFlow calls paid models and (optionally) CI. This document describes the
budgets and caps that bound that spend, and how to verify current prices from
official sources. It deliberately does **not** promise that a given budget covers
a fixed number of tasks: token use per task varies with the issue, the context
size, and how many repair attempts are needed, so any fixed "N tasks per $5"
figure would be a guess presented as a measurement.

## What bounds the spend

| Control | Where | Default | Effect |
| --- | --- | --- | --- |
| Monthly cap | `llm.monthly_cap_usd` in `config/repositories.yaml` | `5.0` | `LLMClient.check_budget()` raises `BudgetExceededError` once the month's recorded spend reaches the cap |
| Per-task time/token/cost ceiling | `killswitch.TaskGuard` | attempts 10, tokens 500k, cost $2.0 | Fails closed: once a ceiling is reached, every further check raises |
| Max retries | `llm.max_retries` | `2` | Caps retries on a single model call |
| Repair attempts | `validation.MAX_REPAIR_ATTEMPTS` | `2` | Caps automated repair loops after a failed validation (SPEC 7) |
| Max output tokens | `llm.max_output_tokens` | `4096` | Caps the size of a single completion |
| Request timeout | `llm.timeout_seconds` | `60.0` | Caps how long one call can hang |
| Kill switch | `kill_switch` / `FORGEFLOW_KILL_SWITCH` / `forgeflow kill-switch on` | off | Halts all model calls and sandbox execution |

Two rules matter more than the numbers:

1. **`check_budget()` runs before every paid call.** If no budget query is wired
   in, ForgeFlow refuses to call at all rather than assume the month's spend is
   zero.
2. **Deterministic filters run first.** Discovery, dedupe, and the six-factor
   ranking are free; only the strongest candidates reach a model. The cheap
   triage model runs before the expensive analysis model.

## How spend is recorded

Every model call is written to the `llm_usage` table with its prompt and
completion token counts and its cost. `storage.spent_in_month()` sums the current
month; `outcome_tracker.cost_for_task()` sums one task. `forgeflow metrics`
reports `cost_per_reviewed_patch` from these records. If a call's price is
unknown, it is flagged by `unknown_models()` rather than silently recorded as
zero.

## Verify current pricing yourself

Prices change. ForgeFlow ships **no** hard-coded prices: `config/repositories.yaml`
has every rate at `0.0` on purpose, and a comment says to fill them from the
vendor's current published pricing. Before trusting any cost report:

1. Read the vendor's official pricing page (links below).
2. Enter the current input and output price per million tokens for each model
   under `llm.pricing` in `config/repositories.yaml`.
3. Re-run the workflow; new `llm_usage` rows will use the updated prices.

Official sources to check:

- Gemini API pricing: <https://ai.google.dev/gemini-api/docs/pricing>
- Jules (coding agent) pricing: <https://jules.google.com/> — check the pricing
  page linked there; Jules has no public per-token price list, so treat its cost
  as operator-supplied.
- GitHub Actions billing: <https://docs.github.com/en/billing/concepts/product-billing/github-actions>

Do not treat a price copied from a blog post or a cached search result as
authoritative; read it from the vendor's own page at the time you need it.

## Why no "tasks per budget" promise

SPEC section 12 is explicit: *"Do not promise that a particular budget will cover
a fixed number of tasks."* A task's cost depends on the issue size, the context
size, the number of retries, and the number of repair attempts, none of which is
fixed. What can be stated honestly is the shape of the bound:

- The monthly cap is a hard ceiling: once recorded spend reaches it, paid calls
  stop.
- Each task is additionally bounded by `TaskGuard` and the retry/repair caps.

So the cap bounds the *spend*, not the *task count*. With the shipped `0.0`
prices, a cost report is a record of tokens, not money, until you enter real
prices.

## CI cost

The GitHub Actions workflow in this repository (`.github/workflows/ci.yml`) runs
`ruff` and `pytest` only. It uses **no secrets**, requests `contents: read`, and
runs only on the free tier for public repositories under normal use. It makes no
network calls beyond installing dependencies and does not call any paid model.
