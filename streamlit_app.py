"""Local Streamlit entry point for the CloudRCA incident dashboard."""

from __future__ import annotations

import streamlit as st
from cloudrca_backend.api import Dataset, Job, Page
from cloudrca_backend.dashboard import (
    BackendClient,
    BackendClientError,
    DashboardSettings,
    display_label,
    incident_summary,
)


def render() -> None:
    st.set_page_config(page_title="CloudRCA", page_icon="🔎", layout="wide")
    settings = DashboardSettings.from_environment()

    with st.sidebar:
        st.title("CloudRCA")
        page = st.radio("Navigate", ("Overview", "Analyze", "Incidents", "Evidence"), label_visibility="visible")
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
    elif page == "Analyze":
        render_analysis(settings)
    elif page == "Incidents":
        render_incidents(settings)
    else:
        st.info("No evidence is selected. Choose an incident to inspect its supporting events.")


def client(settings: DashboardSettings) -> BackendClient:
    return BackendClient(settings)


def render_analysis(settings: DashboardSettings) -> None:
    uploaded = st.file_uploader("Dataset", type=("json", "jsonl"), help="Upload a JSON or JSONL local log dataset.")
    if uploaded is not None and st.button("Upload and validate", type="primary"):
        with st.spinner("Validating dataset…"):
            try:
                st.session_state["dataset"] = client(settings).upload_dataset(
                    uploaded.name, uploaded.getvalue(), uploaded.type or "application/octet-stream"
                )
            except BackendClientError as error:
                st.error(str(error))

    dataset = st.session_state.get("dataset")
    if isinstance(dataset, Dataset):
        st.success(f"Validated {dataset.filename}")
        st.json(dataset.validation, expanded=False)
        st.caption("Layer coverage is determined by the existing normalization and routing pipeline during analysis.")
        if st.button("Start analysis", type="primary"):
            try:
                st.session_state["job"] = client(settings).start_analysis(dataset.dataset_id)
            except BackendClientError as error:
                st.error(str(error))
    else:
        st.info("Upload a dataset to view validation feedback and begin analysis.")

    job = st.session_state.get("job")
    if isinstance(job, Job):
        render_job(settings, job)


def render_job(settings: DashboardSettings, job: Job) -> None:
    status = job.status.value
    st.subheader("Analysis progress")
    state = "running" if status in {"queued", "running"} else "error" if status == "failed" else "complete"
    st.status(f"Status: {display_label(status)}", state=state)
    st.caption(f"Job: {job.job_id}")
    refresh, cancel, retry = st.columns(3)
    if refresh.button("Refresh", use_container_width=True):
        try:
            st.session_state["job"] = client(settings).get_job(job.job_id)
            st.rerun()
        except BackendClientError as error:
            st.error(str(error))
    if status in {"queued", "running"} and cancel.button("Cancel", use_container_width=True):
        try:
            st.session_state["job"] = client(settings).cancel_job(job.job_id)
            st.rerun()
        except BackendClientError as error:
            st.error(str(error))
    if status in {"failed", "cancelled"} and retry.button("Retry", use_container_width=True):
        try:
            st.session_state["job"] = client(settings).retry_job(job.job_id)
            st.rerun()
        except BackendClientError as error:
            st.error(str(error))
    if status == "partial":
        st.warning("Analysis completed partially. Available incidents remain safe to inspect.")
    elif status == "failed":
        st.error(job.error or "Analysis failed.")


def render_incidents(settings: DashboardSettings) -> None:
    filters = {
        "severity": st.selectbox(
            "Severity", ("", "critical", "error", "warning", "high", "medium", "low", "info", "debug", "unknown")
        ),
        "status": st.selectbox("Status", ("", "open", "investigating", "resolved")),
        "layer": st.selectbox("Layer", ("", "database", "vm_guest_os", "host_hypervisor")),
    }
    sort_key = st.selectbox("Sort incidents by", ("timestamp", "severity", "summary"))
    offset = int(st.session_state.get("incident_offset", 0))
    try:
        with st.spinner("Loading incidents…"):
            page = client(settings).list_incidents(filters, offset=offset)
    except BackendClientError as error:
        st.error(str(error))
        return
    if not isinstance(page, Page) or not page.items:
        st.info("No incidents match the selected filters.")
        return
    incidents = sorted(page.items, key=lambda item: str(item.payload.get(sort_key, "")))
    for item in incidents:
        payload = item.payload
        with st.expander(incident_summary(payload)):
            st.write({key: payload.get(key) for key in ("severity", "timestamp", "layer", "status")})
            if st.button("Open incident", key=item.artifact_id):
                st.session_state["selected_incident_id"] = item.artifact_id
                st.success("Incident selected. Its detail and evidence view arrives in the next dashboard step.")
    total = page.total
    previous, next_page = st.columns(2)
    if previous.button("Previous", disabled=offset == 0, use_container_width=True):
        st.session_state["incident_offset"] = max(0, offset - 20)
        st.rerun()
    if next_page.button("Next", disabled=offset + 20 >= total, use_container_width=True):
        st.session_state["incident_offset"] = offset + 20
        st.rerun()


render()
