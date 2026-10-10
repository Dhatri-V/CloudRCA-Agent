"""Local Streamlit entry point for the CloudRCA incident dashboard."""

from __future__ import annotations

import streamlit as st
from cloudrca_backend.dashboard import (
    BackendClient,
    BackendClientError,
    DashboardSettings,
    display_label,
)


def render() -> None:
    st.set_page_config(page_title="CloudRCA", page_icon="🔎", layout="wide")
    settings = DashboardSettings.from_environment()

    with st.sidebar:
        st.title("CloudRCA")
        page = st.radio("Navigate", ("Overview", "Incidents", "Evidence"), label_visibility="visible")
        st.caption(f"Backend: {settings.backend_url}")
        if st.button("Check backend connection", use_container_width=True):
            try:
                status = BackendClient(settings).health().get("status", "unknown")
            except BackendClientError as error:
                st.error(str(error))
            else:
                st.success(f"Backend status: {display_label(str(status))}")

    st.title(page)
    st.caption("All timestamps and evidence are displayed in UTC.")
    if page == "Overview":
        st.info("No analysis is selected. Upload data to begin a local RCA run.")
    elif page == "Incidents":
        st.info("No incidents are available yet. Completed analysis runs will appear here.")
    else:
        st.info("No evidence is selected. Choose an incident to inspect its supporting events.")


render()
