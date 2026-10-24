# Evaluation methodology and results

This document describes how ForgeFlow measures itself (SPEC section 11), what the
harness actually reports today, and — just as importantly — what it does **not**
claim. It is written to be honest first: a metric with no data says "not enough
data", and a benchmark is not claimed unless it was measured here and can be
reproduced.

## Reproduce it with one command

```bash
uv run python -m evaluation
```

The command builds an in-memory SQLite database from a fixed set of controlled
fixtures, records real facts for each through ForgeFlow's own storage, outcome,
and validation APIs, then computes and prints the metrics. It performs no network
access and needs no credentials. The same code path is covered by
`tests/test_evaluation.py`, so the numbers below cannot drift silently.

## What the harness measures

The nine SPEC section 11 metrics, plus per-task fields and a small set of
reproducible patch cases.

| Metric | How it is computed | Denominator |
| --- | --- | --- |
| `issue_relevance_rate` | tracked tasks a maintainer engaged with (approved, requested changes, or merged) ÷ tracked tasks | tracked PRs |
| `patch_review_acceptance_rate` | tracked tasks approved or merged ÷ tracked tasks | tracked PRs |
| `test_pass_rate` | checks that passed ÷ checks that actually executed | executed checks |
| `pr_merge_rate` | tracked tasks merged ÷ tracked tasks | tracked PRs |
| `substantive_revision_rate` | tracked tasks with a changes-requested review ÷ tracked tasks | tracked PRs |
| `cost_per_reviewed_patch` | total recorded model cost ÷ tracked tasks | tracked PRs |
| `task_failure_rate` | tracked tasks closed without merging ÷ tracked tasks | tracked PRs |
| `security_incidents` | audit events at `critical` severity | — |
| `time_saved_hours` | **not measured** | — |

Per-task fields (SPEC section 11 also asks for these):

- `patch_usefulness` — how far the PR got: 1.0 merged, 0.5 open with an approval,
  0.25 open, 0.0 closed without merging.
- `test_quality` — fraction of executed checks that passed. A skipped check is
  not a pass.
- `complexity` — patch size, saturating at 400 changed lines.
- `maintainer_feedback` — the review bodies, verbatim (redacted on storage).

## Results on the current fixtures

These are the numbers the harness prints today. They are **fixture arithmetic**,
chosen so the expected values are known by construction; they are not a claim
about real repositories.

```
issue_relevance_rate             0.5000 (n=4)
patch_review_acceptance_rate     0.2500 (n=4)
test_pass_rate                   0.8750 (n=8)
pr_merge_rate                    0.2500 (n=4)
substantive_revision_rate        0.2500 (n=4)
cost_per_reviewed_patch          $0.2500 (n=4)
task_failure_rate                0.2500 (n=4)
security_incidents               0
time_saved_hours                 not measured
```

The four tracked fixture tasks are: one merged with an approval, one open draft
with no review, one open draft where the maintainer requested a regression test,
and one closed without merging. A fifth task was discovered but never
implemented, so it has no patch and no tracked PR.

### "Not enough data", shown rather than hidden

When there are no tracked PRs at all, every rate reports **not enough data**
instead of `0.0`. `0.0` would read as "measured, and the answer is none", which
is a different and misleading claim. `tests/test_evaluation.py` asserts this on an
empty database. `security_incidents` is the exception: a count of zero is a real
measurement (nothing bad happened), not an absence of data.

## The one real end-to-end run so far

ForgeFlow has published exactly once, to the sandbox repository it is allowed to
write to: draft PR
[`manyashre1306-glitch/forgeflow-sandbox#2`](https://github.com/manyashre1306-glitch/forgeflow-sandbox/pull/2).
The patch was **synthetic** (produced by the mock agent, not a real coding model,
because no `JULES_API_KEY` and no container runtime are available here). Tracking
it records status **open**, **draft**, not merged, with no reviews yet. That is a
single data point in a sandbox, so the harness does not fold it into a headline
rate — a rate over one synthetic task would be noise presented as signal.

## Reproducible patch cases (not SWE-bench)

SPEC section 11 allows "a small set of known issues or a benchmark such as
SWE-bench where appropriate". SWE-bench is **not run here**: it needs a dataset,
network access, and a container runtime, none of which are present. Claiming
SWE-bench numbers without running it would be fabrication, which SPEC section 15
forbids.

In its place the harness runs three deterministic, offline patch-application
cases that check a stored diff applies to a known base tree and produces a known
result — and that a bad diff is refused rather than guessed at:

```
[ok] applies_a_clean_addition: content matches
[ok] refuses_context_mismatch: refused: context mismatch ...
[ok] refuses_a_missing_base_file: refused: ... not present in the base tree ...
patch_case_pass_rate: 1.0000
```

## Limitations

- The metrics are computed over **controlled fixtures**, not a real workload. They
  prove the arithmetic and the "not enough data" behaviour; they say nothing about
  real-world usefulness.
- `time_saved_hours` is **not measured**. ForgeFlow does not time a human
  baseline, and a constant estimate would be a fabricated benchmark.
- There is **one** real publish, in a sandbox, with a **synthetic** patch. One
  data point is not a rate.
- No benchmark performance is claimed, because none was measured reproducibly
  here.
- Test-pass and cost figures come from ForgeFlow's own records; a task with no
  recorded evidence contributes nothing rather than being counted as a pass.

## The honest one-line summary

> ForgeFlow's evaluation harness is reproducible and correct on controlled
> fixtures, and it reports "not enough data" where data is missing. It does not
> claim benchmark performance, real-world acceptance rates, or time saved,
> because those have not been measured.
