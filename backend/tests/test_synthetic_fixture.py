"""The labelled synthetic incident fixture normalizes, routes, and groups exactly as its expectation file says."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from cloudrca_backend.incident_grouping import GroupingConfig, IncidentGrouper
from cloudrca_backend.normalization import normalize_file
from cloudrca_backend.routing import load_routing_config, route_and_resolve

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "synthetic" / "redis_vm_cpu_incident.jsonl"
EXPECTED = json.loads((ROOT / "tests" / "fixtures" / "synthetic" / "redis_vm_cpu_incident.expected.json").read_text())


def test_synthetic_fixture_is_labelled() -> None:
    records = [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]
    assert EXPECTED["synthetic"] is True
    assert all(record["message"].startswith("SYNTHETIC:") for record in records if "message" in record)


def test_synthetic_fixture_forms_one_cross_layer_incident_with_the_expected_components() -> None:
    structure = EXPECTED["expected_structure"]
    batch = normalize_file(FIXTURE)
    assert batch.statistics.accepted == structure["normalized_events"] and batch.statistics.rejected == 0
    routed = tuple(route_and_resolve(event, load_routing_config(ROOT / "config" / "routing" / "aiops2025.yaml")) for event in batch.events)
    assert dict(Counter(item.routing.layer.value for item in routed)) == structure["routed"]

    incidents = IncidentGrouper(GroupingConfig()).group(routed).incidents
    assert len(incidents) == structure["incidents"]
    incident = incidents[0]
    assert sorted(layer.value for layer in incident.affected_layers) == structure["incident_layers"]
    components = {item.component_id: item.layer.value for item in incident.affected_components}
    for expected in (EXPECTED["expected_root_cause"], EXPECTED["expected_symptom"]):
        assert components.get(expected["component_id"]) == expected["layer"]
