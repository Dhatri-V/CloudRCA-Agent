"""Versioned FastAPI API with durable SQLite analysis jobs and artifacts."""

from __future__ import annotations

import enum
import hashlib
import json
import os
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import structlog
import uvicorn
from fastapi import BackgroundTasks, Body, FastAPI, File, Header, HTTPException, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field

logger = structlog.get_logger(__name__)
SCHEMA_VERSION = 2


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ArtifactKind(str, enum.Enum):
    RUN = "run"
    EVENT = "event"
    FINDING = "finding"
    INCIDENT = "incident"
    GRAPH = "graph"
    REPORT = "report"
    UPLOAD = "upload"


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


class Artifact(BaseModel):
    artifact_id: str
    job_id: str
    kind: ArtifactKind
    payload: dict[str, object]


class Upload(BaseModel):
    upload_id: str
    job_id: str
    filename: str
    content_type: str
    size: int
    sha256: str


class Dataset(BaseModel):
    dataset_id: str
    filename: str
    sha256: str
    validation: dict[str, object]


@dataclass(frozen=True)
class AnalysisResult:
    status: JobStatus = JobStatus.COMPLETED
    artifacts: tuple[tuple[ArtifactKind, dict[str, object]], ...] = ()


AnalysisRunner = Callable[[dict[str, object]], AnalysisResult]
CapabilityProbe = Callable[[], bool]


def default_runner(payload: dict[str, object]) -> AnalysisResult:
    """Record a run boundary; production analysis is injected by the application host."""
    return AnalysisResult(artifacts=((ArtifactKind.RUN, {"status": "completed"}),))


