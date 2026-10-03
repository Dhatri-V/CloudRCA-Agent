"""A1 database-layer analysis built on the shared specialist runtime."""

from __future__ import annotations

import json
from enum import StrEnum

from pydantic import Field, ValidationError, model_validator

from .contracts import ComponentRef, Confidence, ContractModel, Identifier, Layer, NonEmptyText, Severity, TimeWindow
from .routing import RoutedEvent, RoutingLayer
from .specialist_runtime import (
    SpecialistRequest,
    SpecialistRuntime,
    SpecialistStructuredRunResult,
    specialist_event_context,
)


class DatabaseErrorType(StrEnum):
    CONNECTION = "connection_failure"
    DEADLOCK = "deadlock"
    QUERY = "query_failure_or_performance"
    REPLICATION = "replication_failure"
    STORAGE = "storage_failure"
    AVAILABILITY = "availability_failure"


class HypothesisType(StrEnum):
    HYPOTHESIS = "hypothesis"


class DatabaseObservation(ContractModel):
    """A direct database observation tied to the events that support it."""

    statement: NonEmptyText
    evidence_event_ids: tuple[Identifier, ...] = Field(min_length=1)


class DatabaseHypothesis(ContractModel):
    """An explicitly labelled, evidence-bound inference rather than a fact."""

    type: HypothesisType = HypothesisType.HYPOTHESIS
    statement: NonEmptyText
    evidence_event_ids: tuple[Identifier, ...] = Field(min_length=1)
    confidence: Confidence


class DatabaseFinding(ContractModel):
    """An evidence-bound database symptom and explicitly uncertain hypothesis."""

    error_type: DatabaseErrorType
    severity: Severity
    component: ComponentRef | None = None
    time_range: TimeWindow | None = None
    symptoms: tuple[str, ...] = Field(min_length=1)
    observations: tuple[DatabaseObservation, ...] = Field(min_length=1)
    hypotheses: tuple[DatabaseHypothesis, ...] = ()
    confidence: Confidence

    @model_validator(mode="after")
    def component_must_be_database_layer(self) -> DatabaseFinding:
        if self.component is not None and self.component.layer is not Layer.DATABASE:
            raise ValueError("database finding component must be in the database layer")
        return self


class DatabaseAnalysis(ContractModel):
    """A1 output; an empty set explicitly means no database anomaly was found."""

    findings: tuple[DatabaseFinding, ...] = ()


class DatabaseAgentInputError(ValueError):
    """Raised before Qwen is called for input outside A1's database boundary."""


def build_database_prompt(request: SpecialistRequest, prompt_version: str) -> str:
    """Build the deterministic A1 prompt using Issue #7's safe event context."""
    payload = {
        "specialist_layer": Layer.DATABASE.value,
        "events": [specialist_event_context(item) for item in request.events],
    }
    instructions = (
        f"CLOUDRCA_DATABASE_PROMPT_VERSION={prompt_version}",
        "You are A1, the database specialist. Analyze only these database-routed events.",
        "Supported incident types: connection_failure, deadlock, query_failure_or_performance, "
        "replication_failure, storage_failure, availability_failure.",
        "Observations must be directly supported by the cited input events. Hypotheses are inferences, "
        "must be explicitly uncertain, and must never be stated as confirmed facts.",
        "Every observation and hypothesis needs evidence_event_ids that cite only supplied event IDs. "
        "Hypotheses must use type=hypothesis. If evidence conflicts, "
        "record the conflict as an observation and lower confidence or return no finding; do not resolve it by invention.",
        "If no supported database anomaly exists, return {\"findings\": []} exactly.",
        "Return exactly one JSON object that validates against DATABASE_ANALYSIS_SCHEMA.",
        f"DATABASE_ANALYSIS_SCHEMA={json.dumps(DatabaseAnalysis.model_json_schema(mode='validation'), sort_keys=True, separators=(',', ':'))}",
        f"INPUT={json.dumps(payload, sort_keys=True, separators=(',', ':'))}",
    )
    return "\n".join(instructions)


def validate_database_analysis(raw: str, request: SpecialistRequest) -> DatabaseAnalysis:
    """Reject model claims not anchored to A1's input events."""
    try:
        analysis = DatabaseAnalysis.model_validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False)
        raise ValueError(json.dumps(errors, sort_keys=True, separators=(",", ":"))) from exc
    events = {item.event.event_id: item.event for item in request.events}
    for finding in analysis.findings:
        evidence_ids = {
            event_id for item in finding.observations for event_id in item.evidence_event_ids
        } | {event_id for item in finding.hypotheses for event_id in item.evidence_event_ids}
        if not evidence_ids.issubset(events):
            raise ValueError("database observation or hypothesis references event IDs outside the specialist input")
        component_ids = {
            identifier
            for event_id in evidence_ids
            for identifier in (events[event_id].database_id, events[event_id].pod_id, events[event_id].service)
            if identifier is not None
        }
        if finding.component is not None and finding.component.component_id not in component_ids:
            raise ValueError("database finding component is not supported by its event evidence")
        if finding.time_range is not None:
            times = [events[event_id].timestamp for event_id in evidence_ids]
            if finding.time_range.start != min(times) or finding.time_range.end != max(times):
                raise ValueError("database finding time range must equal its event evidence range")
    return analysis


class DatabaseAgent:
    """A1 entry point; the shared runtime owns Qwen execution and repairs."""

    def __init__(self, runtime: SpecialistRuntime) -> None:
        self._runtime = runtime

    def analyze(
        self, events: tuple[RoutedEvent, ...], *, correlation_id: str | None = None
    ) -> SpecialistStructuredRunResult[DatabaseAnalysis]:
        invalid = [item.event.event_id for item in events if item.routing.layer is not RoutingLayer.DATABASE]
        if invalid:
            raise DatabaseAgentInputError(f"A1 accepts only database-routed events: {', '.join(invalid)}")
        request = SpecialistRequest(layer=Layer.DATABASE, events=events, correlation_id=correlation_id)
        return self._runtime.run_structured(
            request,
            prompt_builder=build_database_prompt,
            response_validator=validate_database_analysis,
            output_name="DatabaseAnalysis",
        )
