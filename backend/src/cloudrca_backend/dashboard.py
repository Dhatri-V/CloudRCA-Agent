"""Shared presentation and HTTP client primitives for the local Streamlit dashboard."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .api import Dataset, Job, Page

ModelT = TypeVar("ModelT", bound=BaseModel)


class DashboardSettings(BaseModel):
    """Local dashboard settings, configurable without embedding environment-specific URLs."""

    model_config = ConfigDict(extra="forbid")

    backend_url: str = Field(default="http://127.0.0.1:8000")
    timeout_seconds: float = Field(default=5.0, gt=0, le=60)

    @field_validator("backend_url")
    @classmethod
    def backend_url_must_be_http(cls, value: str) -> str:
        normalized = value.rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("backend_url must be an absolute http or https URL")
        return normalized

    @classmethod
    def from_environment(cls) -> DashboardSettings:
        return cls(backend_url=os.getenv("CLOUDRCA_BACKEND_URL", "http://127.0.0.1:8000"))


class BackendClientError(RuntimeError):
    """A safe, user-displayable failure when the local API is unavailable or malformed."""


class BackendClient:
    """Small client boundary used by Streamlit pages."""

    def __init__(self, settings: DashboardSettings) -> None:
        self._settings = settings

    def health(self) -> dict[str, object]:
        return self.get_json("/healthz")

    def get_json(self, path: str) -> dict[str, object]:
        return self._request_json(Request(self._url(path), headers={"Accept": "application/json"}))

    def upload_dataset(self, filename: str, content: bytes, content_type: str) -> Dataset:
        boundary = f"cloudrca-{uuid4().hex}"
        safe_filename = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
        body = b"".join(
            (
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="file"; filename="{safe_filename}"\r\n'.encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                content,
                f"\r\n--{boundary}--\r\n".encode(),
            )
        )
        request = Request(
            self._url("/api/v1/datasets"),
            data=body,
            headers={"Accept": "application/json", "Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        return self._model(Dataset, request)

    def start_analysis(self, dataset_id: str) -> Job:
        return self._model(Job, Request(self._url(f"/api/v1/datasets/{dataset_id}/analysis"), data=b"", method="POST"))

    def get_job(self, job_id: str) -> Job:
        return self._model(Job, Request(self._url(f"/api/v1/analysis-jobs/{job_id}")))

    def cancel_job(self, job_id: str) -> Job:
        return self._model(Job, Request(self._url(f"/api/v1/analysis-jobs/{job_id}/cancel"), data=b"", method="POST"))

    def retry_job(self, job_id: str) -> Job:
        return self._model(Job, Request(self._url(f"/api/v1/analysis-jobs/{job_id}/retry"), data=b"", method="POST"))

    def list_incidents(self, filters: Mapping[str, str], offset: int = 0, limit: int = 20) -> Page:
        query = {key: value for key, value in filters.items() if value}
        query.update({"offset": str(offset), "limit": str(limit)})
        return self._model(Page, Request(self._url(f"/api/v1/incidents?{urlencode(query)}")))

    def _model(self, model: type[ModelT], request: Request) -> ModelT:
        try:
            return model.model_validate(self._request_json(request))
        except ValidationError as error:
            raise BackendClientError("The CloudRCA backend returned an unexpected response.") from error

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            raise ValueError("API paths must start with '/'")
        return f"{self._settings.backend_url}{path}"

    def _request_json(self, request: Request) -> dict[str, object]:
        try:
            with urlopen(request, timeout=self._settings.timeout_seconds) as response:  # noqa: S310 - configured local API URL
                payload: object = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BackendClientError("The CloudRCA backend is unavailable or returned an invalid response.") from error
        if not isinstance(payload, dict):
            raise BackendClientError("The CloudRCA backend returned an unexpected response.")
        return payload


def format_utc(timestamp: datetime) -> str:
    """Return a consistently labelled UTC timestamp for dashboard evidence."""
    if timestamp.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return timestamp.astimezone(UTC).isoformat().replace("+00:00", " UTC")


def display_label(value: str) -> str:
    """Turn stable API values such as ``in_progress`` into readable labels."""
    return value.replace("_", " ").replace("-", " ").title()


def evidence_label(event_ids: tuple[str, ...]) -> str:
    """Render event evidence consistently without losing the source identifiers."""
    if not event_ids:
        return "Evidence: none"
    return f"Evidence: {', '.join(event_ids)}"


def incident_summary(incident: Mapping[str, object]) -> str:
    """Use the API's supported fields to provide a safe concise incident label."""
    for key in ("probable_cause", "summary", "title", "incident_id"):
        value = incident.get(key)
        if isinstance(value, str) and value:
            return value
    return "Incident details are not yet available."


def severity_tone(severity: str) -> str:
    """Map known severity values to Streamlit's accessible built-in status tones."""
    return {"critical": "error", "high": "warning", "medium": "info", "low": "success"}.get(severity.lower(), "info")