class JobRepository:
    def __init__(self, database: Path) -> None:
        self.database = database
        database.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def migrate(self) -> None:
        with self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS analysis_jobs (job_id TEXT PRIMARY KEY, status TEXT NOT NULL, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, payload TEXT NOT NULL, error TEXT)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS analysis_artifacts (artifact_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, "
                "kind TEXT NOT NULL, payload TEXT NOT NULL, FOREIGN KEY(job_id) REFERENCES analysis_jobs(job_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS analysis_uploads (upload_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, "
                "filename TEXT NOT NULL, content_type TEXT NOT NULL, content BLOB NOT NULL, sha256 TEXT NOT NULL, "
                "FOREIGN KEY(job_id) REFERENCES analysis_jobs(job_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS datasets (dataset_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, "
                "filename TEXT NOT NULL, content BLOB NOT NULL, sha256 TEXT NOT NULL, validation TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS analysis_requests (idempotency_key TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, job_id TEXT NOT NULL)"
            )
            connection.execute("INSERT OR REPLACE INTO schema_version VALUES (?)", (SCHEMA_VERSION,))

    def create(self, payload: dict[str, object]) -> Job:
        now, job_id = datetime.now(UTC), str(uuid4())
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO analysis_jobs VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, JobStatus.QUEUED.value, now.isoformat(), now.isoformat(), json.dumps(payload), None),
            )
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
            connection.execute(
                "UPDATE analysis_jobs SET status = ?, updated_at = ?, error = ? WHERE job_id = ?",
                (status.value, now.isoformat(), error, job_id),
            )
        logger.info("analysis_job_transitioned", job_id=job_id, status=status.value)
        return self.get(job_id) or current

    def save_artifact(self, job_id: str, kind: ArtifactKind, payload: dict[str, object]) -> Artifact:
        if self.get(job_id) is None:
            raise KeyError(job_id)
        artifact = Artifact(artifact_id=str(uuid4()), job_id=job_id, kind=kind, payload=payload)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO analysis_artifacts VALUES (?, ?, ?, ?)",
                (artifact.artifact_id, job_id, kind.value, json.dumps(payload)),
            )
        return artifact

    def artifacts(self, job_id: str) -> tuple[Artifact, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM analysis_artifacts WHERE job_id = ? ORDER BY artifact_id", (job_id,)
            ).fetchall()
        return tuple(
            Artifact(artifact_id=str(row[0]), job_id=str(row[1]), kind=ArtifactKind(str(row[2])), payload=json.loads(str(row[3])))
            for row in rows
        )

    def save_upload(self, job_id: str, filename: str, content_type: str, content: bytes) -> Upload:
        if self.get(job_id) is None:
            raise KeyError(job_id)
        if not filename or "\r" in filename or "\n" in filename:
            raise ValueError("invalid upload filename")
        upload = Upload(
            upload_id=str(uuid4()), job_id=job_id, filename=filename, content_type=content_type,
            size=len(content), sha256=hashlib.sha256(content).hexdigest(),
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO analysis_uploads VALUES (?, ?, ?, ?, ?, ?)",
                (upload.upload_id, job_id, filename, content_type, content, upload.sha256),
            )
        self.save_artifact(job_id, ArtifactKind.UPLOAD, upload.model_dump())
        return upload

    def upload_content(self, job_id: str, upload_id: str) -> tuple[Upload, bytes] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT upload_id, job_id, filename, content_type, content, sha256 FROM analysis_uploads "
                "WHERE job_id = ? AND upload_id = ?",
                (job_id, upload_id),
            ).fetchone()
        if row is None:
            return None
        content = bytes(row[4])
        return Upload(
            upload_id=str(row[0]), job_id=str(row[1]), filename=str(row[2]), content_type=str(row[3]),
            size=len(content), sha256=str(row[5]),
        ), content

    def ready(self) -> bool:
        try:
            with self._connect() as connection:
                return connection.execute("SELECT version FROM schema_version").fetchone() == (SCHEMA_VERSION,)
        except sqlite3.Error:
            return False

    def create_dataset(self, filename: str, content: bytes, idempotency_key: str | None) -> Dataset:
        if idempotency_key:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT dataset_id, filename, sha256, validation FROM datasets WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
            if row:
                return Dataset(dataset_id=str(row[0]), filename=str(row[1]), sha256=str(row[2]), validation=json.loads(str(row[3])))
        validation = validate_dataset(filename, content)
        if not validation["valid"]:
            raise ValueError(str(validation["error"]))
        dataset = Dataset(dataset_id=str(uuid4()), filename=filename, sha256=hashlib.sha256(content).hexdigest(), validation=validation)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO datasets VALUES (?, ?, ?, ?, ?, ?)",
                (dataset.dataset_id, idempotency_key, filename, content, dataset.sha256, json.dumps(validation)),
            )
        return dataset

    def dataset(self, dataset_id: str) -> Dataset | None:
        with self._connect() as connection:
            row = connection.execute("SELECT dataset_id, filename, sha256, validation FROM datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
        return Dataset(dataset_id=str(row[0]), filename=str(row[1]), sha256=str(row[2]), validation=json.loads(str(row[3]))) if row else None

    def analysis_request(self, dataset_id: str, idempotency_key: str | None) -> Job:
        if idempotency_key:
            with self._connect() as connection:
                row = connection.execute("SELECT dataset_id, job_id FROM analysis_requests WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if row:
                if str(row[0]) != dataset_id:
                    raise ValueError("idempotency key belongs to another dataset")
                return self.get(str(row[1])) or self.create({})
        if self.dataset(dataset_id) is None:
            raise KeyError(dataset_id)
        job = self.create({"dataset_id": dataset_id, "stage": "queued"})
        if idempotency_key:
            with self._connect() as connection:
                connection.execute("INSERT INTO analysis_requests VALUES (?, ?, ?)", (idempotency_key, dataset_id, job.job_id))
        return job

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _job(row: tuple[object, ...]) -> Job:
        return Job(
            job_id=str(row[0]), status=JobStatus(str(row[1])), created_at=datetime.fromisoformat(str(row[2])),
            updated_at=datetime.fromisoformat(str(row[3])), payload=json.loads(str(row[4])),
            error=str(row[5]) if row[5] is not None else None,
        )


def configured_jcode() -> bool:
    return shutil.which(os.getenv("CLOUDRCA_JCODE_BINARY", "jcode")) is not None


def configured_provider() -> bool:
    return bool(os.getenv(os.getenv("CLOUDRCA_GLM_API_KEY_ENV", "ZHIPU_API_KEY")))


def validate_dataset(filename: str, content: bytes) -> dict[str, object]:
    if len(content) > 10 * 1024 * 1024:
        return {"valid": False, "error": "dataset exceeds 10 MiB limit"}
    suffix = Path(filename).suffix.lower()
    if suffix not in {".json", ".jsonl"}:
        return {"valid": False, "error": "supported formats are .json and .jsonl"}
    try:
        if suffix == ".json":
            records = json.loads(content)
            count = len(records) if isinstance(records, list) else 1 if isinstance(records, dict) else 0
        else:
            records = [json.loads(line) for line in content.splitlines() if line.strip()]
            count = len(records)
        if not count or not all(isinstance(record, dict) for record in records if isinstance(records, list)):
            raise ValueError("records must be JSON objects")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        return {"valid": False, "error": f"invalid dataset: {error}"}
    return {"valid": True, "format": suffix.removeprefix("."), "record_count": count, "size_bytes": len(content)}


def create_app(
    database: Path = Path("data/cloudrca.sqlite3"),
    runner: AnalysisRunner = default_runner,
    jcode_ready: CapabilityProbe = configured_jcode,
    provider_ready: CapabilityProbe = configured_provider,
) -> FastAPI:
    repository = JobRepository(database)
    app = FastAPI(title="CloudRCA API", version="1.0.0")

    def run(job_id: str) -> None:
        try:
            job = repository.transition(job_id, JobStatus.RUNNING)
            result = runner(job.payload)
            for kind, payload in result.artifacts:
                repository.save_artifact(job_id, kind, payload)
            repository.transition(job_id, result.status)
        except (KeyError, ValueError):
            return
        except Exception:
            logger.error("analysis_job_failed", job_id=job_id)
            repository.transition(job_id, JobStatus.FAILED, "analysis job failed")

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readiness() -> dict[str, bool]:
        database_ready, jcode, provider = repository.ready(), jcode_ready(), provider_ready()
        return {"ready": database_ready and jcode and provider, "database": database_ready, "jcode": jcode, "provider": provider}

    @app.post("/api/v1/analysis-jobs", status_code=202, response_model=Job)
    def create_job(request: CreateJob, tasks: BackgroundTasks) -> Job:
        job = repository.create(request.payload)
        tasks.add_task(run, job.job_id)
        return job

    @app.post("/api/v1/datasets", status_code=201, response_model=Dataset)
    async def upload_dataset(
        file: UploadFile = File(), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")
    ) -> Dataset:
        try:
            return repository.create_dataset(file.filename or "upload", await file.read(), idempotency_key)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.get("/api/v1/datasets/{dataset_id}/validation", response_model=dict[str, object])
    def validation_summary(dataset_id: str) -> dict[str, object]:
        dataset = repository.dataset(dataset_id)
        if dataset is None:
            raise HTTPException(status_code=404, detail="dataset not found")
        return dataset.validation

    @app.post("/api/v1/datasets/{dataset_id}/analysis", status_code=202, response_model=Job)
    def analyze_dataset(dataset_id: str, tasks: BackgroundTasks, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> Job:
        try:
            job = repository.analysis_request(dataset_id, idempotency_key)
        except KeyError:
            raise HTTPException(status_code=404, detail="dataset not found") from None
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        if job.status is JobStatus.QUEUED:
            tasks.add_task(run, job.job_id)
        return job

    @app.post("/api/v1/analysis-jobs/{job_id}/retry", status_code=202, response_model=Job)
    def retry_job(job_id: str, tasks: BackgroundTasks) -> Job:
        previous = repository.get(job_id)
        if previous is None:
            raise HTTPException(status_code=404, detail="analysis job not found")
        if previous.status not in {JobStatus.FAILED, JobStatus.CANCELLED}:
            raise HTTPException(status_code=409, detail="analysis job cannot be retried")
        job = repository.create(previous.payload)
        tasks.add_task(run, job.job_id)
        return job

    @app.get("/api/v1/analysis-jobs/{job_id}", response_model=Job)
    def get_job(job_id: str) -> Job:
        job = repository.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="analysis job not found")
        return job

    @app.post("/api/v1/analysis-jobs/{job_id}/cancel", response_model=Job)
    def cancel_job(job_id: str) -> Job:
        try:
            return repository.transition(job_id, JobStatus.CANCELLED)
        except KeyError:
            raise HTTPException(status_code=404, detail="analysis job not found") from None
        except ValueError:
            raise HTTPException(status_code=409, detail="analysis job cannot be cancelled") from None

    @app.post("/api/v1/analysis-jobs/{job_id}/artifacts", status_code=201, response_model=Artifact)
    def save_artifact(job_id: str, kind: ArtifactKind, payload: dict[str, object]) -> Artifact:
        try:
            return repository.save_artifact(job_id, kind, payload)
        except KeyError:
            raise HTTPException(status_code=404, detail="analysis job not found") from None

    @app.get("/api/v1/analysis-jobs/{job_id}/artifacts", response_model=tuple[Artifact, ...])
    def list_artifacts(job_id: str) -> tuple[Artifact, ...]:
        if repository.get(job_id) is None:
            raise HTTPException(status_code=404, detail="analysis job not found")
        return repository.artifacts(job_id)

    @app.post("/api/v1/analysis-jobs/{job_id}/uploads", status_code=201, response_model=Upload)
    def save_upload(
        job_id: str, filename: str, content: bytes = Body(), content_type: str = "application/octet-stream"
    ) -> Upload:
        try:
            return repository.save_upload(job_id, filename, content_type, content)
        except KeyError:
            raise HTTPException(status_code=404, detail="analysis job not found") from None
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid upload filename") from None

    @app.get("/api/v1/analysis-jobs/{job_id}/uploads/{upload_id}")
    def get_upload(job_id: str, upload_id: str) -> Response:
        stored = repository.upload_content(job_id, upload_id)
        if stored is None:
            raise HTTPException(status_code=404, detail="upload not found")
        upload, content = stored
        return Response(content=content, media_type=upload.content_type, headers={"Content-Disposition": f'attachment; filename="{upload.filename}"'})

    return app


def main() -> None:
    database = Path(os.getenv("CLOUDRCA_API_DATABASE", "data/cloudrca.sqlite3"))
    uvicorn.run(create_app(database), host="127.0.0.1", port=8000)
