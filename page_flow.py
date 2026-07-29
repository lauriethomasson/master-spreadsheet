"""
page_flow.py

Single source of truth for page order in this app. Streamlit's default
sidebar page list is hidden (see page_setup.py) in favor of explicit
Back/Next buttons driven by this list, so adding a page later just means
adding one entry here - nothing else needs to change.
"""

import streamlit as st

PAGES = [
    {"path": "app.py", "label": "Upload"},
    {"path": "pages/2_Review_and_Master.py", "label": "Review & Master"},
]


def render_nav_buttons(current_path: str) -> None:
    """
    Call at the bottom of a page script:

        page_flow.render_nav_buttons("app.py")

    current_path must match a "path" entry in PAGES exactly (the same
    string st.switch_page expects - relative to the entrypoint file).
    """
    matches = [i for i, p in enumerate(PAGES) if p["path"] == current_path]
    if not matches:
        raise ValueError(f"{current_path!r} is not registered in page_flow.PAGES")
    index = matches[0]
    is_first = index == 0
    is_last = index == len(PAGES) - 1

    st.divider()
    back_col, _, next_col = st.columns([1, 4, 1])

    with back_col:
        back_label = "← Back" if is_first else f"← {PAGES[index - 1]['label']}"
        if st.button(back_label, width="stretch", disabled=is_first, key="page_flow_back"):
            st.switch_page(PAGES[index - 1]["path"])

    with next_col:
        next_label = "Next →" if is_last else f"{PAGES[index + 1]['label']} →"
        if st.button(next_label, width="stretch", type="primary", disabled=is_last, key="page_flow_next"):
            st.switch_page(PAGES[index + 1]["path"])
