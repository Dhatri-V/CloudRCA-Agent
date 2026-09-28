"""Deterministic layer routing and evidence-constrained topology resolution."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Mapping

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import (
    ComponentKind,
    ComponentRef,
    EvidenceKind,
    EvidenceReference,
    Layer,
    NormalizedEvent,
    Provenance,
    ProvenanceKind,
    RelationshipType,
    TimeWindow,
    TopologyRelationship,
)

ConfidenceScore = Annotated[float, Field(ge=0.0, le=1.0)]


class RoutingLayer(StrEnum):
    DATABASE = "database"
    VM = "vm_guest_os"
    HYPERVISOR = "host_hypervisor"
    UNKNOWN = "unknown"
    AMBIGUOUS = "ambiguous"


class RoutingRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    aliases: tuple[str, ...] = ()
    fallback_patterns: tuple[str, ...] = ()

    @model_validator(mode="after")
    def values_must_compile_and_not_be_blank(self) -> RoutingRule:
        if any(not alias.strip() for alias in self.aliases):
            raise ValueError("routing aliases must not be blank")
        for pattern in self.fallback_patterns:
            re.compile(pattern)
        return self


class ConfidenceRules(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    exact_identifier: ConfidenceScore = 1.0
    exact_metadata: ConfidenceScore = 0.95
    fallback_pattern: ConfidenceScore = 0.55


class RoutingConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: Annotated[int, Field(ge=1)]
    confidence: ConfidenceRules
    layers: dict[Layer, RoutingRule]

    @model_validator(mode="after")
    def all_layers_must_be_configured(self) -> RoutingConfig:
        if set(self.layers) != set(Layer):
            raise ValueError("routing configuration must define database, vm_guest_os, and host_hypervisor")
        return self


class RoutingMatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    layer: Layer
    field: str
    value: str
    rule: str
    precedence: Annotated[int, Field(ge=1)]
    confidence: ConfidenceScore


class RoutingDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    layer: RoutingLayer
    confidence: ConfidenceScore
    explanation: str = Field(min_length=1)
    candidate_layers: tuple[Layer, ...] = ()
    matches: tuple[RoutingMatch, ...] = ()


class UnavailableTopology(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source: ComponentRef
    target_kind: ComponentKind
    relationship_type: RelationshipType
    reason: str = Field(min_length=1)


class TopologyResolution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    relationships: tuple[TopologyRelationship, ...] = ()
    unavailable: tuple[UnavailableTopology, ...] = ()


class RoutedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    event: NormalizedEvent
    routing: RoutingDecision
    topology: TopologyResolution


class TopologyAugmentation(BaseModel):
    """An explicit non-observed VM-to-host placement supplied by a scenario."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    augmentation_id: str = Field(min_length=1, max_length=256)
    vm_id: str = Field(min_length=1, max_length=256)
    host_id: str = Field(min_length=1, max_length=256)
    provenance_kind: ProvenanceKind
    scenario_id: str = Field(min_length=1, max_length=256)
    generator_version: str = Field(min_length=1, max_length=256)
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    @model_validator(mode="after")
    def augmentation_must_not_claim_observation(self) -> TopologyAugmentation:
        if self.provenance_kind is ProvenanceKind.OBSERVED:
            raise ValueError("topology augmentation cannot be marked observed")
        if (self.valid_from is None) != (self.valid_to is None):
            raise ValueError("augmentation validity requires both start and end")
        valid_from, valid_to = self.valid_from, self.valid_to
        if valid_from is not None and valid_to is not None:
            TimeWindow(start=valid_from, end=valid_to)
        return self


def load_routing_config(path: str | Path) -> RoutingConfig:
    """Load validated routing rules from YAML."""
    source = Path(path)
    parsed: Any = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("routing configuration must be a YAML mapping")
    return RoutingConfig.model_validate(parsed)


def _normalized_tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", value.lower()))


def _alias_matches(value: str, alias: str) -> bool:
    value_tokens = _normalized_tokens(value)
    alias_tokens = _normalized_tokens(alias)
    if not alias_tokens or len(alias_tokens) > len(value_tokens):
        return False
    width = len(alias_tokens)
    return any(value_tokens[index : index + width] == alias_tokens for index in range(len(value_tokens) - width + 1))


def _string_metadata(event: NormalizedEvent) -> tuple[tuple[str, str], ...]:
    fields: list[tuple[str, str]] = []
    if event.service:
        fields.append(("service", event.service))
    fields.append(("source", event.source))
    attributes = event.metadata.get("source_attributes")
    if isinstance(attributes, dict):
        for key, value in sorted(attributes.items()):
            if isinstance(value, str) and value.strip():
                fields.append((f"metadata.source_attributes.{key}", value))
    if event.pod_id:
        fields.append(("pod_id", event.pod_id))
    return tuple(fields)


