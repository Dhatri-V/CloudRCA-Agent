"""Deterministic pre-RCA event correlation graph for candidate incidents."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum

import networkx as nx
from pydantic import Field

from .contracts import ContractModel, EvidenceReference, Identifier, Layer
from .incident_grouping import CandidateIncident
from .routing import RoutedEvent


class CorrelationEdgeType(StrEnum):
    TEMPORAL = "temporal_candidate"
    TOPOLOGY = "topology_candidate"
    WEAK = "weak_temporal_candidate"


class CorrelationConfig(ContractModel):
    window_seconds: float = Field(default=300.0, ge=0.0)
    time_weight: float = Field(default=0.4, ge=0.0, le=1.0)
    topology_weight: float = Field(default=0.4, ge=0.0, le=1.0)
    entity_weight: float = Field(default=0.1, ge=0.0, le=1.0)
    semantic_weight: float = Field(default=0.1, ge=0.0, le=1.0)

    def score_weights(self) -> float:
        return self.time_weight + self.topology_weight + self.entity_weight + self.semantic_weight


class CorrelationNode(ContractModel):
    node_id: Identifier
    event_id: Identifier
    timestamp: str
    layer: Layer
    provenance: EvidenceReference


class CorrelationEdge(ContractModel):
    source_id: Identifier
    target_id: Identifier
    edge_type: CorrelationEdgeType
    score: float = Field(ge=0.0, le=1.0)
    time_score: float = Field(ge=0.0, le=1.0)
    topology_score: float = Field(ge=0.0, le=1.0)
    entity_score: float = Field(ge=0.0, le=1.0)
    semantic_score: float = Field(ge=0.0, le=1.0)
    topology_relationship_ids: tuple[Identifier, ...] = ()
    evidence: tuple[EvidenceReference, ...] = Field(min_length=2)
    rationale: tuple[str, ...] = Field(min_length=1)


class CorrelationGraph(ContractModel):
    incident_id: Identifier
    nodes: tuple[CorrelationNode, ...]
    edges: tuple[CorrelationEdge, ...] = ()


class CorrelationInputError(ValueError):
    """Raised when incident membership and supplied event evidence disagree."""


def _ids(event: RoutedEvent) -> set[str]:
    value = event.event
    return {item for item in (value.host_id, value.vm_id, value.pod_id, value.database_id, value.service) if item}


class CrossLayerCorrelator:
    """Build a canonical NetworkX graph; edges are candidates, never causal conclusions."""

    def __init__(self, config: CorrelationConfig) -> None:
        if config.score_weights() > 1.0:
            raise ValueError("correlation score weights must total at most one")
        self._config = config

    def correlate(self, incident: CandidateIncident, events: tuple[RoutedEvent, ...]) -> CorrelationGraph:
        expected = {item.reference_id for item in incident.source_events}
        supplied = {item.event.event_id for item in events}
        if expected != supplied or len(supplied) != len(events):
            raise CorrelationInputError("supplied events must exactly match candidate incident source events")
        ordered = tuple(sorted(events, key=lambda item: (item.event.timestamp, item.event.event_id)))
        graph: nx.Graph[str] = nx.Graph()
        nodes = tuple(
            CorrelationNode(
                node_id=f"event:{item.event.event_id}",
                event_id=item.event.event_id,
                timestamp=item.event.timestamp.isoformat().replace("+00:00", "Z"),
                layer=item.event.layer,
                provenance=next(reference for reference in incident.source_events if reference.reference_id == item.event.event_id),
            )
            for item in ordered
        )
        graph.add_nodes_from(node.node_id for node in nodes)
        edges: list[CorrelationEdge] = []
        window = timedelta(seconds=self._config.window_seconds)
        relationships = {
            frozenset((relationship.source.component_id, relationship.target.component_id)): relationship.relationship_id
            for event in ordered
            for relationship in event.topology.relationships
        }
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 :]:
                delta = right.event.timestamp - left.event.timestamp
                if delta > window:
                    break
                left_ids, right_ids = _ids(left), _ids(right)
                shared = left_ids & right_ids
                topology = tuple(sorted(rel_id for pair, rel_id in relationships.items() if pair & left_ids and pair & right_ids))
                if not shared and not topology:
                    continue
                time_score = 1.0 if not window else 1.0 - (delta / window)
                topology_score, entity_score = (1.0 if topology else 0.0), (1.0 if shared else 0.0)
                semantic_score = 1.0 if left.event.layer is right.event.layer else 0.0
                score = (
                    float(time_score) * self._config.time_weight
                    + topology_score * self._config.topology_weight
                    + entity_score * self._config.entity_weight
                    + semantic_score * self._config.semantic_weight
                )
                source_id, target_id = f"event:{left.event.event_id}", f"event:{right.event.event_id}"
                evidence = tuple(node.provenance for node in nodes if node.node_id in (source_id, target_id))
                edge = CorrelationEdge(
                    source_id=source_id,
                    target_id=target_id,
                    edge_type=CorrelationEdgeType.TOPOLOGY if topology else CorrelationEdgeType.TEMPORAL,
                    score=score,
                    time_score=float(time_score),
                    topology_score=topology_score,
                    entity_score=entity_score,
                    semantic_score=semantic_score,
                    topology_relationship_ids=topology,
                    evidence=evidence,
                    rationale=("within configured UTC window", "validated topology relationship" if topology else "canonical entity match"),
                )
                graph.add_edge(source_id, target_id, edge=edge)
                edges.append(edge)
        return CorrelationGraph(
            incident_id=incident.incident_id,
            nodes=nodes,
            edges=tuple(sorted(edges, key=lambda item: (item.source_id, item.target_id))),
        )
