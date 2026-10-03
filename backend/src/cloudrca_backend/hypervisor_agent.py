"""A3 host/hypervisor analysis built on the shared specialist runtime."""

from __future__ import annotations

import json
from enum import StrEnum

from pydantic import Field, ValidationError, model_validator

from .contracts import (
    ComponentKind,
    ComponentRef,
    Confidence,
    ContractModel,
    Identifier,
    Layer,
    NonEmptyText,
    Severity,
    TimeWindow,
    TopologyRelationship,
)
from .routing import RoutedEvent, RoutingLayer
from .specialist_runtime import (
    SpecialistRequest,
    SpecialistRuntime,
    SpecialistStructuredRunResult,
    specialist_event_context,
)


class HypervisorErrorType(StrEnum):
    HOST_CPU = "host_cpu_pressure"
    HOST_MEMORY = "host_memory_pressure"
    STORAGE = "storage_failure"
    IO = "io_failure"
    NETWORK = "network_failure"
    VIRTUALIZATION = "virtualization_failure"
    LIFECYCLE = "lifecycle_failure"
    RESOURCE_CONTENTION = "resource_contention"


class HypothesisType(StrEnum):
    HYPOTHESIS = "hypothesis"


class EvidenceState(StrEnum):
    CONSISTENT = "consistent"
    CONTRADICTORY = "contradictory"


CONTRADICTORY_CONFIDENCE_CEILING = 0.5


class HypervisorSymptom(ContractModel):
    """A host symptom tied to the events that support it."""

    category: HypervisorErrorType
    statement: NonEmptyText
    evidence_event_ids: tuple[Identifier, ...] = Field(min_length=1)


class HypervisorObservation(ContractModel):
    """A direct host observation tied to the events that support it."""

    category: HypervisorErrorType
    statement: NonEmptyText
    evidence_event_ids: tuple[Identifier, ...] = Field(min_length=1)


class HypervisorHypothesis(ContractModel):
    """An explicitly labelled, evidence-bound inference rather than a fact."""

    type: HypothesisType = HypothesisType.HYPOTHESIS
    category: HypervisorErrorType
    statement: NonEmptyText
    evidence_event_ids: tuple[Identifier, ...] = Field(min_length=1)
    confidence: Confidence


class HypervisorImpact(ContractModel):
    """A downstream component claim backed by an existing topology relationship."""

    affected_component: ComponentRef
    statement: NonEmptyText
    topology_relationship_ids: tuple[Identifier, ...] = Field(min_length=1)


class HypervisorFinding(ContractModel):
    """An evidence-bound host finding with optional topology-backed impact."""

    error_type: HypervisorErrorType
    severity: Severity
    component: ComponentRef | None = None
    time_range: TimeWindow
    symptoms: tuple[HypervisorSymptom, ...] = Field(min_length=1)
    observations: tuple[HypervisorObservation, ...] = Field(min_length=1)
    hypotheses: tuple[HypervisorHypothesis, ...] = ()
    impacts: tuple[HypervisorImpact, ...] = ()
    confidence: Confidence
    evidence_state: EvidenceState = EvidenceState.CONSISTENT
    contradiction_event_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def claims_must_be_host_scoped(self) -> HypervisorFinding:
        if self.component is not None and (
            self.component.layer is not Layer.HYPERVISOR or self.component.kind is not ComponentKind.HOST
        ):
            raise ValueError("hypervisor finding component must be a hypervisor host")
        if (
            any(item.category is not self.error_type for item in self.symptoms)
            or any(item.category is not self.error_type for item in self.observations)
            or any(item.category is not self.error_type for item in self.hypotheses)
        ):
            raise ValueError("hypervisor finding claims must use the finding's host category")
        if self.evidence_state is EvidenceState.CONTRADICTORY:
            if len(self.contradiction_event_ids) < 2:
                raise ValueError("contradictory hypervisor findings require at least two contradiction event IDs")
            if self.confidence.score > CONTRADICTORY_CONFIDENCE_CEILING or any(
                item.confidence.score > CONTRADICTORY_CONFIDENCE_CEILING for item in self.hypotheses
            ):
                raise ValueError("contradictory hypervisor findings require confidence at or below 0.5")
        return self


class HypervisorAnalysis(ContractModel):
    """A3 output; an empty set explicitly means no hypervisor anomaly was found."""

    findings: tuple[HypervisorFinding, ...] = ()


class HypervisorAgentInputError(ValueError):
    """Raised before Qwen is called for input outside A3's hypervisor boundary."""


