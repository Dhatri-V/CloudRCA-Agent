from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request

import pytest
from cloudrca_backend.dashboard import (
    BackendClient,
    BackendClientError,
    DashboardSettings,
    display_label,
    evidence_label,
    format_utc,
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


def test_dashboard_presentation_helpers_are_consistent() -> None:
    timestamp = datetime(2026, 10, 10, 14, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    assert format_utc(timestamp) == "2026-10-10T09:00:00 UTC"
    assert display_label("in_progress") == "In Progress"
    assert evidence_label(("event-1", "event-2")) == "Evidence: event-1, event-2"
    assert evidence_label(()) == "Evidence: none"
    assert severity_tone("critical") == "error"


def test_dashboard_rejects_timestamps_without_timezone() -> None:
    with pytest.raises(ValueError, match="timezone"):
        format_utc(datetime(2026, 10, 10, 9, 0))


def test_streamlit_shell_starts_with_navigation_and_empty_state() -> None:
    app = AppTest.from_file(Path(__file__).parents[2] / "streamlit_app.py").run()

    assert not app.exception
    assert app.sidebar.radio[0].value == "Overview"
    assert "No analysis is selected" in app.info[0].value
