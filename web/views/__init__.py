"""The website's pages, one module per page.

Each module exposes ``render(mode)``. Pages import Streamlit lazily through
:mod:`web.ui`, so importing a page module never requires Streamlit to be
installed and never performs I/O at import time.
"""
