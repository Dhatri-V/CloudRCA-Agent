from pathlib import Path

from cloudrca_backend.api import AnalysisResult, ArtifactKind, JobRepository, JobStatus, create_app
from fastapi.testclient import TestClient


def test_jobs_artifacts_uploads_and_restart_persistence(tmp_path: Path) -> None:
    database = tmp_path / "api.sqlite3"
    client = TestClient(create_app(database, jcode_ready=lambda: True, provider_ready=lambda: True))
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"ready": True, "database": True, "jcode": True, "provider": True}
    job_id = client.post("/api/v1/analysis-jobs", json={"payload": {"source": "test"}}).json()["job_id"]
    stored = TestClient(create_app(database)).get(f"/api/v1/analysis-jobs/{job_id}").json()
    assert stored["payload"] == {"source": "test"}
    assert stored["status"] == "completed"
    for kind in ("event", "finding", "incident", "graph", "report"):
        assert client.post(f"/api/v1/analysis-jobs/{job_id}/artifacts?kind={kind}", json={"kind": kind}).status_code == 201
    uploaded = client.post(
        f"/api/v1/analysis-jobs/{job_id}/uploads?filename=logs.txt&content_type=text/plain", content=b"safe logs"
    ).json()
    restored = TestClient(create_app(database))
    assert restored.get(f"/api/v1/analysis-jobs/{job_id}/artifacts").status_code == 200
    download = restored.get(f"/api/v1/analysis-jobs/{job_id}/uploads/{uploaded['upload_id']}")
    assert download.content == b"safe logs"
    assert download.headers["content-type"] == "text/plain; charset=utf-8"


def test_runner_partial_and_readiness_are_safe(tmp_path: Path) -> None:
    database = tmp_path / "api.sqlite3"

    def runner(payload: dict[str, object]) -> AnalysisResult:
        return AnalysisResult(JobStatus.PARTIAL, ((ArtifactKind.FINDING, {"safe": True}),))

    client = TestClient(create_app(database, runner, jcode_ready=lambda: False, provider_ready=lambda: True))
    assert client.get("/readyz").json() == {"ready": False, "database": True, "jcode": False, "provider": True}
    job = client.post("/api/v1/analysis-jobs", json={}).json()
    assert client.get(f"/api/v1/analysis-jobs/{job['job_id']}").json()["status"] == "partial"
    assert client.get(f"/api/v1/analysis-jobs/{job['job_id']}/artifacts").json()[0]["kind"] == "finding"


def test_job_transitions_are_valid(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "api.sqlite3")
    job = repository.create({})
    repository.transition(job.job_id, JobStatus.RUNNING)
    assert repository.transition(job.job_id, JobStatus.FAILED, "safe failure").status is JobStatus.FAILED
    try:
        repository.transition(job.job_id, JobStatus.RUNNING)
    except ValueError as error:
        assert "invalid transition" in str(error)
    else:
        raise AssertionError("terminal jobs cannot resume")
