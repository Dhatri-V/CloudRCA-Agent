"""Shared presentation and HTTP client primitives for the local Streamlit dashboard."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    """Small read-only client boundary used by Streamlit pages."""

    def __init__(self, settings: DashboardSettings) -> None:
        self._settings = settings

    def health(self) -> dict[str, object]:
        return self.get_json("/healthz")

    def get_json(self, path: str) -> dict[str, object]:
        if not path.startswith("/"):
            raise ValueError("API paths must start with '/'")
        request = Request(f"{self._settings.backend_url}{path}", headers={"Accept": "application/json"})
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


def severity_tone(severity: str) -> str:
    """Map known severity values to Streamlit's accessible built-in status tones."""
    return {"critical": "error", "high": "warning", "medium": "info", "low": "success"}.get(severity.lower(), "info")
