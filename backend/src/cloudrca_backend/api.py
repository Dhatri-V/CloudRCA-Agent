"""Versioned FastAPI MVP with durable SQLite analysis-job state."""

from __future__ import annotations

import enum
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = {JobStatus.COMPLETED, JobStatus.PARTIAL, JobStatus.FAILED, JobStatus.CANCELLED}
TRANSITIONS = {JobStatus.QUEUED: {JobStatus.RUNNING, JobStatus.CANCELLED}, JobStatus.RUNNING: TERMINAL}


class CreateJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payload: dict[str, object] = Field(default_factory=dict)


class Job(BaseModel):
    job_id: str
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    payload: dict[str, object]
    error: str | None = None


class JobRepository:
    def __init__(self, database: Path) -> None:
        self.database = database
        database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
            connection.execute("INSERT OR IGNORE INTO schema_version VALUES (1)")
            connection.execute("""CREATE TABLE IF NOT EXISTS analysis_jobs (
                job_id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, payload TEXT NOT NULL, error TEXT)""")

    def create(self, payload: dict[str, object]) -> Job:
        now, job_id = datetime.now(UTC), str(uuid4())
        with self._connect() as connection:
            connection.execute("INSERT INTO analysis_jobs VALUES (?, ?, ?, ?, ?, ?)", (job_id, JobStatus.QUEUED.value, now.isoformat(), now.isoformat(), json.dumps(payload), None))
        return Job(job_id=job_id, status=JobStatus.QUEUED, created_at=now, updated_at=now, payload=payload)

    def get(self, job_id: str) -> Job | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM analysis_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def transition(self, job_id: str, status: JobStatus, error: str | None = None) -> Job:
        current = self.get(job_id)
        if current is None:
            raise KeyError(job_id)
        if status not in TRANSITIONS.get(current.status, set()):
            raise ValueError(f"invalid transition {current.status.value} -> {status.value}")
        now = datetime.now(UTC)
        with self._connect() as connection:
            connection.execute("UPDATE analysis_jobs SET status = ?, updated_at = ?, error = ? WHERE job_id = ?", (status.value, now.isoformat(), error, job_id))
        return self.get(job_id) or current

    def ready(self) -> bool:
        try:
            with self._connect() as connection:
                return connection.execute("SELECT version FROM schema_version").fetchone() == (1,)
        except sqlite3.Error:
            return False

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database)

    @staticmethod
    def _job(row: tuple[object, ...]) -> Job:
        return Job(job_id=str(row[0]), status=JobStatus(str(row[1])), created_at=datetime.fromisoformat(str(row[2])), updated_at=datetime.fromisoformat(str(row[3])), payload=json.loads(str(row[4])), error=str(row[5]) if row[5] is not None else None)


def create_app(database: Path = Path("data/cloudrca.sqlite3")) -> FastAPI:
    repository = JobRepository(database)
    app = FastAPI(title="CloudRCA API", version="1.0.0")

    def run(job_id: str) -> None:
        try:
            repository.transition(job_id, JobStatus.RUNNING)
            repository.transition(job_id, JobStatus.COMPLETED)
        except (KeyError, ValueError):
            return

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readiness() -> dict[str, object]:
        return {"ready": repository.ready(), "database": repository.ready(), "jcode": "not_checked", "provider": "not_checked"}

    @app.post("/api/v1/analysis-jobs", status_code=202, response_model=Job)
    def create_job(request: CreateJob, tasks: BackgroundTasks) -> Job:
        job = repository.create(request.payload)
        tasks.add_task(run, job.job_id)
        return job

    @app.get("/api/v1/analysis-jobs/{job_id}", response_model=Job)
    def get_job(job_id: str) -> Job:
        job = repository.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="analysis job not found")
        return job

    return app
