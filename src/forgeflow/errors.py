"""Shared error types with no dependencies.

This module exists so that a user-facing error can be defined without importing
:mod:`forgeflow.cli`. The publishing credential module must raise a type the CLI
already catches, and importing the CLI from it would create a cycle
(``cli -> publisher -> publish_token -> cli``). A leaf module breaks that cycle
without weakening the credential isolation.
"""

from __future__ import annotations


class ForgeFlowError(RuntimeError):
    """A user-facing failure. The CLI prints the message and exits non-zero."""
