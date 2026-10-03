"""Deterministic candidate-incident windowing over routed events and specialist findings."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import timedelta
from enum import StrEnum

from pydantic import Field, JsonValue

from .contracts import (
    ComponentKind,
    ComponentRef,
    ContractModel,
    EvidenceKind,
    EvidenceReference,
    Identifier,
    Layer,
    TimeWindow,
)
from .orchestration import SpecialistStatus, SpecialistWorkflowResult
from .routing import RoutedEvent


class GroupingReasonType(StrEnum):
    SINGLETON = "singleton_anomaly"
    SAME_ENTITY = "same_entity_within_window"
    TOPOLOGY = "topology_related_within_window"


class GroupingConfig(ContractModel):
    """Evaluation-visible, inclusive adjacency threshold for point events."""

    adjacency_seconds: float = Field(default=300.0, ge=0.0)


class GroupingRationale(ContractModel):
    reason: GroupingReasonType
    event_ids: tuple[Identifier, ...] = Field(min_length=1)
    topology_relationship_ids: tuple[Identifier, ...] = ()


class AttachedSpecialistFinding(ContractModel):
    layer: Layer
    specialist: Identifier
    finding_id: Identifier
    finding: dict[str, JsonValue]


class CandidateIncident(ContractModel):
    incident_id: Identifier
    time_window: TimeWindow
    source_events: tuple[EvidenceReference, ...] = Field(min_length=1)
    affected_components: tuple[ComponentRef, ...] = ()
    affected_layers: frozenset[Layer] = Field(min_length=1)
    findings: tuple[AttachedSpecialistFinding, ...] = ()
    rationales: tuple[GroupingRationale, ...] = Field(min_length=1)


class CandidateIncidentSet(ContractModel):
    incidents: tuple[CandidateIncident, ...] = ()


class GroupingInputError(ValueError):
    """Raised for duplicate or otherwise invalid grouping input."""


def _stable_id(prefix: str, value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(encoded).hexdigest()}"


def _components(event: RoutedEvent) -> tuple[ComponentRef, ...]:
    source = event.event
    components: list[ComponentRef] = []
    if source.host_id:
        components.append(ComponentRef(component_id=source.host_id, kind=ComponentKind.HOST, layer=Layer.HYPERVISOR))
    if source.vm_id:
        components.append(ComponentRef(component_id=source.vm_id, kind=ComponentKind.VM, layer=Layer.VM))
    if source.pod_id:
        components.append(ComponentRef(component_id=source.pod_id, kind=ComponentKind.POD, layer=Layer.DATABASE))
    if source.database_id:
        components.append(ComponentRef(component_id=source.database_id, kind=ComponentKind.DATABASE, layer=Layer.DATABASE))
    if source.service:
        components.append(ComponentRef(component_id=source.service, kind=ComponentKind.SERVICE, layer=source.layer))
    return tuple(components)


def _topology_graph(events: tuple[RoutedEvent, ...]) -> tuple[dict[str, set[str]], dict[frozenset[str], set[str]]]:
    graph: dict[str, set[str]] = defaultdict(set)
    relationship_ids: dict[frozenset[str], set[str]] = defaultdict(set)
    for event in events:
        for relationship in event.topology.relationships:
            left, right = relationship.source.component_id, relationship.target.component_id
            graph[left].add(right)
            graph[right].add(left)
            relationship_ids[frozenset((left, right))].add(relationship.relationship_id)
    return graph, relationship_ids


def _topology_connected(left: set[str], right: set[str], graph: dict[str, set[str]]) -> bool:
    for start in left:
        seen = {start}
        pending = [start]
        while pending:
            current = pending.pop()
            if current in right:
                return True
            for adjacent in graph.get(current, set()) - seen:
                seen.add(adjacent)
                pending.append(adjacent)
    return False


def _finding_event_ids(value: object) -> set[str]:
    if isinstance(value, dict):
        ids = set(value.get("evidence_event_ids", [])) if isinstance(value.get("evidence_event_ids"), list) else set()
        return ids | set().union(*(_finding_event_ids(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_finding_event_ids(item) for item in value)) if value else set()
    return set()


class IncidentGrouper:
    """Rule-only grouping; no LLM or similarity clustering is used."""

    def __init__(self, config: GroupingConfig) -> None:
        self._config = config

    def group(
        self, events: tuple[RoutedEvent, ...], workflow: SpecialistWorkflowResult | None = None
    ) -> CandidateIncidentSet:
        if not events:
            return CandidateIncidentSet()
        ordered = tuple(sorted(events, key=lambda item: (item.event.timestamp, item.event.event_id)))
        event_ids = [item.event.event_id for item in ordered]
        if len(event_ids) != len(set(event_ids)):
            raise GroupingInputError("grouping input event IDs must be unique")
        graph, relationship_ids = _topology_graph(ordered)
        parents = list(range(len(ordered)))
        reasons: dict[tuple[int, int], GroupingRationale] = {}

        def root(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        def union(left: int, right: int) -> None:
            left_root, right_root = root(left), root(right)
            if left_root != right_root:
                parents[max(left_root, right_root)] = min(left_root, right_root)

        threshold = timedelta(seconds=self._config.adjacency_seconds)
        for left in range(len(ordered)):
            left_components = {item.component_id for item in _components(ordered[left])}
            for right in range(left + 1, len(ordered)):
                if ordered[right].event.timestamp - ordered[left].event.timestamp > threshold:
                    break
                right_components = {item.component_id for item in _components(ordered[right])}
                shared = left_components & right_components
                if shared:
                    reasons[left, right] = GroupingRationale(
                        reason=GroupingReasonType.SAME_ENTITY,
                        event_ids=(ordered[left].event.event_id, ordered[right].event.event_id),
                    )
                    union(left, right)
                elif left_components and right_components and _topology_connected(left_components, right_components, graph):
                    supporting = tuple(sorted({relationship_id for ids in relationship_ids.values() for relationship_id in ids}))
                    reasons[left, right] = GroupingRationale(
                        reason=GroupingReasonType.TOPOLOGY,
                        event_ids=(ordered[left].event.event_id, ordered[right].event.event_id),
                        topology_relationship_ids=supporting,
                    )
                    union(left, right)
        groups: dict[int, list[int]] = defaultdict(list)
        for index in range(len(ordered)):
            groups[root(index)].append(index)
        candidates = tuple(self._candidate(tuple(ordered[index] for index in indices), reasons, indices, workflow) for indices in groups.values())
        return CandidateIncidentSet(incidents=tuple(sorted(candidates, key=lambda item: (item.time_window.start, item.incident_id))))

    def _candidate(
        self,
        events: tuple[RoutedEvent, ...],
        edge_reasons: dict[tuple[int, int], GroupingRationale],
        indices: list[int],
        workflow: SpecialistWorkflowResult | None,
    ) -> CandidateIncident:
        ids = tuple(sorted(item.event.event_id for item in events))
        source_events = tuple(
            EvidenceReference(
                evidence_id=_stable_id("evidence", {"event_id": item.event.event_id}),
                kind=EvidenceKind.EVENT,
                reference_id=item.event.event_id,
                provenance=item.event.provenance,
            )
            for item in sorted(events, key=lambda item: item.event.event_id)
        )
        components = {item.component_id: item for event in events for item in _components(event)}
        rationales = [edge_reasons[key] for key in edge_reasons if key[0] in indices and key[1] in indices]
        if not rationales:
            rationales = [GroupingRationale(reason=GroupingReasonType.SINGLETON, event_ids=ids)]
        return CandidateIncident(
            incident_id=_stable_id("incident", {"event_ids": ids}),
            time_window=TimeWindow(start=min(item.event.timestamp for item in events), end=max(item.event.timestamp for item in events)),
            source_events=source_events,
            affected_components=tuple(sorted(components.values(), key=lambda item: (item.layer.value, item.kind.value, item.component_id))),
            affected_layers=frozenset(item.event.layer for item in events),
            findings=self._findings(ids, workflow),
            rationales=tuple(sorted(rationales, key=lambda item: (item.reason.value, item.event_ids))),
        )

    @staticmethod
    def _findings(event_ids: tuple[str, ...], workflow: SpecialistWorkflowResult | None) -> tuple[AttachedSpecialistFinding, ...]:
        if workflow is None:
            return ()
        attached: list[AttachedSpecialistFinding] = []
        event_set = set(event_ids)
        for execution in workflow.executions:
            if execution.status is not SpecialistStatus.SUCCESS or execution.output is None:
                continue
            findings = execution.output.get("findings", [])
            if not isinstance(findings, list):
                continue
            for finding in findings:
                if not isinstance(finding, dict):
                    continue
                evidence_ids = _finding_event_ids(finding)
                if evidence_ids and evidence_ids.issubset(event_set):
                    payload = dict(finding)
                    attached.append(
                        AttachedSpecialistFinding(
                            layer=execution.layer,
                            specialist=execution.specialist,
                            finding_id=_stable_id("specialist-finding", {"layer": execution.layer.value, "finding": payload}),
                            finding=payload,
                        )
                    )
        return tuple(sorted(attached, key=lambda item: item.finding_id))
