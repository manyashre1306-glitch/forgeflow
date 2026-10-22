"""ForgeFlow command-line interface.

Phase 1 scaffold: only the Typer application and a version option exist.
Discovery, ranking, storage, and reporting commands are added in later steps.
"""

import typer

app = typer.Typer(
    name="forgeflow",
    help="ForgeFlow: AI-powered open-source contribution automation (Phase 1).",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """ForgeFlow issue discovery and ranking.

    Phase 1 scope: discover public GitHub issues from an allowlist, deduplicate,
    rank, persist to SQLite, and write Markdown reports. ForgeFlow never writes
    to an external repository in Phase 1.
    """


if __name__ == "__main__":
    app()
