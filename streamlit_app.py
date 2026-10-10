"""Local Streamlit entry point for the CloudRCA incident dashboard."""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st
from cloudrca_backend.api import Dataset, Job, Page
from cloudrca_backend.dashboard import (
    BackendClient,
    BackendClientError,
    DashboardSettings,
    display_label,
    incident_summary,
    layer_state,
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
        render_detail(settings)


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
                st.success("Incident selected. Open Evidence to inspect findings and cited events.")
    total = page.total
    previous, next_page = st.columns(2)
    if previous.button("Previous", disabled=offset == 0, use_container_width=True):
        st.session_state["incident_offset"] = max(0, offset - 20)
        st.rerun()
    if next_page.button("Next", disabled=offset + 20 >= total, use_container_width=True):
        st.session_state["incident_offset"] = offset + 20
        st.rerun()


def render_detail(settings: DashboardSettings) -> None:
    incident_id = st.session_state.get("selected_incident_id")
    if not isinstance(incident_id, str):
        st.info("No incident is selected. Choose Open incident from the incident list.")
        return
    try:
        incident = client(settings).incident(incident_id)
        findings = client(settings).list_findings()
        evidence = client(settings).list_evidence()
    except BackendClientError as error:
        st.error(str(error))
        return
    payload = incident.payload
    st.subheader(incident_summary(payload))
    st.caption(f"Incident ID: {incident.artifact_id}")
    severity = str(payload.get("severity", "unknown"))
    st.write(f"Severity: {display_label(severity)}")
    cards = st.columns(3)
    for card, name, identifier in zip(cards, ("Database", "VM", "Hypervisor"), ("database", "vm_guest_os", "host_hypervisor"), strict=True):
        card.metric(name, display_label(layer_state(payload, identifier)))
    uncertainty = payload.get("uncertainty")
    if isinstance(uncertainty, str) and uncertainty:
        st.warning(uncertainty)
    render_timeline_and_graph(payload)
    st.subheader("Findings")
    if not findings.items:
        st.info("No persisted findings are available for this incident.")
    for finding in findings.items:
        with st.expander(incident_summary(finding.payload)):
            st.json(finding.payload, expanded=False)
    st.subheader("Evidence and provenance")
    if not evidence.items:
        st.info("No persisted evidence is available for this incident.")
    for event in evidence.items:
        with st.expander(str(event.payload.get("event_id", event.artifact_id))):
            st.json(event.payload, expanded=False)


def render_timeline_and_graph(payload: dict[str, object]) -> None:
    timeline = [item for item in payload.get("timeline", []) if isinstance(item, dict)] if isinstance(payload.get("timeline"), list) else []
    st.subheader("UTC timeline")
    if not timeline:
        st.info("No canonical timeline is persisted for this incident.")
    else:
        ordered = sorted(timeline, key=lambda item: str(item.get("timestamp", "")))
        figure = go.Figure(go.Scatter(x=[item.get("timestamp") for item in ordered], y=[item.get("layer", "event") for item in ordered], mode="markers", text=[item.get("summary", "") for item in ordered], hovertemplate="%{x}<br>%{y}<br>%{text}<extra></extra>"))
        figure.update_layout(xaxis_title="UTC", yaxis_title="Layer", height=300)
        st.plotly_chart(figure, use_container_width=True)
        st.dataframe(ordered, use_container_width=True, hide_index=True)
    chain = [item for item in payload.get("causal_chain", []) if isinstance(item, dict)] if isinstance(payload.get("causal_chain"), list) else []
    st.subheader("Causal graph")
    if not chain:
        st.info("No persisted causal graph is available for this incident.")
        return
    nodes = [str(item.get("cause_id", "cause")) for item in chain] + [str(chain[-1].get("effect_id", "effect"))]
    graph = go.Figure(go.Scatter(x=list(range(len(nodes))), y=[0] * len(nodes), mode="lines+markers+text", text=nodes, textposition="top center", hovertemplate="%{text}<extra></extra>"))
    graph.update_layout(xaxis=dict(visible=False), yaxis=dict(visible=False), height=220)
    st.plotly_chart(graph, use_container_width=True)
    st.dataframe(chain, use_container_width=True, hide_index=True)


render()
