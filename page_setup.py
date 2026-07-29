"""
page_setup.py

Shared bootstrap for app.py and every pages/*.py file. Streamlit's classic
multipage architecture runs each page file as its own independent script —
there's no shared "app shell" script that runs first — so anything that
must happen at the top of every page (page config, an initial loading
indicator) has to be triggered from each file individually. This module
exists so that's one function call per file instead of duplicated logic.
"""

from contextlib import contextmanager

import streamlit as st

PAGE_CONFIG = dict(page_title="Master Spreadsheet Pipeline", page_icon="📋", layout="wide")


@contextmanager
def setup_page(page_key: str, loading_message: str = "Loading..."):
    """
    Call as the very first line of a page script, wrapping everything else
    in a `with` block:

        with page_setup.setup_page("review"):
            st.title(...)
            ...

    Sets page config, then shows loading_message only the first time this
    page runs in the current browser session — not on every rerun. Streamlit
    reruns the whole script on any interaction (a button click, an edited
    cell), not just on initial navigation to the page, so without gating on
    st.session_state, the message would reappear on every single click
    instead of just the initial page load.
    """
    st.set_page_config(**PAGE_CONFIG)

    loaded_key = f"_page_loaded_{page_key}"
    first_load = not st.session_state.get(loaded_key)
    st.session_state[loaded_key] = True

    if first_load:
        with st.spinner(loading_message):
            yield
    else:
        yield
