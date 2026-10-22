"""Smoke test: the CLI entry point runs and its --help exits 0."""

import subprocess
import sys


def test_cli_help_exits_zero() -> None:
    """`python -m forgeflow.cli --help` prints usage and exits 0."""
    result = subprocess.run(
        [sys.executable, "-m", "forgeflow.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "forgeflow" in result.stdout.lower()
    assert "Usage" in result.stdout or "usage" in result.stdout
