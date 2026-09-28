"""Tests for the shared, versioned CloudRCA data contracts."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cloudrca_backend.contracts import (
    SCHEMA_DIALECT,
    SCHEMA_MODELS,
    CausalEdge,
    ComponentRef,
    Confidence,
    Correlation,
    EvidenceReference,
    Finding,
    Incident,
    NormalizedEvent,
    Provenance,
    RCAReport,
    RootCauseHypothesis,
    TimelineEntry,
    TimeWindow,
    TopologyRelationship,
    contract_json_schema,
)
from pydantic import ValidationError

UTC_TIME = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)


def provenance(kind: str = "observed") -> Provenance:
    return Provenance(kind=kind, source="aiops2025", record_id="row-1")


def evidence(reference_id: str = "event-1") -> EvidenceReference:
    return EvidenceReference(
        evidence_id=f"evidence-{reference_id}",
        kind="event",
        reference_id=reference_id,
        description="Supporting normalized event",
        provenance=provenance(),
    )


def confidence(score: float = 0.8, level: str = "high") -> Confidence:
    return Confidence(score=score, level=level)


def component(
    component_id: str = "vm-1", kind: str = "vm", layer: str = "vm_guest_os"
) -> ComponentRef:
    return ComponentRef(component_id=component_id, kind=kind, layer=layer)


def finding() -> Finding:
    return Finding(
        finding_id="finding-1",
        layer="vm_guest_os",
        summary="Sustained I/O wait",
        suspected_problem="The VM is waiting on storage",
        confidence=confidence(),
        evidence=(evidence(),),
        event_ids=("event-1",),
        component_ids=("vm-1",),
        time_window=TimeWindow(start=UTC_TIME, end=UTC_TIME + timedelta(minutes=5)),
    )


def test_normalized_event_round_trips_as_json_and_is_frozen() -> None:
    event = NormalizedEvent(
        event_id="event-1",
        timestamp=UTC_TIME,
        source="database.log",
        layer="database",
        severity="error",
        message="Connection timed out",
        vm_id="vm-1",
        database_id="db-1",
        metadata={"attempt": 3},
        provenance=provenance(),
    )

    assert NormalizedEvent.model_validate_json(event.model_dump_json()) == event
    with pytest.raises(ValidationError, match="frozen"):
        event.event_id = "changed"


@pytest.mark.parametrize(
    "timestamp",
    [datetime(2025, 6, 1, 10, 0), datetime(2025, 6, 1, 15, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))],
)
def test_timestamps_must_be_timezone_aware_utc(timestamp: datetime) -> None:
    with pytest.raises(ValidationError, match="timezone-aware UTC|offset \\+00:00"):
        TimeWindow(start=timestamp, end=timestamp)


def test_invalid_enums_and_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        NormalizedEvent(
            event_id="event-1",
            timestamp=UTC_TIME,
            source="database.log",
            layer="network",
            severity="urgent",
            message="Bad input",
            provenance=provenance(),
            unknown="value",
        )


@pytest.mark.parametrize("score", [0.0, 1.0])
def test_confidence_accepts_boundary_scores(score: float) -> None:
    assert Confidence(score=score, level="medium").score == score


@pytest.mark.parametrize("score", [-0.01, 1.01])
def test_confidence_rejects_scores_outside_boundaries(score: float) -> None:
    with pytest.raises(ValidationError):
        Confidence(score=score, level="medium")


@pytest.mark.parametrize("kind", ["observed", "augmented", "synthetic"])
def test_topology_records_real_augmented_and_synthetic_provenance(kind: str) -> None:
    relationship = TopologyRelationship(
        relationship_id=f"topology-{kind}",
        source=component("db-1", "database", "database"),
        target=component(),
        relationship_type="runs_on",
        validity=TimeWindow(start=UTC_TIME, end=UTC_TIME + timedelta(hours=1)),
        provenance=provenance(kind),
        evidence=(evidence("topology-row-1"),),
    )
    assert relationship.provenance.kind.value == kind


def test_topology_rejects_self_relationship() -> None:
    vm = component()
    with pytest.raises(ValidationError, match="must differ"):
        TopologyRelationship(
            relationship_id="topology-1",
            source=vm,
            target=vm,
            relationship_type="runs_on",
            provenance=provenance(),
            evidence=(evidence(),),
        )


def test_topology_rejects_missing_evidence() -> None:
    with pytest.raises(ValidationError):
        TopologyRelationship(
            relationship_id="topology-1",
            source=component("db-1", "database", "database"),
            target=component(),
            relationship_type="runs_on",
            provenance=provenance(),
            evidence=(),
        )


def test_finding_and_correlation_require_evidence() -> None:
    with pytest.raises(ValidationError):
        Finding(
            finding_id="finding-1",
            layer="database",
            summary="Timeouts",
            suspected_problem="Connection exhaustion",
            confidence=confidence(),
            evidence=(),
        )
    correlation = Correlation(
        correlation_id="correlation-1",
        summary="VM pressure preceded database timeouts",
        event_ids=("event-1", "event-2"),
        confidence=confidence(),
        evidence=(evidence(),),
    )
    assert len(correlation.event_ids) == 2


def test_incident_groups_events_components_and_findings() -> None:
    incident = Incident(
        incident_id="incident-1",
        time_window=TimeWindow(start=UTC_TIME, end=UTC_TIME + timedelta(minutes=5)),
        affected_layers={"vm_guest_os", "database"},
        affected_components=(component(),),
        event_ids=("event-1",),
        findings=(finding(),),
        status="investigating",
    )
    assert incident.status.value == "investigating"
    assert {layer.value for layer in incident.affected_layers} == {"vm_guest_os", "database"}


def test_rca_report_models_timeline_causal_chain_alternatives_and_runtime() -> None:
    report = RCAReport(
        incident_id="incident-1",
        probable_root_cause="Host storage contention caused VM I/O wait",
        root_cause_layer="host_hypervisor",
        root_cause_component=component("host-1", "host", "host_hypervisor"),
        timeline=(TimelineEntry(timestamp=UTC_TIME, summary="I/O wait began", event_ids=("event-1",)),),
        causal_chain=(
            CausalEdge(
                cause_id="host-1",
                effect_id="vm-1",
                summary="Storage contention delayed VM operations",
                confidence=confidence(),
                evidence=(evidence(),),
            ),
        ),
        evidence=(evidence(),),
        alternative_hypotheses=(
            RootCauseHypothesis(
                summary="The VM was independently overloaded",
                layer="vm_guest_os",
                component=component(),
                confidence=confidence(0.4, "low"),
                evidence=(evidence(),),
            ),
        ),
        confidence=confidence(),
        uncertainty="Synthetic host telemetry limits certainty.",
        runtime={"provider": "z.ai", "model": "glm-5.3", "harness": "jcode"},
    )
    assert RCAReport.model_validate_json(report.model_dump_json()) == report


def test_hypothesis_rejects_mismatched_component_layer() -> None:
    with pytest.raises(ValidationError, match="must match"):
        RootCauseHypothesis(
            summary="Mismatch",
            layer="database",
            component=component(),
            confidence=confidence(),
            evidence=(evidence(),),
        )


def test_rca_report_rejects_mismatched_root_component_layer() -> None:
    with pytest.raises(ValidationError, match="must match"):
        RCAReport(
            incident_id="incident-1",
            probable_root_cause="Storage contention",
            root_cause_layer="database",
            root_cause_component=component(),
            timeline=(TimelineEntry(timestamp=UTC_TIME, summary="Failure began", event_ids=("event-1",)),),
            causal_chain=(
                CausalEdge(
                    cause_id="vm-1",
                    effect_id="db-1",
                    summary="VM pressure delayed the database",
                    confidence=confidence(),
                    evidence=(evidence(),),
                ),
            ),
            evidence=(evidence(),),
            confidence=confidence(),
            runtime={"provider": "z.ai", "model": "glm-5.3", "harness": "jcode"},
        )


def test_contract_schemas_are_versioned_json_schema_2020_12() -> None:
    for name, model in SCHEMA_MODELS.items():
        schema = contract_json_schema(name, model)
        assert schema["$schema"] == SCHEMA_DIALECT
        assert schema["$id"].endswith(f"/{name}.schema.json")
        assert "schema_version" in schema["properties"]
        persisted = json.loads(Path(f"schemas/v1.0.0/{name}.schema.json").read_text())
        assert persisted == schema
