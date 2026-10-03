"""Deterministic preliminary incident grouping tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from cloudrca_backend.contracts import (
    ComponentRef,
    EvidenceReference,
    Layer,
    NormalizedEvent,
    Provenance,
    RelationshipType,
    TopologyRelationship,
)
from cloudrca_backend.incident_grouping import GroupingConfig, GroupingInputError, IncidentGrouper
from cloudrca_backend.orchestration import (
    SpecialistExecution,
    SpecialistStatus,
    SpecialistWorkflowResult,
    WorkflowStatus,
)
from cloudrca_backend.routing import TopologyResolution, load_routing_config, route_and_resolve

UTC = datetime(2025, 1, 1, tzinfo=timezone.utc)
CONFIG = load_routing_config("config/routing/aiops2025.yaml")


def event(event_id: str, layer: Layer, seconds: int, *, host: str | None = None, vm: str | None = None, db: str | None = None):
    value = NormalizedEvent(event_id=event_id, timestamp=UTC + timedelta(seconds=seconds), source="test", layer=layer, severity="error", message="anomaly", host_id=host, vm_id=vm, database_id=db, provenance=Provenance(kind="observed", source="test", record_id=event_id))
    return route_and_resolve(value, CONFIG)


def test_window_boundaries_singletons_and_stable_ids_are_deterministic() -> None:
    grouper = IncidentGrouper(GroupingConfig(adjacency_seconds=60))
    first, second, outside = event("a", Layer.VM, 0, vm="vm-1"), event("b", Layer.VM, 60, vm="vm-1"), event("c", Layer.VM, 121, vm="vm-1")
    result = grouper.group((outside, second, first))
    assert [tuple(item.reference_id for item in incident.source_events) for incident in result.incidents] == [("a", "b"), ("c",)]
    assert result == grouper.group((first, second, outside))
    assert result.incidents[0].incident_id != result.incidents[1].incident_id


def test_transitive_same_entity_and_unrelated_noise_behavior() -> None:
    grouper = IncidentGrouper(GroupingConfig(adjacency_seconds=60))
    related = (event("a", Layer.VM, 0, vm="vm-1"), event("b", Layer.VM, 60, vm="vm-1"), event("c", Layer.VM, 120, vm="vm-1"))
    noise = event("noise", Layer.VM, 30, vm="vm-2")
    result = grouper.group((*related, noise))
    assert sorted(len(item.source_events) for item in result.incidents) == [1, 3]


def topology(event_value, *, vm: str, host: str):
    relationship = TopologyRelationship(relationship_id="vm-host", source=ComponentRef(component_id=vm, kind="vm", layer="vm_guest_os"), target=ComponentRef(component_id=host, kind="host", layer="host_hypervisor"), relationship_type=RelationshipType.RUNS_ON, provenance=Provenance(kind="synthetic", source="test", record_id="vm-host"), evidence=(EvidenceReference(evidence_id="topology-evidence", kind="topology", reference_id="vm-host"),))
    return event_value.model_copy(update={"topology": TopologyResolution(relationships=(relationship,))})


def test_topology_groups_cross_layer_only_when_relationship_exists() -> None:
    grouper = IncidentGrouper(GroupingConfig(adjacency_seconds=60))
    host, vm = event("host", Layer.HYPERVISOR, 0, host="host-1"), event("vm", Layer.VM, 30, vm="vm-1")
    assert len(grouper.group((host, vm)).incidents) == 2
    joined = grouper.group((topology(host, vm="vm-1", host="host-1"), vm))
    assert len(joined.incidents) == 1
    assert joined.incidents[0].rationales[0].topology_relationship_ids == ("vm-host",)


def test_specialist_findings_attach_only_to_their_evidence_group_and_preserve_provenance() -> None:
    first, second = event("a", Layer.DATABASE, 0, db="db-1"), event("b", Layer.DATABASE, 10, db="db-1")
    finding = {"error_type": "connection_failure", "confidence": {"score": 0.8, "level": "medium"}, "observations": [{"evidence_event_ids": ["a", "b"]}]}
    workflow = SpecialistWorkflowResult(run_id="run", session_id="session", incident_id="incident", trace_id="trace", status=WorkflowStatus.SUCCESS, executions=(SpecialistExecution(layer=Layer.DATABASE, specialist="A1", status=SpecialistStatus.SUCCESS, attempt_count=1, output={"findings": [finding]}),))
    result = IncidentGrouper(GroupingConfig(adjacency_seconds=60)).group((first, second), workflow)
    assert result.incidents[0].findings[0].finding["confidence"] == {"score": 0.8, "level": "medium"}
    assert [item.reference_id for item in result.incidents[0].source_events] == ["a", "b"]


def test_empty_duplicate_missing_identifiers_and_invalid_config() -> None:
    grouper = IncidentGrouper(GroupingConfig(adjacency_seconds=0))
    assert grouper.group(()).incidents == ()
    value = event("a", Layer.VM, 0, vm=None)
    assert len(grouper.group((value,)).incidents) == 1
    with pytest.raises(GroupingInputError):
        grouper.group((value, value))
    with pytest.raises(Exception):
        GroupingConfig(adjacency_seconds=-1)
