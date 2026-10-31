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
