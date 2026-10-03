"""A2 VM/guest-operating-system analysis built on the shared specialist runtime."""

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
)
from .routing import RoutedEvent, RoutingLayer
from .specialist_runtime import (
    SpecialistRequest,
    SpecialistRuntime,
    SpecialistStructuredRunResult,
    specialist_event_context,
)


class VMErrorType(StrEnum):
    CPU = "cpu_pressure"
    MEMORY = "memory_pressure"
    SWAP = "swap_pressure"
    DISK = "disk_failure"
    FILESYSTEM = "filesystem_failure"
    NETWORK = "network_failure"
    PROCESS = "process_failure"
    KERNEL = "kernel_failure"
    SERVICE = "service_failure"


class HypothesisType(StrEnum):
    HYPOTHESIS = "hypothesis"


class EvidenceState(StrEnum):
    CONSISTENT = "consistent"
    CONTRADICTORY = "contradictory"


CONTRADICTORY_CONFIDENCE_CEILING = 0.5


class VMSymptom(ContractModel):
    """A VM symptom tied to the events that support it."""

    category: VMErrorType
    statement: NonEmptyText
    evidence_event_ids: tuple[Identifier, ...] = Field(min_length=1)


class VMObservation(ContractModel):
    """A direct VM observation tied to the events that support it."""

    category: VMErrorType
    statement: NonEmptyText
    evidence_event_ids: tuple[Identifier, ...] = Field(min_length=1)


class VMHypothesis(ContractModel):
    """An explicitly labelled, evidence-bound inference rather than a fact."""

    type: HypothesisType = HypothesisType.HYPOTHESIS
    category: VMErrorType
    statement: NonEmptyText
    evidence_event_ids: tuple[Identifier, ...] = Field(min_length=1)
    confidence: Confidence


class VMFinding(ContractModel):
    """An evidence-bound VM symptom and explicitly uncertain hypothesis."""

    error_type: VMErrorType
    severity: Severity
    component: ComponentRef | None = None
    time_range: TimeWindow
    symptoms: tuple[VMSymptom, ...] = Field(min_length=1)
    observations: tuple[VMObservation, ...] = Field(min_length=1)
    hypotheses: tuple[VMHypothesis, ...] = ()
    confidence: Confidence
    evidence_state: EvidenceState = EvidenceState.CONSISTENT
    contradiction_event_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def component_must_be_vm_layer(self) -> VMFinding:
        if self.component is not None and self.component.layer is not Layer.VM:
            raise ValueError("VM finding component must be in the VM layer")
        if (
            any(item.category is not self.error_type for item in self.symptoms)
            or any(item.category is not self.error_type for item in self.observations)
            or any(item.category is not self.error_type for item in self.hypotheses)
        ):
            raise ValueError("VM finding claims must use the finding's VM category")
        if self.evidence_state is EvidenceState.CONTRADICTORY:
            if len(self.contradiction_event_ids) < 2:
                raise ValueError("contradictory VM findings require at least two contradiction event IDs")
            if self.confidence.score > CONTRADICTORY_CONFIDENCE_CEILING or any(
                item.confidence.score > CONTRADICTORY_CONFIDENCE_CEILING for item in self.hypotheses
            ):
                raise ValueError("contradictory VM findings require confidence at or below 0.5")
        return self


class VMAnalysis(ContractModel):
    """A2 output; an empty set explicitly means no VM anomaly was found."""

    findings: tuple[VMFinding, ...] = ()


class VMAgentInputError(ValueError):
    """Raised before Qwen is called for input outside A2's VM boundary."""