def build_hypervisor_prompt(request: SpecialistRequest, prompt_version: str) -> str:
    """Build the deterministic A3 prompt using Issue #7's safe event context."""
    payload = {
        "specialist_layer": Layer.HYPERVISOR.value,
        "events": [specialist_event_context(item) for item in request.events],
    }
    instructions = (
        f"CLOUDRCA_HYPERVISOR_PROMPT_VERSION={prompt_version}",
        "You are A3, the host and hypervisor specialist. Analyze only these host_hypervisor-routed events.",
        "Supported incident types: host_cpu_pressure, host_memory_pressure, storage_failure, io_failure, "
        "network_failure, virtualization_failure, lifecycle_failure, resource_contention.",
        "Do not diagnose guest-OS/VM failures or database failures.",
        "Observations must be directly supported by cited input events. Hypotheses are inferences, must use "
        "type=hypothesis, and must never be stated as confirmed facts.",
        "Every symptom, observation, and hypothesis needs evidence_event_ids that cite only supplied event IDs. "
        "Only report a host ID present in cited events. An impact needs topology_relationship_ids for supplied "
        "topology relationships that connect that host to the affected component; otherwise omit impacts. "
        "If evidence conflicts, set evidence_state=contradictory, cite both contradiction_event_ids, and use "
        "confidence at or below 0.5.",
        "If no supported hypervisor anomaly exists, return {\"findings\": []} exactly.",
        "Return exactly one JSON object that validates against HYPERVISOR_ANALYSIS_SCHEMA.",
        f"HYPERVISOR_ANALYSIS_SCHEMA={json.dumps(HypervisorAnalysis.model_json_schema(mode='validation'), sort_keys=True, separators=(',', ':'))}",
        f"INPUT={json.dumps(payload, sort_keys=True, separators=(',', ':'))}",
    )
    return "\n".join(instructions)


def _impact_is_supported(
    impact: HypervisorImpact, host_id: str, relationships: dict[str, TopologyRelationship]
) -> bool:
    for relationship_id in impact.topology_relationship_ids:
        relationship = relationships.get(relationship_id)
        if relationship is None:
            return False
        components = (relationship.source, relationship.target)
        if not any(
            item.component_id == host_id and item.kind is ComponentKind.HOST and item.layer is Layer.HYPERVISOR
            for item in components
        ) or not any(
            item.component_id == impact.affected_component.component_id
            and item.kind is impact.affected_component.kind
            and item.layer is impact.affected_component.layer
            for item in components
        ):
            return False
    return True


def validate_hypervisor_analysis(raw: str, request: SpecialistRequest) -> HypervisorAnalysis:
    """Reject A3 claims not anchored to its supplied events and topology."""
    try:
        analysis = HypervisorAnalysis.model_validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False)
        raise ValueError(json.dumps(errors, default=str, sort_keys=True, separators=(',', ":"))) from exc
    events = {item.event.event_id: item.event for item in request.events}
    relationships = {
        relationship.relationship_id: relationship
        for item in request.events
        for relationship in item.topology.relationships
    }
    for finding in analysis.findings:
        evidence_ids = {
            event_id for item in finding.symptoms for event_id in item.evidence_event_ids
        } | {event_id for item in finding.observations for event_id in item.evidence_event_ids} | {
            event_id for item in finding.hypotheses for event_id in item.evidence_event_ids
        }
        if not evidence_ids.issubset(events):
            raise ValueError("hypervisor claim references event IDs outside the specialist input")
        if not set(finding.contradiction_event_ids).issubset(evidence_ids):
            raise ValueError("hypervisor contradiction event IDs must be cited by the finding")
        host_ids = {events[event_id].host_id for event_id in evidence_ids if events[event_id].host_id is not None}
        if len(host_ids) > 1:
            raise ValueError("hypervisor finding cannot combine evidence from multiple host IDs")
        if host_ids and (
            finding.component is None
            or finding.component.component_id not in host_ids
            or finding.component.kind is not ComponentKind.HOST
        ):
            raise ValueError("hypervisor finding must preserve a host ID supported by its event evidence")
        if not host_ids and (finding.component is not None or finding.impacts):
            raise ValueError("hypervisor findings without a host ID cannot claim a component or downstream impact")
        if host_ids:
            host_id = next(item for item in host_ids if item is not None)
            if not all(_impact_is_supported(item, host_id, relationships) for item in finding.impacts):
                raise ValueError("hypervisor impact is not supported by a supplied host topology relationship")
        times = [events[event_id].timestamp for event_id in evidence_ids]
        if finding.time_range.start != min(times) or finding.time_range.end != max(times):
            raise ValueError("hypervisor finding time range must equal its event evidence range")
    return analysis


class HypervisorAgent:
    """A3 entry point; the shared runtime owns Qwen execution and repairs."""

    def __init__(self, runtime: SpecialistRuntime) -> None:
        self._runtime = runtime

    def analyze(
        self, events: tuple[RoutedEvent, ...], *, correlation_id: str | None = None
    ) -> SpecialistStructuredRunResult[HypervisorAnalysis]:
        event_ids = [item.event.event_id for item in events]
        if not event_ids:
            raise HypervisorAgentInputError("A3 requires at least one hypervisor-routed event")
        if len(set(event_ids)) != len(event_ids):
            raise HypervisorAgentInputError("A3 input event IDs must be unique")
        invalid = [item.event.event_id for item in events if item.routing.layer is not RoutingLayer.HYPERVISOR]
        if invalid:
            raise HypervisorAgentInputError(f"A3 accepts only hypervisor-routed events: {', '.join(invalid)}")
        request = SpecialistRequest(layer=Layer.HYPERVISOR, events=events, correlation_id=correlation_id)
        return self._runtime.run_structured(
            request,
            prompt_builder=build_hypervisor_prompt,
            response_validator=validate_hypervisor_analysis,
            output_name="HypervisorAnalysis",
        )
