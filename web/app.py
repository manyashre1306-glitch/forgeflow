"""ForgeFlow website entry point.

Run it with::

    uv run --extra web streamlit run web/app.py

The mode is resolved once, at startup, from ``FORGEFLOW_WEB_MODE`` and shown on
every page. Hosted is the default, so an unconfigured deployment is the stricter
one.

Navigation is a sidebar radio rather than Streamlit's automatic ``pages/``
discovery, so the available pages and their order are explicit and a page cannot
be reached by guessing a URL.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `streamlit run web/app.py` to import the `web` package without an
# install step, since Streamlit runs this file as a script.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from web import ui  # noqa: E402
from web.modes import resolve_mode  # noqa: E402
from web.views import (  # noqa: E402
    analysis_page,
    contribute,
    demo_page,
    docs_page,
    finder,
    home,
    patch_page,
    publish_page,
    rules,
    safety,
    validation_page,
)

#: (label, render callable). Order is the workflow order, with reference pages
#: last. Local-only pages are listed but render a locked message in hosted mode.
PAGES = (
    ("Home", home.render),
    ("Issue Finder", finder.render),
    ("Repo Rules Checker", rules.render),
    ("AI Analysis", analysis_page.render),
    ("Patch Generator", patch_page.render),
    ("Validation", validation_page.render),
    ("Approval and Publish", publish_page.render),
    ("Live Demo", demo_page.render),
    ("Safety", safety.render),
    ("Docs", docs_page.render),
    ("Contribute", contribute.render),
)


def main() -> None:
    st = ui._st()
    ui.page_setup("ForgeFlow")
    mode = resolve_mode()

    st.sidebar.title("ForgeFlow")
    st.sidebar.caption("Hosted" if mode.is_hosted else "Local")
    labels = [label for label, _ in PAGES]
    choice = st.sidebar.radio("Page", labels, key="nav_page")
    st.sidebar.divider()
    st.sidebar.caption(f"Mode source: {mode.source}")
    st.sidebar.caption(
        "Repository code is never executed in hosted mode. Your keys stay in this browser session."
    )

    renderer = dict(PAGES)[choice]
    renderer(mode)


# Streamlit runs a script with ``__name__ == "__main__"``, so this keeps the
# page working while leaving the module importable without Streamlit.
if __name__ == "__main__":
    main()
