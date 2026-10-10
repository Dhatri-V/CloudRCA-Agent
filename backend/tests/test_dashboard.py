from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request

import pytest
import uvicorn
from cloudrca_backend.api import AnalysisResult, ArtifactKind, Job, JobStatus, create_app
from cloudrca_backend.dashboard import (
    BackendClient,
    BackendClientError,
    DashboardSettings,
    display_label,
    evidence_label,
    format_utc,
    layer_state,
    severity_tone,
)
from streamlit.testing.v1 import AppTest


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


@contextmanager
def running_api(tmp_path: Path) -> Iterator[str]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()

    def runner(_payload: dict[str, object]) -> AnalysisResult:
        return AnalysisResult(artifacts=((ArtifactKind.INCIDENT, {"summary": "VM CPU pressure", "layer": "vm_guest_os"}),))

    server = uvicorn.Server(uvicorn.Config(create_app(tmp_path / "api.sqlite3", runner=runner), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_dashboard_settings_come_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDRCA_BACKEND_URL", "https://api.example.test/")

    assert DashboardSettings.from_environment().backend_url == "https://api.example.test"


def test_client_reads_existing_health_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_open(request: Request, timeout: float) -> FakeResponse:
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return FakeResponse(b'{"status":"ok"}')

    monkeypatch.setattr("cloudrca_backend.dashboard.urlopen", fake_open)

    assert BackendClient(DashboardSettings()).health() == {"status": "ok"}
    assert captured == {"url": "http://127.0.0.1:8000/healthz", "timeout": 5.0}


def test_client_hides_transport_and_malformed_response_details(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cloudrca_backend.dashboard.urlopen", lambda *_args, **_kwargs: FakeResponse(b"not json"))

    with pytest.raises(BackendClientError, match="unavailable or returned an invalid response"):
        BackendClient(DashboardSettings()).health()


def test_client_hides_invalid_api_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cloudrca_backend.dashboard.urlopen", lambda *_args, **_kwargs: FakeResponse(b'{"status":"queued"}'))

    with pytest.raises(BackendClientError, match="unexpected response"):
        BackendClient(DashboardSettings()).start_analysis("dataset-1")


def test_client_uses_existing_dataset_job_and_incident_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[Request] = []
    responses = iter(
        (
            b'{"dataset_id":"dataset-1","filename":"events.json","sha256":"hash","validation":{"valid":true}}',
            b'{"job_id":"job-1","status":"queued","created_at":"2026-10-10T00:00:00Z","updated_at":"2026-10-10T00:00:00Z","payload":{}}',
            b'{"items":[],"total":0,"offset":20,"limit":20}',
        )
    )

    def fake_open(request: Request, timeout: float) -> FakeResponse:
        requests.append(request)
        return FakeResponse(next(responses))

    monkeypatch.setattr("cloudrca_backend.dashboard.urlopen", fake_open)
    client = BackendClient(DashboardSettings())

    assert client.upload_dataset('events".json', b'{}', "application/json").dataset_id == "dataset-1"
    assert client.start_analysis("dataset-1").job_id == "job-1"
    assert client.list_incidents({"severity": "high", "layer": "vm_guest_os"}, offset=20).total == 0
    assert requests[0].get_method() == "POST"
    assert requests[0].full_url == "http://127.0.0.1:8000/api/v1/datasets"
    assert b'filename="events_.json"' in (requests[0].data or b"")
    assert requests[1].full_url.endswith("/api/v1/datasets/dataset-1/analysis")
    assert requests[2].full_url.endswith("/api/v1/incidents?severity=high&layer=vm_guest_os&offset=20&limit=20")


def test_local_upload_to_analysis_to_incident_list_flow(tmp_path: Path) -> None:
    with running_api(tmp_path) as base_url:
        client = BackendClient(DashboardSettings(backend_url=base_url))
        dataset = client.upload_dataset("events.json", b'{"event":"ok"}', "application/json")
        client.start_analysis(dataset.dataset_id)
        for _ in range(100):
            incidents = client.list_incidents({"layer": "vm_guest_os"})
            if incidents.items:
                break
            time.sleep(0.01)

    assert incidents.total == 1
    assert incidents.items[0].payload["summary"] == "VM CPU pressure"


def test_dashboard_presentation_helpers_are_consistent() -> None:
    timestamp = datetime(2026, 10, 10, 14, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    assert format_utc(timestamp) == "2026-10-10T09:00:00 UTC"
    assert display_label("in_progress") == "In Progress"
    assert evidence_label(("event-1", "event-2")) == "Evidence: event-1, event-2"
    assert evidence_label(()) == "Evidence: none"
    assert layer_state({"affected_layers": ["database", "vm_guest_os"]}, "database") == "available"
    assert layer_state({}, "host_hypervisor") == "not reported"
    assert severity_tone("critical") == "error"


def test_dashboard_rejects_timestamps_without_timezone() -> None:
    with pytest.raises(ValueError, match="timezone"):
        format_utc(datetime(2026, 10, 10, 9, 0))


def test_streamlit_shell_starts_with_navigation_and_empty_state() -> None:
    app = AppTest.from_file(Path(__file__).parents[2] / "streamlit_app.py").run()

    assert not app.exception
    assert app.sidebar.radio[0].value == "Overview"
    assert app.sidebar.radio[0].options == ["Overview", "Analyze", "Incidents", "Evidence"]
    assert "No analysis is selected" in app.info[0].value


def test_streamlit_analysis_page_has_a_safe_empty_upload_state() -> None:
    app = AppTest.from_file(Path(__file__).parents[2] / "streamlit_app.py").run()
    app.sidebar.radio[0].set_value("Analyze").run()

    assert not app.exception
    assert len(app.file_uploader) == 1
    assert "Upload a dataset" in app.info[0].value


def test_streamlit_analysis_page_explains_partial_completion() -> None:
    app = AppTest.from_file(Path(__file__).parents[2] / "streamlit_app.py").run()
    app.sidebar.radio[0].set_value("Analyze").run()
    app.session_state["job"] = Job(
        job_id="job-1",
        status=JobStatus.PARTIAL,
        created_at=datetime(2026, 10, 10, tzinfo=timezone.utc),
        updated_at=datetime(2026, 10, 10, tzinfo=timezone.utc),
        payload={},
    )
    app.run()

    assert "completed partially" in app.warning[0].value
