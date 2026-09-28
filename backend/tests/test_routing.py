"""Golden routing and evidence-constrained topology tests."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from cloudrca_backend.contracts import Layer, NormalizedEvent, Provenance, ProvenanceKind
from cloudrca_backend.normalization import normalize_file
from cloudrca_backend.routing import (
    RoutingConfig,
    RoutingLayer,
    TopologyAugmentation,
    load_routing_config,
    resolve_topology,
    route_and_resolve,
    route_event,
)
from pydantic import ValidationError

ROOT = Path(__file__).parents[2]
ROUTING_CONFIG = ROOT / "config" / "routing" / "aiops2025.yaml"
GOLDEN = Path(__file__).parent / "fixtures" / "routing" / "golden.yaml"
AIOPS_FIXTURES = ROOT / "tests" / "fixtures" / "aiops2025"
UTC_TIME = datetime(2025, 4, 29, 6, 3, 8, tzinfo=timezone.utc)


@pytest.fixture
def config() -> RoutingConfig:
    return load_routing_config(ROUTING_CONFIG)


def event(**changes: object) -> NormalizedEvent:
    values: dict[str, object] = {
        "event_id": "event-1",
        "timestamp": UTC_TIME,
        "source": "custom-telemetry",
        "layer": Layer.DATABASE,
        "severity": "unknown",
        "message": "component event",
        "provenance": Provenance(kind="observed", source="AIOps2025"),
    }
    values.update(changes)
    return NormalizedEvent.model_validate(values)


def test_golden_routes_cover_all_layers_unknown_and_ambiguous(config: RoutingConfig) -> None:
    fixture = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
    for case in fixture["cases"]:
        decision = route_event(event(**case["event"]), config)
        assert decision.layer.value == case["expected_layer"], case["name"]
        assert decision.confidence == case["expected_confidence"], case["name"]
        assert decision.explanation


def test_exact_metadata_takes_precedence_over_message_fallback(config: RoutingConfig) -> None:
    decision = route_event(event(service="redis", message="hypervisor failure"), config)
    assert decision.layer is RoutingLayer.DATABASE
    assert decision.confidence == config.confidence.exact_metadata
    assert decision.matches[0].field == "service"


def test_exact_identifier_takes_precedence_over_metadata(config: RoutingConfig) -> None:
    decision = route_event(event(vm_id="vm-1", service="redis"), config)
    assert decision.layer is RoutingLayer.VM
    assert decision.confidence == config.confidence.exact_identifier
    assert "VM identifier" in decision.explanation


def test_configured_fallback_has_lower_confidence(config: RoutingConfig) -> None:
    decision = route_event(event(message="Database connection failed"), config)
    assert decision.layer is RoutingLayer.DATABASE
    assert decision.confidence == config.confidence.fallback_pattern
    assert "fallback pattern" in decision.explanation


def test_dataset_aliases_route_tidb_tikv_and_node(config: RoutingConfig) -> None:
    assert route_event(event(service="tidb"), config).layer is RoutingLayer.DATABASE
    assert route_event(event(source="aiops2025.metric.tikv"), config).layer is RoutingLayer.DATABASE
    assert route_event(event(source="aiops2025.metric.node"), config).layer is RoutingLayer.VM


def test_new_yaml_alias_needs_no_core_code_change(config: RoutingConfig) -> None:
    raw = config.model_dump(mode="json")
    raw["layers"]["database"]["aliases"].append("new-database")
    changed = RoutingConfig.model_validate(raw)
    assert route_event(event(service="new-database"), changed).layer is RoutingLayer.DATABASE


def test_conflicting_equal_strength_evidence_remains_ambiguous(config: RoutingConfig) -> None:
    decision = route_event(event(service="redis", source="libvirt"), config)
    assert decision.layer is RoutingLayer.AMBIGUOUS
    assert decision.candidate_layers == (Layer.DATABASE, Layer.HYPERVISOR)
    assert "multiple layers" in decision.explanation


def test_unknown_event_remains_observable(config: RoutingConfig) -> None:
    routed = route_and_resolve(event(), config)
    assert routed.event.event_id == "event-1"
    assert routed.routing.layer is RoutingLayer.UNKNOWN
    assert routed.routing.confidence == 0.0


def test_repeated_routing_is_deterministic(config: RoutingConfig) -> None:
    candidate = event(service="redis", database_id="redis-cart-0", vm_id="vm-worker-08")
    assert route_and_resolve(candidate, config) == route_and_resolve(candidate, config)


def test_issue_5_normalized_redis_event_routes_without_conversion(config: RoutingConfig) -> None:
    batch = normalize_file(AIOPS_FIXTURES / "logs.parquet")
    routed = route_and_resolve(batch.events[0], config)
    assert routed.event is batch.events[0]
    assert routed.routing.layer is RoutingLayer.DATABASE
    assert routed.topology.relationships[0].provenance.kind is ProvenanceKind.OBSERVED


def test_real_redis_pod_to_worker_topology_is_observed(config: RoutingConfig) -> None:
    candidate = event(
        service="redis",
        database_id="redis-cart-0",
        pod_id="redis-cart-0",
        vm_id="vm-worker-08",
        metadata={
            "component_pod_association": {
                "component": "redis",
                "pod_id": "redis-cart-0",
                "basis": "same_source_record",
            }
        },
    )
    routed = route_and_resolve(candidate, config)
    relationship = routed.topology.relationships[0]
    assert relationship.source.component_id == "redis-cart-0"
    assert relationship.target.component_id == "vm-worker-08"
    assert relationship.provenance.kind is ProvenanceKind.OBSERVED
    assert routed.topology.unavailable[0].target_kind.value == "host"


@pytest.mark.parametrize("service", ["tidb", "tikv", "pd"])
def test_unproven_database_placement_is_explicitly_unavailable(
    service: str, config: RoutingConfig
) -> None:
    candidate = event(service=service, database_id=f"{service}-endpoint")
    routed = route_and_resolve(candidate, config)
    assert routed.topology.relationships == ()
    assert routed.topology.unavailable[0].target_kind.value == "vm"
    assert "No validated" in routed.topology.unavailable[0].reason


def test_real_vm_to_host_mapping_is_explicitly_unavailable(config: RoutingConfig) -> None:
    candidate = event(source="aiops2025.metric.node", vm_id="vm-worker-03")
    decision = route_event(candidate, config)
    topology = resolve_topology(candidate, decision)
    assert topology.relationships == ()
    assert topology.unavailable[0].target_kind.value == "host"
    assert "does not provide real" in topology.unavailable[0].reason


def test_non_aiops_redis_metadata_does_not_claim_observed_topology(config: RoutingConfig) -> None:
    candidate = event(
        service="redis",
        database_id="redis-cart-0",
        pod_id="redis-cart-0",
        vm_id="vm-worker-08",
        metadata={
            "component_pod_association": {
                "component": "redis",
                "pod_id": "redis-cart-0",
                "basis": "same_source_record",
            }
        },
        provenance=Provenance(kind="observed", source="another-dataset"),
    )
    routed = route_and_resolve(candidate, config)
    assert routed.topology.relationships == ()
    assert routed.topology.unavailable[0].target_kind.value == "vm"


def test_independent_redis_and_pod_fields_do_not_create_observed_topology(
    config: RoutingConfig,
) -> None:
    candidate = event(
        service="redis",
        database_id="redis",
        pod_id="unrelated-pod-0",
        vm_id="vm-worker-08",
    )
    routed = route_and_resolve(candidate, config)
    assert routed.topology.relationships == ()
    assert routed.topology.unavailable[0].target_kind.value == "vm"


@pytest.mark.parametrize("kind", [ProvenanceKind.AUGMENTED, ProvenanceKind.SYNTHETIC])
def test_explicit_host_augmentation_preserves_provenance(
    kind: ProvenanceKind, config: RoutingConfig
) -> None:
    candidate = event(source="aiops2025.metric.node", vm_id="vm-worker-03")
    augmentation = TopologyAugmentation(
        augmentation_id=f"augmentation-{kind.value}",
        vm_id="vm-worker-03",
        host_id="demo-host-01",
        provenance_kind=kind,
        scenario_id="demo-scenario",
        generator_version="1.0.0",
    )
    routed = route_and_resolve(candidate, config, augmentations=(augmentation,))
    relationship = routed.topology.relationships[0]
    assert relationship.target.component_id == "demo-host-01"
    assert relationship.provenance.kind is kind
    assert relationship.provenance.details["scenario_id"] == "demo-scenario"


def test_long_augmentation_id_produces_bounded_stable_evidence_id(config: RoutingConfig) -> None:
    candidate = event(source="aiops2025.metric.node", vm_id="vm-worker-03")
    augmentation = TopologyAugmentation(
        augmentation_id="a" * 256,
        vm_id="vm-worker-03",
        host_id="demo-host-01",
        provenance_kind="synthetic",
        scenario_id="demo-scenario",
        generator_version="1.0.0",
    )
    routed = route_and_resolve(candidate, config, augmentations=(augmentation,))
    evidence_id = routed.topology.relationships[0].evidence[0].evidence_id
    assert len(evidence_id) <= 256
    assert evidence_id == routed.topology.relationships[0].evidence[0].evidence_id


def test_augmentation_cannot_claim_observed_provenance() -> None:
    with pytest.raises(ValidationError, match="cannot be marked observed"):
        TopologyAugmentation(
            augmentation_id="bad",
            vm_id="vm-1",
            host_id="host-1",
            provenance_kind="observed",
            scenario_id="demo",
            generator_version="1",
        )
