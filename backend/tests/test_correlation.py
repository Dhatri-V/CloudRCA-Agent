"""Deterministic pre-RCA correlation graph tests."""

from datetime import datetime, timedelta, timezone

import pytest
from cloudrca_backend.contracts import Layer, NormalizedEvent, Provenance
from cloudrca_backend.correlation import CorrelationConfig, CorrelationInputError, CrossLayerCorrelator
from cloudrca_backend.incident_grouping import GroupingConfig, IncidentGrouper
from cloudrca_backend.routing import load_routing_config, route_and_resolve

UTC = datetime(2025, 1, 1, tzinfo=timezone.utc)
CONFIG = load_routing_config("config/routing/aiops2025.yaml")


def event(event_id: str, seconds: int, *, vm: str | None = "vm-1"):
    value = NormalizedEvent(event_id=event_id, timestamp=UTC + timedelta(seconds=seconds), source="node", layer=Layer.VM, severity="error", vm_id=vm, message="cpu pressure", provenance=Provenance(kind="observed", source="test", record_id=event_id))
    return route_and_resolve(value, CONFIG)


def test_timeline_edges_boundary_and_deterministic_order() -> None:
    first, second = event("b", 0), event("a", 60)
    incident = IncidentGrouper(GroupingConfig(adjacency_seconds=60)).group((first, second)).incidents[0]
    correlator = CrossLayerCorrelator(CorrelationConfig(window_seconds=60))
    graph = correlator.correlate(incident, (second, first))
    assert [node.event_id for node in graph.nodes] == ["b", "a"]
    assert graph.edges[0].time_score == 0
    assert graph == correlator.correlate(incident, (first, second))


def test_out_of_window_and_missing_entity_do_not_fabricate_edges() -> None:
    first, second = event("a", 0), event("b", 61)
    incident = IncidentGrouper(GroupingConfig(adjacency_seconds=100)).group((first, second)).incidents[0]
    assert CrossLayerCorrelator(CorrelationConfig(window_seconds=60)).correlate(incident, (first, second)).edges == ()
    alone = event("c", 0, vm=None)
    singleton = IncidentGrouper(GroupingConfig()).group((alone,)).incidents[0]
    assert CrossLayerCorrelator(CorrelationConfig()).correlate(singleton, (alone,)).edges == ()


def test_invalid_configuration_and_membership_are_rejected() -> None:
    with pytest.raises(ValueError):
        CrossLayerCorrelator(CorrelationConfig(time_weight=1, topology_weight=1))
    source = event("a", 0)
    incident = IncidentGrouper(GroupingConfig()).group((source,)).incidents[0]
    with pytest.raises(CorrelationInputError):
        CrossLayerCorrelator(CorrelationConfig()).correlate(incident, (event("other", 0),))
