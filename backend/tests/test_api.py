from pathlib import Path

from cloudrca_backend.api import JobRepository, JobStatus, create_app
from fastapi.testclient import TestClient


def test_health_jobs_and_restart_persistence(tmp_path: Path) -> None:
    database = tmp_path / "api.sqlite3"
    client = TestClient(create_app(database))
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json()["ready"] is True
    created = client.post("/api/v1/analysis-jobs", json={"payload": {"source": "test"}}).json()
    stored = TestClient(create_app(database)).get(f"/api/v1/analysis-jobs/{created['job_id']}").json()
    assert stored["payload"] == {"source": "test"}
    assert stored["status"] == "completed"


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