def build_vm_prompt(request: SpecialistRequest, prompt_version: str) -> str:
    """Build the deterministic A2 prompt using Issue #7's safe event context."""
    payload = {
        "specialist_layer": Layer.VM.value,
        "events": [specialist_event_context(item) for item in request.events],
    }
    instructions = (
        f"CLOUDRCA_VM_PROMPT_VERSION={prompt_version}",
        "You are A2, the VM and guest operating-system specialist. Analyze only these VM-routed events.",
        "Supported incident types: cpu_pressure, memory_pressure, swap_pressure, disk_failure, "
        "filesystem_failure, network_failure, process_failure, kernel_failure, service_failure.",
        "Observations must be directly supported by the cited input events. Hypotheses are inferences, "
        "must be explicitly uncertain, and must never be stated as confirmed facts.",
        "Every symptom, observation, and hypothesis needs evidence_event_ids that cite only supplied event IDs. "
        "Hypotheses must use type=hypothesis. Preserve a cited VM ID when one is available. "
        "If evidence conflicts, set evidence_state=contradictory, cite both contradiction_event_ids, and use confidence at or below 0.5.",
        "If no supported VM anomaly exists, return {\"findings\": []} exactly.",
        "Return exactly one JSON object that validates against VM_ANALYSIS_SCHEMA.",
        f"VM_ANALYSIS_SCHEMA={json.dumps(VMAnalysis.model_json_schema(mode='validation'), sort_keys=True, separators=(',', ':'))}",
        f"INPUT={json.dumps(payload, sort_keys=True, separators=(',', ':'))}",
    )
    return "\n".join(instructions)


def validate_vm_analysis(raw: str, request: SpecialistRequest) -> VMAnalysis:
    """Reject A2 claims not anchored to its supplied VM events."""
    try:
        analysis = VMAnalysis.model_validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False)
        raise ValueError(json.dumps(errors, default=str, sort_keys=True, separators=(",", ":"))) from exc
    events = {item.event.event_id: item.event for item in request.events}
    for finding in analysis.findings:
        evidence_ids = {
            event_id for item in finding.symptoms for event_id in item.evidence_event_ids
        } | {event_id for item in finding.observations for event_id in item.evidence_event_ids} | {
            event_id for item in finding.hypotheses for event_id in item.evidence_event_ids
        }
        if not evidence_ids.issubset(events):
            raise ValueError("VM claim references event IDs outside the specialist input")
        if not set(finding.contradiction_event_ids).issubset(evidence_ids):
            raise ValueError("VM contradiction event IDs must be cited by the finding")
        vm_ids = {events[event_id].vm_id for event_id in evidence_ids if events[event_id].vm_id is not None}
        service_ids = {events[event_id].service for event_id in evidence_ids if events[event_id].service is not None}
        if len(vm_ids) > 1:
            raise ValueError("VM finding cannot combine evidence from multiple VM IDs")
        if vm_ids and (
            finding.component is None
            or finding.component.component_id not in vm_ids
            or finding.component.kind is not ComponentKind.VM
        ):
            raise ValueError("VM finding must preserve a VM ID supported by its event evidence")
        if not vm_ids and finding.component is not None and (
            finding.component.component_id not in service_ids
            or finding.component.kind not in (ComponentKind.NODE, ComponentKind.SERVICE)
        ):
            raise ValueError("VM finding component is not supported by its event evidence")
        times = [events[event_id].timestamp for event_id in evidence_ids]
        if finding.time_range.start != min(times) or finding.time_range.end != max(times):
            raise ValueError("VM finding time range must equal its event evidence range")
    return analysis


class VMAgent:
    """A2 entry point; the shared runtime owns Qwen execution and repairs."""

    def __init__(self, runtime: SpecialistRuntime) -> None:
        self._runtime = runtime

    def analyze(self, events: tuple[RoutedEvent, ...], *, correlation_id: str | None = None) -> SpecialistStructuredRunResult[VMAnalysis]:
        event_ids = [item.event.event_id for item in events]
        if not event_ids:
            raise VMAgentInputError("A2 requires at least one VM-routed event")
        if len(set(event_ids)) != len(event_ids):
            raise VMAgentInputError("A2 input event IDs must be unique")
        invalid = [item.event.event_id for item in events if item.routing.layer is not RoutingLayer.VM]
        if invalid:
            raise VMAgentInputError(f"A2 accepts only VM-routed events: {', '.join(invalid)}")
        request = SpecialistRequest(layer=Layer.VM, events=events, correlation_id=correlation_id)
        return self._runtime.run_structured(
            request,
            prompt_builder=build_vm_prompt,
            response_validator=validate_vm_analysis,
            output_name="VMAnalysis",
        )