def _identifier_matches(event: NormalizedEvent, config: RoutingConfig) -> list[RoutingMatch]:
    matches: list[RoutingMatch] = []
    if event.database_id:
        matches.append(
            RoutingMatch(
                layer=Layer.DATABASE,
                field="database_id",
                value=event.database_id,
                rule="database identifier present",
                precedence=300,
                confidence=config.confidence.exact_identifier,
            )
        )
        return matches
    if event.host_id:
        matches.append(
            RoutingMatch(
                layer=Layer.HYPERVISOR,
                field="host_id",
                value=event.host_id,
                rule="host identifier present",
                precedence=300,
                confidence=config.confidence.exact_identifier,
            )
        )
        return matches
    if event.vm_id and not event.pod_id:
        matches.append(
            RoutingMatch(
                layer=Layer.VM,
                field="vm_id",
                value=event.vm_id,
                rule="VM identifier present without a more specific component",
                precedence=300,
                confidence=config.confidence.exact_identifier,
            )
        )
    return matches


def route_event(event: NormalizedEvent, config: RoutingConfig) -> RoutingDecision:
    """Classify an event using deterministic evidence and explicit precedence."""
    matches = _identifier_matches(event, config)
    for field, value in _string_metadata(event):
        for layer, rule in config.layers.items():
            for alias in rule.aliases:
                if _alias_matches(value, alias):
                    matches.append(
                        RoutingMatch(
                            layer=layer,
                            field=field,
                            value=value,
                            rule=f"configured exact alias '{alias}'",
                            precedence=200,
                            confidence=config.confidence.exact_metadata,
                        )
                    )
    for layer, rule in config.layers.items():
        for pattern in rule.fallback_patterns:
            if re.search(pattern, event.message, flags=re.IGNORECASE):
                matches.append(
                    RoutingMatch(
                        layer=layer,
                        field="message",
                        value=event.message,
                        rule=f"configured fallback pattern '{pattern}'",
                        precedence=100,
                        confidence=config.confidence.fallback_pattern,
                    )
                )
    if not matches:
        return RoutingDecision(
            layer=RoutingLayer.UNKNOWN,
            confidence=0.0,
            explanation="No configured identifier, metadata alias, or fallback pattern matched.",
        )
    highest = max(match.precedence for match in matches)
    winning = tuple(match for match in matches if match.precedence == highest)
    layers = tuple(sorted({match.layer for match in winning}, key=lambda item: item.value))
    confidence = max(match.confidence for match in winning)
    if len(layers) > 1:
        names = ", ".join(layer.value for layer in layers)
        return RoutingDecision(
            layer=RoutingLayer.AMBIGUOUS,
            confidence=confidence,
            explanation=f"Equally strong routing evidence matched multiple layers: {names}.",
            candidate_layers=layers,
            matches=winning,
        )
    winner = winning[0]
    return RoutingDecision(
        layer=RoutingLayer(layers[0].value),
        confidence=confidence,
        explanation=f"{winner.field} matched {winner.rule} for {winner.layer.value}.",
        candidate_layers=layers,
        matches=winning,
    )


def _stable_id(prefix: str, values: Mapping[str, str]) -> str:
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(encoded).hexdigest()}"


def _event_evidence(event: NormalizedEvent, description: str) -> EvidenceReference:
    return EvidenceReference(
        evidence_id=_stable_id("evidence", {"event_id": event.event_id, "description": description}),
        kind=EvidenceKind.EVENT,
        reference_id=event.event_id,
        description=description,
        provenance=event.provenance,
    )


def _component(component_id: str, kind: ComponentKind, layer: Layer) -> ComponentRef:
    return ComponentRef(component_id=component_id, kind=kind, layer=layer)


def _has_validated_redis_pod_association(event: NormalizedEvent) -> bool:
    association = event.metadata.get("component_pod_association")
    component = association.get("component") if isinstance(association, dict) else None
    return (
        isinstance(association, dict)
        and association.get("basis") == "same_source_record"
        and association.get("pod_id") == event.pod_id
        and isinstance(component, str)
        and _alias_matches(component, "redis")
    )


