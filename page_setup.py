"""
page_setup.py

Shared bootstrap for app.py and every pages/*.py file. Streamlit's classic
multipage architecture runs each page file as its own independent script —
there's no shared "app shell" script that runs first — so anything that
must happen at the top of every page (page config, hiding the default
sidebar nav, an initial loading indicator) has to be triggered from each
file individually. This module exists so that's one function call per file
instead of duplicated logic.
"""

from contextlib import contextmanager

import streamlit as st

PAGE_CONFIG = dict(
    page_title="Master Spreadsheet Pipeline",
    page_icon="📋",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Streamlit's classic multipage mode auto-injects a page list into the
# sidebar from the pages/ directory - there's no Python-level toggle for
# just that (initial_sidebar_state only controls collapsed/expanded, and a
# collapsed sidebar can still be re-expanded to reveal it). Targeting this
# testid is the standard way to actually remove it; page order/navigation
# is handled explicitly instead via page_flow.py's Back/Next buttons.
_HIDE_SIDEBAR_NAV_CSS = """
<style>
[data-testid="stSidebarNav"] { display: none; }
</style>
"""


@contextmanager
def setup_page(page_key: str, loading_message: str = "Loading..."):
    """
    Call as the very first line of a page script, wrapping everything else
    in a `with` block:

        with page_setup.setup_page("review"):
            st.title(...)
            ...

    Sets page config, hides Streamlit's default sidebar page nav (replaced
    by page_flow.py's explicit Back/Next buttons), then shows
    loading_message only the first time this page runs in the current
    browser session — not on every rerun. Streamlit reruns the whole script
    on any interaction (a button click, an edited cell), not just on initial
    navigation to the page, so without gating on st.session_state, the
    message would reappear on every single click instead of just the
    initial page load.
    """
    st.set_page_config(**PAGE_CONFIG)
    st.markdown(_HIDE_SIDEBAR_NAV_CSS, unsafe_allow_html=True)

    loaded_key = f"_page_loaded_{page_key}"
    first_load = not st.session_state.get(loaded_key)
    st.session_state[loaded_key] = True

    if first_load:
        with st.spinner(loading_message):
            yield
    else:
        yield
