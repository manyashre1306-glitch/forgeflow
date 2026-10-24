"""ForgeFlow's Streamlit website (SPEC section 15 portfolio deliverables).

The website is a thin presentation layer over the real ForgeFlow modules. It
contains no discovery, ranking, analysis, or publishing logic of its own; it
calls the same functions the CLI calls.

Two run modes, selected by ``FORGEFLOW_WEB_MODE``:

* ``hosted`` (the default when the variable is unset) runs on a shared public
  server. It never reads the local database, never imports the publisher or the
  publishing token, and never executes repository code. Every secret is the
  visitor's own and lives only in the session.
* ``local`` runs on the operator's own machine. It may use the real database and
  the real publishing token, but only through the existing modules and their
  existing guards.

The mode is a hard gate in code, not a label. See :mod:`web.modes`.
"""
