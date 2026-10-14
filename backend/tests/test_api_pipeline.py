"""FastAPI job runner wiring: real pipeline execution, artifact persistence, and failure handling."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from cloudrca_backend import api
from cloudrca_backend.api import AnalysisResult, ArtifactKind, JobRepository, create_app
from cloudrca_backend.pipeline import RcaPipeline
from cloudrca_backend.routing import load_routing_config
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROUTING_PATH = Path(__file__).resolve().parents[2] / "config" / "routing" / "aiops2025.yaml"


def analyze(client: TestClient, content: bytes, filename: str = "aiops.jsonl") -> dict[str, Any]:
    dataset = client.post("/api/v1/datasets", files={"file": (filename, content, "application/json")}).json()
    job = client.post(f"/api/v1/datasets/{dataset['dataset_id']}/analysis").json()
    result: dict[str, Any] = client.get(f"/api/v1/analysis-jobs/{job['job_id']}").json()
    return result


def test_uploaded_dataset_runs_pipeline_and_artifacts_are_retrievable(
    tmp_path: Path, aiops_jsonl: bytes, scripted_jcode: Any, agents_for: Any
) -> None:
    database = tmp_path / "api.sqlite3"
    repository = JobRepository(database)
    transport = scripted_jcode()
    runner = RcaPipeline(repository.dataset_content, lambda: agents_for(transport), load_routing_config(ROUTING_PATH))
    client = TestClient(create_app(database, runner))

    job = analyze(client, aiops_jsonl)
    assert job["status"] == "completed" and job["error"] is None
    kinds = [item["kind"] for item in client.get(f"/api/v1/analysis-jobs/{job['job_id']}/artifacts").json()]
    assert kinds.count("run") == 1 and kinds.count("event") == 10 and kinds.count("incident") == 3

    restarted = TestClient(create_app(database))  # persisted in SQLite, readable after a restart
    incidents = restarted.get("/api/v1/incidents").json()
    assert incidents["total"] == 3
    reported = [item for item in incidents["items"] if item["payload"]["analysis_outcome"] == "rca_reported"]
    assert len(reported) == 2
    detail = restarted.get(f"/api/v1/incidents/{reported[0]['artifact_id']}").json()
    assert detail["job_id"] == job["job_id"]
    assert restarted.get("/api/v1/incidents", params={"layer": "vm_guest_os"}).json()["total"] == 1
    assert restarted.get("/api/v1/findings").json()["total"] == 2
    assert restarted.get("/api/v1/evidence").json()["total"] == 10

    artifacts = restarted.get(f"/api/v1/analysis-jobs/{job['job_id']}/artifacts").json()
    report = next(item for item in artifacts if item["kind"] == "report")
    fetched = restarted.get(f"/api/v1/reports/{report['artifact_id']}").json()
    event_ids = {item["payload"]["event_id"] for item in artifacts if item["kind"] == "event"}
    assert {item["reference_id"] for item in fetched["payload"]["evidence"]} & event_ids
    graph = next(item for item in artifacts if item["kind"] == "graph")
    assert restarted.get(f"/api/v1/graphs/{graph['artifact_id']}").json()["payload"]["incident_id"]


def test_failed_pipeline_job_records_reason(tmp_path: Path, scripted_jcode: Any, agents_for: Any) -> None:
    database = tmp_path / "api.sqlite3"
    repository = JobRepository(database)
    runner = RcaPipeline(repository.dataset_content, lambda: agents_for(scripted_jcode()), load_routing_config(ROUTING_PATH))
    job = analyze(TestClient(create_app(database, runner)), b'{"event": "ok"}', "events.json")
    assert job["status"] == "failed"
    assert job["error"] == "no supported events after normalization (UNSUPPORTED_RECORD=1)"


@pytest.mark.parametrize("error", [ValueError("bad"), KeyError("missing"), RuntimeError("provider said sk-secret")])
def test_runner_exceptions_fail_the_job_instead_of_leaving_it_running(tmp_path: Path, error: Exception) -> None:
    def runner(payload: dict[str, object]) -> AnalysisResult:
        raise error

    client = TestClient(create_app(tmp_path / "api.sqlite3", runner))
    job = client.post("/api/v1/analysis-jobs", json={}).json()
    stored = client.get(f"/api/v1/analysis-jobs/{job['job_id']}").json()
    assert stored["status"] == "failed" and stored["error"] == "analysis job failed"


def test_artifact_persistence_is_atomic(tmp_path: Path) -> None:
    def runner(payload: dict[str, object]) -> AnalysisResult:
        return AnalysisResult(artifacts=((ArtifactKind.EVENT, {"ok": True}), (ArtifactKind.REPORT, {"bad": object()})))

    client = TestClient(create_app(tmp_path / "api.sqlite3", runner))
    job = client.post("/api/v1/analysis-jobs", json={}).json()
    assert client.get(f"/api/v1/analysis-jobs/{job['job_id']}").json()["status"] == "failed"
    assert client.get(f"/api/v1/analysis-jobs/{job['job_id']}/artifacts").json() == []


def test_results_are_discarded_when_job_is_cancelled_while_running(tmp_path: Path) -> None:
    database = tmp_path / "api.sqlite3"

    def runner(payload: dict[str, object]) -> AnalysisResult:
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE analysis_jobs SET status = 'cancelled' WHERE status = 'running'")
        return AnalysisResult(artifacts=((ArtifactKind.INCIDENT, {"late": True}),))

    client = TestClient(create_app(database, runner))
    job = client.post("/api/v1/analysis-jobs", json={}).json()
    assert client.get(f"/api/v1/analysis-jobs/{job['job_id']}").json()["status"] == "cancelled"
    assert client.get(f"/api/v1/analysis-jobs/{job['job_id']}/artifacts").json() == []


def test_incident_filters_consider_more_than_one_hundred_incidents(tmp_path: Path) -> None:
    incidents = tuple((ArtifactKind.INCIDENT, {"layer": "database" if index == 150 else "vm_guest_os"}) for index in range(151))
    client = TestClient(create_app(tmp_path / "api.sqlite3", lambda payload: AnalysisResult(artifacts=incidents)))
    client.post("/api/v1/analysis-jobs", json={})
    assert client.get("/api/v1/incidents", params={"layer": "database"}).json()["total"] == 1
    assert client.get("/api/v1/incidents").json()["total"] == 151


def test_cli_entry_point_runs_the_production_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, aiops_jsonl: bytes) -> None:
    captured: dict[str, FastAPI] = {}
    monkeypatch.setenv("CLOUDRCA_API_DATABASE", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("CLOUDRCA_ROUTING_CONFIG", str(ROUTING_PATH))
    for name in ("CLOUDRCA_PROFILE", "CLOUDRCA_DATA_DIR", "CLOUDRCA_KNOWLEDGE_DIR", "CLOUDRCA_JCODE_BINARY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(api.uvicorn, "run", lambda app, **options: captured.setdefault("app", app))
    api.main()
    job = analyze(TestClient(captured["app"]), aiops_jsonl)
    # Without LLM configuration the real pipeline fails the job explicitly rather than reporting stub success.
    assert job["status"] == "failed"
    assert str(job["error"]).startswith("LLM runtime is not configured: Missing required configuration")
