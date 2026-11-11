"""Healthy windows end as no_anomaly_detected without a main RCA call; any real anomaly signal still gets one.

The healthy control is a real 21-minute Splitwise EC2 baseline (identifiers replaced; see the fixture README).
Before this change it produced rca_failed: application evidence forced a main RCA call with nothing to explain.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cloudrca_backend.anomaly_signals import CHECKS, anomaly_signals
from cloudrca_backend.api import JobStatus
from cloudrca_backend.normalization import normalize_file
from cloudrca_backend.pipeline import AnalysisAgents, RcaPipeline
from cloudrca_backend.routing import load_routing_config
from cloudrca_backend.specialist_runtime import JCodeTimeoutError

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "tests" / "fixtures" / "splitwise" / "real_baseline_healthy.jsonl"
SYNTHETIC = ROOT / "tests" / "fixtures" / "synthetic"
ROUTING = load_routing_config(ROOT / "config" / "routing" / "aiops2025.yaml")


def baseline_records() -> list[dict[str, Any]]:
    return [json.loads(line) for line in BASELINE.read_text().splitlines()]


def write(tmp_path: Path, records: list[dict[str, Any]]) -> Path:
    path = tmp_path / "dataset.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records))
    return path


def run(path: Path, transport: Any, agents_for: Any) -> tuple[Any, dict[str, Any]]:
    scripted: AnalysisAgents = agents_for(transport)
    result = RcaPipeline(lambda _: (path.name, path.read_bytes()), lambda: scripted, ROUTING)({"dataset_id": "d"})
    [incident] = [payload for kind, payload in result.artifacts if kind.value == "incident"]
    return result, incident


def signals_of(tmp_path: Path, records: list[dict[str, Any]]) -> list[tuple[str, str]]:
    events = normalize_file(write(tmp_path, records)).events
    record_of = {event.event_id: event.provenance.record_id for event in events}
    return [(str(record_of[signal.event_id]), signal.check) for signal in anomaly_signals(events)]


def test_the_real_baseline_carries_no_anomaly_signal(tmp_path: Path) -> None:
    batch = normalize_file(BASELINE)
    assert batch.statistics.accepted == 104 and batch.statistics.rejected == 0
    assert anomaly_signals(batch.events) == ()


def test_a_healthy_baseline_completes_as_no_anomaly_without_a_main_rca_call(tmp_path: Path, scripted_jcode: Any, agents_for: Any) -> None:
    transport = scripted_jcode()
    result, incident = run(BASELINE, transport, agents_for)
    assert result.status is JobStatus.COMPLETED and result.error is None
    assert incident["analysis_outcome"] == "no_anomaly_detected"
    assert transport.prompts["vm"] and transport.prompts["main_rca"] == []  # A2 still looks; no RCA is requested
    assert incident["anomaly_signals"] == {"count": 0, "checks": list(CHECKS), "signals": []}
    assert incident["uncertainty"].startswith("No anomaly detected: Deterministic checks found no anomaly in 104 event(s)")
    assert not [kind for kind, _ in result.artifacts if kind.value == "report"]


@pytest.mark.parametrize(("kind", "change", "check"), [
    ("app.request_summary", {"error_5xx_count": 1}, "http_5xx"),
    ("app.request_summary", {"level": "warning"}, "severity"),
    ("app.pool_snapshot", {"db_pool_checked_out": 5}, "pool_saturated"),
    ("app.pool_snapshot", {"db_pool_checked_out": 5, "db_pool_overflow": 2}, "pool_overflow"),
    ("host.heartbeat", {"load_1m": 2.4}, "load_per_cpu"),
    ("host.heartbeat", {"mem_available_mb": 150.0}, "memory_low"),
    ("host.heartbeat", {"disk_free_mb": 300.0, "disk_total_mb": 8192.0}, "disk_low"),
    ("CPUUtilization", {"value": 96.0}, "cpu_saturated"),
    ("CPUCreditBalance", {"value": 0.0}, "cpu_credits_exhausted"),
    ("CPUSurplusCreditsCharged", {"value": 1.5}, "cpu_surplus_charged"),
    ("StatusCheckFailed_Instance", {"value": 1.0}, "status_check_failed"),
    ("EBSIOBalance%", {"value": 4.0}, "ebs_balance_low"),
])
def test_one_real_anomaly_in_the_baseline_is_found_on_its_own_event(tmp_path: Path, kind: str, change: dict[str, Any], check: str) -> None:
    records = baseline_records()
    target = next(
        record for record in records[40:]
        if record["cloudrca_record"] == kind or record.get("metric_name") == kind
    )
    target.update(change)
    found = signals_of(tmp_path, records)
    assert (target["event_id"], check) in found
    assert {record for record, _ in found} == {target["event_id"]}  # nothing else in the healthy window is flagged


def test_an_application_error_without_a_level_is_still_a_signal(tmp_path: Path) -> None:
    records = baseline_records()
    records.append({
        "cloudrca_record": "app.error", "timestamp": "2026-11-11T17:40:00Z", "source": "splitwise.applog", "event_id": "err-1",
        "reported_by": "application", "service": "splitwise-api", "instance_id": "i-0baseline0000001",
        "error_class": "AttributeError", "message": "'NoneType' object has no attribute 'title'", "status": 500,
    })
    assert signals_of(tmp_path, records) == [("err-1", "application_error")]


def test_a_single_5xx_in_a_healthy_window_still_gets_a_main_rca(tmp_path: Path, scripted_jcode: Any, agents_for: Any) -> None:
    records = baseline_records()
    summary = next(record for record in records[50:] if record["cloudrca_record"] == "app.request_summary")
    summary["error_5xx_count"] = 1
    transport = scripted_jcode()
    result, incident = run(write(tmp_path, records), transport, agents_for)
    assert len(transport.prompts["main_rca"]) == 1
    assert incident["analysis_outcome"] == "rca_reported" and result.status is JobStatus.COMPLETED
    assert incident["anomaly_signals"]["count"] == 1 and incident["anomaly_signals"]["signals"][0]["check"] == "http_5xx"


@pytest.mark.parametrize("fixture", ["splitwise_vm_pressure_db_failure.jsonl", "splitwise_concurrency_sqlite_lock.jsonl"])
def test_genuine_incidents_reach_the_main_rca_even_when_specialists_find_nothing(fixture: str, scripted_jcode: Any, agents_for: Any) -> None:
    path = SYNTHETIC / fixture
    assert anomaly_signals(normalize_file(path).events)
    transport = scripted_jcode({"database": [json.dumps({"findings": []})] * 2})
    _, incident = run(path, transport, agents_for)
    assert incident["analysis_outcome"] == "rca_reported" and len(transport.prompts["main_rca"]) == 1
    assert incident["anomaly_signals"]["count"] > 0


def test_a_failed_specialist_never_lets_a_quiet_window_pass_as_healthy(scripted_jcode: Any, agents_for: Any) -> None:
    transport = scripted_jcode({"vm": [JCodeTimeoutError("late")] * 3})
    result, incident = run(BASELINE, transport, agents_for)
    assert incident["analysis_outcome"] == "specialist_failed" and result.status is JobStatus.FAILED
    assert "found no anomaly" in incident["uncertainty"] and "some specialists failed" in incident["uncertainty"]
    assert transport.prompts["main_rca"] == []
