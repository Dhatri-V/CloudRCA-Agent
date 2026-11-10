"""R7 labelled Splitwise scenarios exercise the deterministic pipeline, not model accuracy."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from cloudrca_backend.api import ArtifactKind, JobStatus
from cloudrca_backend.incident_grouping import GroupingConfig, IncidentGrouper
from cloudrca_backend.normalization import normalize_file
from cloudrca_backend.pipeline import RcaPipeline
from cloudrca_backend.routing import load_routing_config, route_and_resolve

ROOT = Path(__file__).resolve().parents[2]
ROUTING = load_routing_config(ROOT / "config/routing/aiops2025.yaml")
SCENARIOS = (
    "splitwise_vm_pressure_db_failure",
    "splitwise_concurrency_sqlite_lock",
)


@pytest.mark.parametrize("name", SCENARIOS)
def test_r7_scenario_is_labelled_and_forms_one_cross_layer_incident(name: str) -> None:
    expected = json.loads((ROOT / "tests/fixtures/synthetic" / f"{name}.expected.json").read_text())
    assert expected["synthetic"] is True and expected["not_real_world_accuracy_evidence"] is True
    batch = normalize_file(ROOT / "tests/fixtures/synthetic" / f"{name}.jsonl")
    structure = expected["expected_structure"]
    assert batch.statistics.accepted == structure["normalized_events"] and batch.statistics.rejected == 0
    routed = tuple(route_and_resolve(event, ROUTING) for event in batch.events)
    assert dict(Counter(item.routing.layer.value for item in routed)) == structure["routed"]
    [incident] = IncidentGrouper(GroupingConfig()).group(routed).incidents
    assert len(incident.source_events) == structure["normalized_events"]
    assert sorted(layer.value for layer in incident.affected_layers) == structure["incident_layers"]


@pytest.mark.parametrize("name", SCENARIOS)
def test_r7_scenario_pipeline_preserves_event_citations(name: str, scripted_jcode: Any, agents_for: Any) -> None:
    fixture = ROOT / "tests/fixtures/synthetic" / f"{name}.jsonl"
    pipeline = RcaPipeline(lambda dataset_id: (fixture.name, fixture.read_bytes()) if dataset_id == name else None, lambda: agents_for(scripted_jcode()), ROUTING)
    result = pipeline({"dataset_id": name})
    assert result.status is JobStatus.COMPLETED
    artifacts = {kind: [payload for item_kind, payload in result.artifacts if item_kind is kind] for kind in ArtifactKind}
    [incident] = artifacts[ArtifactKind.INCIDENT]
    [report] = artifacts[ArtifactKind.REPORT]
    event_ids = {event["event_id"] for event in artifacts[ArtifactKind.EVENT]}
    assert set(incident["event_ids"]) == event_ids
    report_event_references = {item["reference_id"] for item in report["evidence"]} & event_ids
    assert report_event_references
    assert all({item["reference_id"] for item in edge["evidence"]} & event_ids for edge in report["causal_chain"])
    assert len(artifacts[ArtifactKind.GRAPH]) == 1


def records(name: str) -> list[dict[str, Any]]:
    lines = (ROOT / "tests/fixtures/synthetic" / f"{name}.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def kind(name: str, record_kind: str) -> list[dict[str, Any]]:
    return sorted((item for item in records(name) if item["cloudrca_record"] == record_kind), key=lambda item: item["timestamp"])


def first_error_time(name: str) -> str:
    return min(item["timestamp"] for item in kind(name, "app.error"))


@pytest.mark.parametrize("name", SCENARIOS)
def test_expected_hypothesis_cites_real_records_and_separates_reporter_from_cause(name: str) -> None:
    expected = json.loads((ROOT / "tests/fixtures/synthetic" / f"{name}.expected.json").read_text())
    ids = {item["event_id"] for item in records(name)}
    assert all(item["event_id"] in ids for item in expected["supporting_evidence"])
    assert expected["reporting_component"]["component_id"] != expected["expected_hypothesis"]["component_id"]
    assert expected["contrary_or_competing"] and expected["missing_telemetry"]
    routed = tuple(route_and_resolve(event, ROUTING) for event in normalize_file(ROOT / "tests/fixtures/synthetic" / f"{name}.jsonl").events)
    [incident] = IncidentGrouper(GroupingConfig()).group(routed).incidents
    components = {(item.component_id, item.layer.value) for item in incident.affected_components}
    hypothesis = expected["expected_hypothesis"]
    assert (hypothesis["component_id"], hypothesis["layer"]) in components


def test_both_scenarios_end_in_the_same_symptom() -> None:
    # The database-reported symptom alone cannot distinguish the causes; the preceding evidence must.
    messages = {name: {(item["error_class"], item["message"], item["reported_by"]) for item in kind(name, "app.error")} for name in SCENARIOS}
    assert messages["splitwise_vm_pressure_db_failure"] == messages["splitwise_concurrency_sqlite_lock"]


def test_vm_scenario_pressure_precedes_slowdown_with_unchanged_workload() -> None:
    name = "splitwise_vm_pressure_db_failure"
    first_error = first_error_time(name)
    summaries = kind(name, "app.request_summary")
    heartbeats = kind(name, "host.heartbeat")
    baseline, onset = summaries[:2], summaries[2]
    assert onset["timestamp"] < first_error and onset["error_5xx_count"] == 0
    assert all(abs(item["write_count"] - baseline[0]["write_count"]) <= 2 for item in summaries)  # workload unchanged
    assert all(item["max_in_flight"] <= 5 for item in summaries)
    assert onset["max_db_ms"] > 50 * baseline[0]["max_db_ms"]  # database slows only after pressure begins
    assert heartbeats[2]["load_1m"] > 10 * heartbeats[0]["load_1m"] and heartbeats[2]["mem_available_mb"] < 0.1 * heartbeats[0]["mem_available_mb"]
    credits = [item["value"] for item in kind(name, "aws.cloudwatch.metric") if item["metric_name"] == "CPUCreditBalance"]
    assert credits[0] > 50 and credits[-1] == 0


def test_concurrency_scenario_write_surge_on_a_healthy_vm() -> None:
    name = "splitwise_concurrency_sqlite_lock"
    first_error = first_error_time(name)
    summaries = kind(name, "app.request_summary")
    baseline, onset = summaries[:2], summaries[2]
    assert onset["timestamp"] < first_error and onset["error_5xx_count"] == 0
    assert onset["write_count"] >= 5 * baseline[0]["write_count"] and onset["max_in_flight"] >= 5 * baseline[0]["max_in_flight"]
    assert all(item["load_1m"] <= 1.0 and item["mem_available_mb"] >= 500 for item in kind(name, "host.heartbeat"))
    metrics = kind(name, "aws.cloudwatch.metric")
    assert all(item["value"] <= 30 for item in metrics if item["metric_name"] == "CPUUtilization")
    assert all(item["value"] >= 90 for item in metrics if item["metric_name"] == "CPUCreditBalance")
