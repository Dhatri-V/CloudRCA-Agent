"""End-to-end flows through the HTTP API with the production pipeline and a scripted LLM boundary."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cloudrca_backend.api import JobRepository, create_app
from cloudrca_backend.pipeline import AnalysisAgents, RcaPipeline
from cloudrca_backend.routing import load_routing_config
from cloudrca_backend.settings import SettingsError
from fastapi.testclient import TestClient

ROUTING = load_routing_config(Path(__file__).resolve().parents[2] / "config" / "routing" / "aiops2025.yaml")


def app(database: Path, factory: Callable[[], AnalysisAgents]) -> TestClient:
    return TestClient(create_app(database, RcaPipeline(JobRepository(database).dataset_content, factory, ROUTING)))


def upload(client: TestClient, filename: str, content: bytes, key: str | None = None) -> str:
    headers = {"Idempotency-Key": key} if key else {}
    response = client.post("/api/v1/datasets", files={"file": (filename, content, "application/json")}, headers=headers)
    assert response.status_code == 201, response.text
    return str(response.json()["dataset_id"])


def job_artifacts(client: TestClient, job_id: str, kind: str) -> list[dict[str, Any]]:
    return [item for item in client.get(f"/api/v1/analysis-jobs/{job_id}/artifacts").json() if item["kind"] == kind]


def test_json_array_dataset_flows_from_upload_to_report(tmp_path: Path, aiops_jsonl: bytes, scripted_jcode: Any, agents_for: Any) -> None:
    client = app(tmp_path / "api.sqlite3", lambda: agents_for(scripted_jcode()))
    records = [json.loads(line) for line in aiops_jsonl.decode().splitlines()]
    dataset_id = upload(client, "aiops.json", json.dumps(records).encode())
    assert client.get(f"/api/v1/datasets/{dataset_id}/validation").json()["record_count"] == 14

    job = client.post(f"/api/v1/datasets/{dataset_id}/analysis").json()
    job = client.get(f"/api/v1/analysis-jobs/{job['job_id']}").json()
    assert job["status"] == "completed"

    run = job_artifacts(client, job["job_id"], "run")[0]["payload"]
    assert run["normalization"]["accepted"] == 10 and run["outcomes"] == {"insufficient_evidence": 1, "rca_reported": 2}
    events = {item["payload"]["event_id"]: item["payload"] for item in job_artifacts(client, job["job_id"], "event")}
    for report in job_artifacts(client, job["job_id"], "report"):
        fetched = client.get(f"/api/v1/reports/{report['artifact_id']}").json()["payload"]
        cited = [item["reference_id"] for item in fetched["evidence"] if item["kind"] == "event"]
        assert cited and all(events[event_id]["provenance"]["source"] == "AIOps2025" for event_id in cited)
        assert all(events[event_id]["provenance"]["details"]["source_file"] == "aiops.json" for event_id in cited)


def test_failed_job_can_be_retried_once_llm_runtime_is_configured(
    tmp_path: Path, aiops_jsonl: bytes, scripted_jcode: Any, agents_for: Any
) -> None:
    configured = {"ready": False}

    def factory() -> AnalysisAgents:
        if not configured["ready"]:
            raise SettingsError("Missing required configuration: CLOUDRCA_PROFILE")
        return agents_for(scripted_jcode())

    client = app(tmp_path / "api.sqlite3", factory)
    dataset_id = upload(client, "aiops.jsonl", aiops_jsonl)
    failed = client.post(f"/api/v1/datasets/{dataset_id}/analysis").json()
    failed = client.get(f"/api/v1/analysis-jobs/{failed['job_id']}").json()
    assert failed["status"] == "failed" and "CLOUDRCA_PROFILE" in failed["error"]
    assert not job_artifacts(client, failed["job_id"], "report")

    configured["ready"] = True
    retried = client.post(f"/api/v1/analysis-jobs/{failed['job_id']}/retry").json()
    assert retried["job_id"] != failed["job_id"]
    retried = client.get(f"/api/v1/analysis-jobs/{retried['job_id']}").json()
    assert retried["status"] == "completed" and retried["payload"]["dataset_id"] == dataset_id
    assert len(job_artifacts(client, retried["job_id"], "report")) == 2


def test_idempotent_upload_and_analysis_do_not_reprocess(tmp_path: Path, aiops_jsonl: bytes, scripted_jcode: Any, agents_for: Any) -> None:
    transport = scripted_jcode()
    client = app(tmp_path / "api.sqlite3", lambda: agents_for(transport))
    dataset_id = upload(client, "aiops.jsonl", aiops_jsonl, key="upload-1")
    assert upload(client, "aiops.jsonl", aiops_jsonl, key="upload-1") == dataset_id
    headers = {"Idempotency-Key": "analysis-1"}
    first = client.post(f"/api/v1/datasets/{dataset_id}/analysis", headers=headers).json()
    calls = sum(len(prompts) for prompts in transport.prompts.values())
    second = client.post(f"/api/v1/datasets/{dataset_id}/analysis", headers=headers).json()
    assert second["job_id"] == first["job_id"]
    assert sum(len(prompts) for prompts in transport.prompts.values()) == calls
    assert client.get("/api/v1/incidents").json()["total"] == 3


def test_jobs_keep_their_own_evidence(tmp_path: Path, aiops_jsonl: bytes, scripted_jcode: Any, agents_for: Any) -> None:
    client = app(tmp_path / "api.sqlite3", lambda: agents_for(scripted_jcode()))
    logs_only = b"\n".join(line for line in aiops_jsonl.splitlines() if b"@timestamp" in line)
    first = client.post(f"/api/v1/datasets/{upload(client, 'all.jsonl', aiops_jsonl)}/analysis").json()["job_id"]
    second = client.post(f"/api/v1/datasets/{upload(client, 'logs.jsonl', logs_only)}/analysis").json()["job_id"]
    assert len(job_artifacts(client, first, "incident")) == 3
    assert len(job_artifacts(client, second, "incident")) == 1
    for job_id in (first, second):
        events = {item["payload"]["event_id"] for item in job_artifacts(client, job_id, "event")}
        for incident in job_artifacts(client, job_id, "incident"):
            assert set(incident["payload"]["event_ids"]) <= events


def test_unusable_upload_is_rejected_or_failed_explicitly(tmp_path: Path, scripted_jcode: Any, agents_for: Any) -> None:
    client = app(tmp_path / "api.sqlite3", lambda: agents_for(scripted_jcode()))
    assert client.post("/api/v1/datasets", files={"file": ("empty.jsonl", b"", "application/json")}).status_code == 422
    assert client.post("/api/v1/datasets", files={"file": ("bad.jsonl", b"{oops", "application/json")}).status_code == 422
    dataset_id = upload(client, "app.jsonl", b'{"level": "error", "message": "application log without AIOps2025 shape"}')
    job = client.post(f"/api/v1/datasets/{dataset_id}/analysis").json()
    job = client.get(f"/api/v1/analysis-jobs/{job['job_id']}").json()
    assert job["status"] == "failed"
    assert job["error"] == "no supported events after normalization (UNSUPPORTED_RECORD=1)"
    assert client.get("/api/v1/incidents").json()["total"] == 0
