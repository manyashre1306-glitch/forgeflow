"""ForgeFlow evaluation harness (SPEC section 11).

A reproducible, one-command harness that computes the SPEC section 11 metrics
over a small set of *known, controlled* fixture tasks. It exists to prove the
metric computation is correct and to make the "not enough data" cases visible,
not to claim real-world or benchmark performance.

Run it with::

    uv run python -m evaluation

See ``docs/evaluation.md`` for the methodology and the honest limitations.
"""

from __future__ import annotations

from evaluation.harness import EvaluationReport, collect_metrics, render_report

__all__ = ["EvaluationReport", "collect_metrics", "render_report"]
