"""Offline, dry-run end-to-end demonstration (SPEC section 15).

This package drives the whole ForgeFlow pipeline against a controlled fixture
repository, in one process, with no network and no external write of any kind:

    discover -> rank -> context -> analyze -> implement -> validate ->
    exception -> approve -> dry-run publish

The publish step is a **dry run**. It builds the full plan and the PR body that
*would* be sent, records the attempt as a dry run, and asserts that no write was
attempted. Nothing is sent to GitHub.

Run it with::

    uv run python -m demo

See ``docs/demonstration.md`` for what it shows and what it deliberately does
not claim.
"""

from __future__ import annotations

from demo.run_demo import DemoReport, run_demo

__all__ = ["DemoReport", "run_demo"]
