"""Dashboard display of real, job-scoped pipeline results."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.request import Request

import pytest
import uvicorn
from cloudrca_backend.api import Artifact, ArtifactKind, Job, JobRepository, create_app
from cloudrca_backend.dashboard import (
    BackendClient,
    BackendClientError,
    DashboardSettings,
    incident_evidence,
    job_run_summary,
    outcome_tone,
)
from cloudrca_backend.pipeline import RcaPipeline
from cloudrca_backend.routing import load_routing_config
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[2]


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def artifact(artifact_id: str, kind: str, payload: dict[str, object], job_id: str = "job-1") -> Artifact:
    return Artifact(artifact_id=artifact_id, job_id=job_id, kind=ArtifactKind(kind), payload=payload)


def test_incident_evidence_is_scoped_to_its_job_and_incident() -> None:
    incident = artifact("a-incident", "incident", {"incident_id": "incident-1", "event_ids": ["event-1"]})
    artifacts = [
        incident,
        artifact("a-finding", "finding", {"incident_id": "incident-1"}),
        artifact("a-other-finding", "finding", {"incident_id": "incident-2"}),
        artifact("a-event", "event", {"event_id": "event-1"}),
        artifact("a-other-event", "event", {"event_id": "event-2"}),
        artifact("a-other-job", "event", {"event_id": "event-1"}, job_id="job-2"),
        artifact("a-graph", "graph", {"incident_id": "incident-1"}),
        artifact("a-report", "report", {"incident_id": "incident-1"}),
        artifact("a-run", "run", {"normalization": {"accepted": 1}}),
    ]
    scoped = incident_evidence(incident, artifacts)
    assert [item.artifact_id for item in scoped.findings] == ["a-finding"]
    assert [item.artifact_id for item in scoped.events] == ["a-event"]
    assert scoped.graph is not None and scoped.graph.artifact_id == "a-graph"
    assert scoped.report is not None and scoped.report.artifact_id == "a-report"
    assert job_run_summary(artifacts) == {"normalization": {"accepted": 1}}
    assert job_run_summary([]) == {}
    assert [outcome_tone(item) for item in ("rca_reported", "insufficient_evidence", "rca_failed", "not_analyzed")] == [
        "success", "info", "error", "error",
    ]


def test_client_reads_job_artifacts(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []
    body = b'[{"artifact_id":"a","job_id":"job-1","kind":"run","payload":{}}]'

    def fake_open(request: Request, timeout: float) -> FakeResponse:
        captured.append(request.full_url)
        return FakeResponse(body)

    monkeypatch.setattr("cloudrca_backend.dashboard.urlopen", fake_open)
    assert BackendClient(DashboardSettings()).job_artifacts("job-1")[0].kind is ArtifactKind.RUN
    assert captured == ["http://127.0.0.1:8000/api/v1/analysis-jobs/job-1/artifacts"]
    body = b'[{"unexpected": true}]'
    with pytest.raises(BackendClientError, match="unexpected response"):
        BackendClient(DashboardSettings()).job_artifacts("job-1")


@contextmanager
def pipeline_api(tmp_path: Path, agents: Any) -> Iterator[str]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    database = tmp_path / "api.sqlite3"
    runner = RcaPipeline(JobRepository(database).dataset_content, lambda: agents, load_routing_config(ROOT / "config/routing/aiops2025.yaml"))
    server = uvicorn.Server(uvicorn.Config(create_app(database, runner), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def wait_for_terminal(client: BackendClient, job: Job) -> Job:
    for _ in range(200):
        job = client.get_job(job.job_id)
        if job.status.value not in {"queued", "running"}:
            return job
        time.sleep(0.01)
    raise AssertionError("analysis job did not finish")


def test_streamlit_shows_real_job_results_and_scoped_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, aiops_jsonl: bytes, scripted_jcode: Any, agents_for: Any
) -> None:
    with pipeline_api(tmp_path, agents_for(scripted_jcode())) as base_url:
        monkeypatch.setenv("CLOUDRCA_BACKEND_URL", base_url)
        client = BackendClient(DashboardSettings(backend_url=base_url))
        dataset = client.upload_dataset("aiops.jsonl", aiops_jsonl, "application/json")
        job = wait_for_terminal(client, client.start_analysis(dataset.dataset_id))
        assert job.status.value == "completed"

        app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=30).run()
        app.sidebar.radio[0].set_value("Analyze").run()
        app.session_state["job"] = job
        app.run()
        assert not app.exception
        assert [metric.value for metric in app.metric] == ["10", "4", "0"]
        assert "Incidents from this analysis" in [item.value for item in app.subheader]
        assert len(app.dataframe[0].value) == 3

        reported = next(item for item in client.job_artifacts(job.job_id) if item.payload.get("analysis_outcome") == "rca_reported")
        app.session_state["selected_incident_id"] = reported.artifact_id
        app.sidebar.radio[0].set_value("Evidence").run()
        assert not app.exception
        assert {"RCA report", "Findings", "Evidence and provenance"} <= {item.value for item in app.subheader}
        assert any("Rca Reported" in item.value for item in app.success)
        assert any("Probable root cause (hypothesis)" in item.value for item in app.warning)
        assert "Model confidence: low (0.4)" in [item.value for item in app.caption]
        assert len(app.expander) == 1 + len(reported.payload["event_ids"])  # type: ignore[arg-type]

        insufficient = next(
            item for item in client.job_artifacts(job.job_id) if item.payload.get("analysis_outcome") == "insufficient_evidence"
        )
        app.session_state["selected_incident_id"] = insufficient.artifact_id
        app.run()
        assert not app.exception
        assert any("No RCA report was generated" in item.value for item in app.info)
        assert any("no root cause was inferred" in item.value for item in app.warning)