def _observed_redis_placement(event: NormalizedEvent) -> TopologyRelationship:
    assert event.pod_id is not None and event.vm_id is not None
    description = "AIOps2025 record explicitly co-recorded the Redis pod and worker node."
    values = {"pod_id": event.pod_id, "vm_id": event.vm_id, "event_id": event.event_id}
    return TopologyRelationship(
        relationship_id=_stable_id("topology", values),
        source=_component(event.pod_id, ComponentKind.POD, Layer.DATABASE),
        target=_component(event.vm_id, ComponentKind.VM, Layer.VM),
        relationship_type=RelationshipType.RUNS_ON,
        validity=TimeWindow(start=event.timestamp, end=event.timestamp),
        provenance=Provenance(
            kind=ProvenanceKind.OBSERVED,
            source="AIOps2025 co-recorded pod/node placement",
            record_id=event.provenance.record_id,
        ),
        evidence=(_event_evidence(event, description),),
    )


def _synthetic_host_placement(event: NormalizedEvent, augmentation: TopologyAugmentation) -> TopologyRelationship:
    description = f"Explicit {augmentation.provenance_kind.value} VM-to-host mapping for scenario {augmentation.scenario_id}."
    provenance = Provenance(
        kind=augmentation.provenance_kind,
        source="CloudRCA topology augmentation",
        record_id=augmentation.augmentation_id,
        details={
            "scenario_id": augmentation.scenario_id,
            "generator_version": augmentation.generator_version,
        },
    )
    validity = (
        TimeWindow(start=augmentation.valid_from, end=augmentation.valid_to)
        if augmentation.valid_from is not None and augmentation.valid_to is not None
        else None
    )
    return TopologyRelationship(
        relationship_id=_stable_id(
            "topology",
            {"augmentation_id": augmentation.augmentation_id, "vm_id": augmentation.vm_id, "host_id": augmentation.host_id},
        ),
        source=_component(augmentation.vm_id, ComponentKind.VM, Layer.VM),
        target=_component(augmentation.host_id, ComponentKind.HOST, Layer.HYPERVISOR),
        relationship_type=RelationshipType.RUNS_ON,
        validity=validity,
        provenance=provenance,
        evidence=(
            EvidenceReference(
                evidence_id=_stable_id("evidence", {"augmentation_id": augmentation.augmentation_id}),
                kind=EvidenceKind.TOPOLOGY,
                reference_id=augmentation.augmentation_id,
                description=description,
                provenance=provenance,
            ),
        ),
    )


def resolve_topology(
    event: NormalizedEvent,
    decision: RoutingDecision,
    *,
    augmentations: tuple[TopologyAugmentation, ...] = (),
) -> TopologyResolution:
    """Resolve only observed relationships or explicitly supplied augmentations."""
    relationships: list[TopologyRelationship] = []
    unavailable: list[UnavailableTopology] = []
    vm_id = event.vm_id
    if decision.layer is RoutingLayer.DATABASE:
        database_id = event.pod_id or event.database_id or event.service
        observed_aiops2025 = (
            event.provenance.kind is ProvenanceKind.OBSERVED
            and event.provenance.source.casefold() == "aiops2025"
        )
        if (
            database_id
            and event.pod_id
            and vm_id
            and _has_validated_redis_pod_association(event)
            and observed_aiops2025
        ):
            relationships.append(_observed_redis_placement(event))
        elif database_id:
            unavailable.append(
                UnavailableTopology(
                    source=_component(
                        database_id,
                        ComponentKind.POD if event.pod_id else ComponentKind.DATABASE,
                        Layer.DATABASE,
                    ),
                    target_kind=ComponentKind.VM,
                    relationship_type=RelationshipType.RUNS_ON,
                    reason="No validated database-to-worker placement is available for this record.",
                )
            )
    if vm_id:
        matching_augmentations = tuple(item for item in augmentations if item.vm_id == vm_id)
        if len(matching_augmentations) > 1:
            raise ValueError(f"multiple topology augmentations supplied for VM {vm_id}")
        augmentation = matching_augmentations[0] if matching_augmentations else None
        if augmentation:
            relationships.append(_synthetic_host_placement(event, augmentation))
        else:
            unavailable.append(
                UnavailableTopology(
                    source=_component(vm_id, ComponentKind.VM, Layer.VM),
                    target_kind=ComponentKind.HOST,
                    relationship_type=RelationshipType.RUNS_ON,
                    reason="AIOps2025 does not provide real VM-to-physical-host placement.",
                )
            )
    return TopologyResolution(relationships=tuple(relationships), unavailable=tuple(unavailable))


def route_and_resolve(
    event: NormalizedEvent,
    config: RoutingConfig,
    *,
    augmentations: tuple[TopologyAugmentation, ...] = (),
) -> RoutedEvent:
    """Route one canonical event and attach supported topology information."""
    decision = route_event(event, config)
    return RoutedEvent(
        event=event,
        routing=decision,
        topology=resolve_topology(event, decision, augmentations=augmentations),
    )
