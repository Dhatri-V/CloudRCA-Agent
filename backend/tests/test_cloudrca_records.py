"""R2: CloudRCA application, database, VM/OS, and EC2 CloudWatch records normalize, route, and keep their evidence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from cloudrca_backend.contracts import Layer, ProvenanceKind
from cloudrca_backend.incident_grouping import GroupingConfig, IncidentGrouper
from cloudrca_backend.normalization import RejectionReason, normalize_batch, normalize_file, read_aiops2025_records
from cloudrca_backend.pipeline import RcaPipeline
from cloudrca_backend.routing import RoutingLayer, load_routing_config, route_and_resolve

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "splitwise" / "cloudrca_records.jsonl"
ROUTING = load_routing_config(ROOT / "config" / "routing" / "aiops2025.yaml")
RECORDS = [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def by_record_id() -> dict[str, Any]:
    batch = normalize_file(FIXTURE)
    assert batch.statistics.accepted == len(RECORDS) and not batch.rejected
    return {event.provenance.record_id: event for event in batch.events}


def normalize_one(record: dict[str, Any], tmp_path: Path) -> Any:
    path = tmp_path / "records.jsonl"
    path.write_text(json.dumps(record))
    return normalize_file(path)


def test_each_record_type_reaches_the_intended_layer() -> None:
    events = by_record_id()
    expected = {
        "metric-cpu-1000": (Layer.VM, RoutingLayer.VM),
        "hb-1000": (Layer.VM, RoutingLayer.VM),
        "summary-1001": (Layer.APPLICATION, RoutingLayer.APPLICATION),
        "err-1001-a": (Layer.DATABASE, RoutingLayer.DATABASE),  # reported by the database: A1
        "err-1001-b": (Layer.APPLICATION, RoutingLayer.APPLICATION),  # reported by the application
    }
    for record_id, (layer, routed) in expected.items():
        assert events[record_id].layer is layer
        assert route_and_resolve(events[record_id], ROUTING).routing.layer is routed


def test_timestamps_identifiers_and_provenance_are_preserved() -> None:
    events = by_record_id()
    database_error = events["err-1001-a"]
    assert database_error.timestamp == datetime(2026, 10, 30, 10, 1, 12, tzinfo=timezone.utc)
    assert database_error.database_id == "splitwise-sqlite" and database_error.vm_id == "i-0synthetic01"
    assert database_error.service is None  # the reporting application is an attribute, not a database component
    attributes = database_error.metadata["source_attributes"]
    assert attributes["reporting_service"] == "splitwise-api"  # type: ignore[index]
    assert {key: attributes[key] for key in ("request_id", "route", "method", "status", "db_wait_ms")} == {  # type: ignore[index]
        "request_id": "req-0017", "route": "/expenses", "method": "POST", "status": 500, "db_wait_ms": 5000.0,
    }
    assert database_error.metadata["cloudwatch"] == {"log_group": "/splitwise/app", "log_stream": "i-0synthetic01"}
    provenance = database_error.provenance
    assert provenance.kind is ProvenanceKind.OBSERVED and provenance.source == "splitwise.applog"
    assert provenance.record_id == "err-1001-a" and provenance.details["event_id"] == "err-1001-a"
    assert provenance.details["raw_record"] == RECORDS[3]

    metric = events["metric-cpu-1000"]
    assert metric.vm_id == "i-0synthetic01"
    assert metric.metadata["source_attributes"]["dimensions"] == {"InstanceId": "i-0synthetic01"}  # type: ignore[index]
    assert metric.message == "AWS/EC2 CPUUtilization Average=34 Percent (period 60s)"
    application_error = events["err-1001-b"]
    assert application_error.service == "splitwise-api" and application_error.metadata["source_attributes"]["request_id"] == "req-0023"  # type: ignore[index]


def test_records_stay_relatable_across_layers_without_cross_layer_component_collisions() -> None:
    routed = tuple(route_and_resolve(event, ROUTING) for event in normalize_file(FIXTURE).events)
    [incident] = IncidentGrouper(GroupingConfig()).group(routed).incidents
    assert sorted(layer.value for layer in incident.affected_layers) == ["application", "database", "vm_guest_os"]
    components = {(item.component_id, item.layer.value) for item in incident.affected_components}
    assert {("splitwise-api", "application"), ("splitwise-sqlite", "database"), ("i-0synthetic01", "vm_guest_os")} <= components
    assert ("splitwise-api", "database") not in components


def test_normalization_preserves_evidence_without_inferring_causes() -> None:
    for event in by_record_id().values():
        assert set(event.metadata) == {"record_kind", "original_timestamp", "original_severity", "fingerprint", "source_attributes", "cloudwatch"}
        assert event.host_id is None  # AWS exposes no hypervisor host; none is invented


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"timestamp": None}, RejectionReason.SCHEMA_VALIDATION_FAILED),
        ({"timestamp": "2026-10-30T10:01:12"}, RejectionReason.INVALID_TIMESTAMP),
        ({"timestamp": "yesterday"}, RejectionReason.INVALID_TIMESTAMP),
        ({"cloudrca_record": "app.mystery"}, RejectionReason.UNSUPPORTED_RECORD),
        ({"database_id": None}, RejectionReason.SCHEMA_VALIDATION_FAILED),
        ({"reported_by": "kernel"}, RejectionReason.SCHEMA_VALIDATION_FAILED),
        ({"status": 999}, RejectionReason.SCHEMA_VALIDATION_FAILED),
        ({"level": "panic"}, RejectionReason.INVALID_SEVERITY),
    ],
)
def test_invalid_error_records_are_rejected_with_explicit_reasons(change: dict[str, Any], reason: RejectionReason, tmp_path: Path) -> None:
    record = {**RECORDS[3], **change}
    batch = normalize_one(record, tmp_path)
    assert batch.statistics.rejected == 1 and batch.rejected[0].reason is reason


@pytest.mark.parametrize(
    ("index", "drop_or_change", "reason"),
    [
        (0, {"dimensions": {"AutoScalingGroupName": "asg"}}, RejectionReason.SCHEMA_VALIDATION_FAILED),
        (0, {"value": "high"}, RejectionReason.SCHEMA_VALIDATION_FAILED),
        (0, {"value": True}, RejectionReason.SCHEMA_VALIDATION_FAILED),
        (2, {"request_count": False}, RejectionReason.SCHEMA_VALIDATION_FAILED),
        (0, {"metric_name": None}, RejectionReason.MISSING_REQUIRED_FIELD),
        (1, {key: None for key in ("load_1m", "cpu_count", "mem_available_mb", "mem_total_mb", "disk_free_mb", "disk_total_mb")}, RejectionReason.SCHEMA_VALIDATION_FAILED),
        (1, {"mem_available_mb": -5}, RejectionReason.SCHEMA_VALIDATION_FAILED),
        (2, {"error_5xx_count": 99}, RejectionReason.SCHEMA_VALIDATION_FAILED),
        (2, {"request_count": -1}, RejectionReason.SCHEMA_VALIDATION_FAILED),
    ],
)
def test_invalid_metric_heartbeat_and_summary_records_are_rejected(
    index: int, drop_or_change: dict[str, Any], reason: RejectionReason, tmp_path: Path
) -> None:
    record = {key: value for key, value in {**RECORDS[index], **drop_or_change}.items() if value is not None}
    batch = normalize_one(record, tmp_path)
    assert batch.statistics.rejected == 1 and batch.rejected[0].reason is reason


def test_missing_required_fields_are_named(tmp_path: Path) -> None:
    record = {key: value for key, value in RECORDS[3].items() if key not in {"instance_id", "error_class"}}
    batch = normalize_one(record, tmp_path)
    assert batch.rejected[0].reason is RejectionReason.MISSING_REQUIRED_FIELD
    assert "instance_id" in batch.rejected[0].detail and "error_class" in batch.rejected[0].detail


def test_duplicate_exports_are_deduplicated_but_distinct_events_are_kept(tmp_path: Path) -> None:
    twin = {**RECORDS[3], "event_id": "err-1001-a-retry"}
    path = tmp_path / "records.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in (RECORDS[3], RECORDS[3], twin)))
    batch = normalize_batch(read_aiops2025_records(path))
    assert batch.statistics.accepted == 2 and batch.statistics.duplicates == 1


def test_aiops2025_records_are_unaffected(tmp_path: Path) -> None:
    fixtures = ROOT / "tests" / "fixtures" / "aiops2025"
    combined = [normalize_file(fixtures / f"{name}.parquet") for name in ("logs", "node_metrics", "pod_metrics")]
    assert [batch.statistics.accepted for batch in combined] == [3, 4, 3]
    assert all(event.provenance.source == "AIOps2025" for batch in combined for event in batch.events)


def test_pipeline_sends_only_database_and_vm_records_to_specialists(scripted_jcode: Any, agents_for: Any) -> None:
    transport = scripted_jcode()
    result = RcaPipeline(lambda _: (FIXTURE.name, FIXTURE.read_bytes()), lambda: agents_for(transport), ROUTING)({"dataset_id": "d"})
    events = {payload["provenance"]["record_id"]: payload["event_id"] for kind, payload in result.artifacts if kind.value == "event"}
    database_prompt_ids = {item["event_id"] for prompt in transport.prompts["database"] for item in prompt["events"]}
    vm_prompt_ids = {item["event_id"] for prompt in transport.prompts["vm"] for item in prompt["events"]}
    assert database_prompt_ids == {events["err-1001-a"]}
    assert vm_prompt_ids == {events["metric-cpu-1000"], events["hb-1000"]}
    [rca_input] = transport.prompts["main_rca"]
    assert {events["summary-1001"], events["err-1001-b"]} <= {item["reference_id"] for item in rca_input["incident"]["source_events"]}
