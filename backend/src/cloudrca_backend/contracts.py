"""Versioned data contracts shared by CloudRCA components."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, model_validator

CONTRACT_VERSION: Literal["1.0.0"] = "1.0.0"
SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

Identifier = Annotated[str, Field(min_length=1, max_length=256)]
NonEmptyText = Annotated[str, Field(min_length=1)]
ConfidenceScore = Annotated[float, Field(ge=0.0, le=1.0)]


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must use UTC (offset +00:00)")
    return value


UtcDateTime = Annotated[datetime, AfterValidator(_require_utc)]


class ContractModel(BaseModel):
    """Strict immutable base for persisted contract values."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION


class Layer(StrEnum):
    HYPERVISOR = "host_hypervisor"
    VM = "vm_guest_os"
    DATABASE = "database"


class Severity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ConfidenceLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ComponentKind(StrEnum):
    HOST = "host"
    VM = "vm"
    NODE = "node"
    POD = "pod"
    DATABASE = "database"
    SERVICE = "service"


class RelationshipType(StrEnum):
    RUNS_ON = "runs_on"
    CONNECTS_TO = "connects_to"
    DEPENDS_ON = "depends_on"


class ProvenanceKind(StrEnum):
    OBSERVED = "observed"
    AUGMENTED = "augmented"
    SYNTHETIC = "synthetic"


class EvidenceKind(StrEnum):
    EVENT = "event"
    TOPOLOGY = "topology"
    FINDING = "finding"
    METRIC = "metric"
    LOG = "log"


class IncidentStatus(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    RESOLVED = "resolved"


class Provenance(ContractModel):
    kind: ProvenanceKind
    source: Identifier
    record_id: Identifier | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)


class Confidence(ContractModel):
    score: ConfidenceScore
    level: ConfidenceLevel


class TimeWindow(ContractModel):
    start: UtcDateTime
    end: UtcDateTime

    @model_validator(mode="after")
    def end_must_not_precede_start(self) -> TimeWindow:
        if self.end < self.start:
            raise ValueError("time window end must not precede start")
        return self


class ComponentRef(ContractModel):
    component_id: Identifier
    kind: ComponentKind
    layer: Layer
    name: Identifier | None = None


class EvidenceReference(ContractModel):
    evidence_id: Identifier
    kind: EvidenceKind
    reference_id: Identifier
    description: NonEmptyText | None = None
    provenance: Provenance | None = None


class RawLogRecord(ContractModel):
    """Minimal lossless envelope for a source record before normalization."""

    raw_record_id: Identifier
    ingested_at: UtcDateTime
    source: Identifier
    payload: dict[str, JsonValue]
    provenance: Provenance


class NormalizedEvent(ContractModel):
    event_id: Identifier
    timestamp: UtcDateTime
    source: Identifier
    service: Identifier | None = None
    layer: Layer
    severity: Severity
    message: NonEmptyText
    host_id: Identifier | None = None
    vm_id: Identifier | None = None
    pod_id: Identifier | None = None
    database_id: Identifier | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    provenance: Provenance


class TopologyRelationship(ContractModel):
    relationship_id: Identifier
    source: ComponentRef
    target: ComponentRef
    relationship_type: RelationshipType
    validity: TimeWindow | None = None
    provenance: Provenance
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def components_must_differ(self) -> TopologyRelationship:
        if self.source.component_id == self.target.component_id:
            raise ValueError("topology source and target must differ")
        return self


class Finding(ContractModel):
    finding_id: Identifier
    layer: Layer
    summary: NonEmptyText
    suspected_problem: NonEmptyText
    confidence: Confidence
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1)
    event_ids: tuple[Identifier, ...] = Field(default_factory=tuple)
    component_ids: tuple[Identifier, ...] = Field(default_factory=tuple)
    time_window: TimeWindow | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)


class Correlation(ContractModel):
    correlation_id: Identifier
    summary: NonEmptyText
    event_ids: tuple[Identifier, ...] = Field(min_length=2)
    confidence: Confidence
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1)


class Incident(ContractModel):
    incident_id: Identifier
    time_window: TimeWindow
    affected_layers: frozenset[Layer] = Field(min_length=1)
    affected_components: tuple[ComponentRef, ...] = Field(min_length=1)
    event_ids: tuple[Identifier, ...] = Field(min_length=1)
    findings: tuple[Finding, ...] = Field(min_length=1)
    status: IncidentStatus = IncidentStatus.OPEN


class TimelineEntry(ContractModel):
    timestamp: UtcDateTime
    summary: NonEmptyText
    event_ids: tuple[Identifier, ...] = Field(min_length=1)


class CausalEdge(ContractModel):
    cause_id: Identifier
    effect_id: Identifier
    summary: NonEmptyText
    confidence: Confidence
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def cause_and_effect_must_differ(self) -> CausalEdge:
        if self.cause_id == self.effect_id:
            raise ValueError("causal edge cause and effect must differ")
        return self


class RootCauseHypothesis(ContractModel):
    summary: NonEmptyText
    layer: Layer
    component: ComponentRef | None = None
    confidence: Confidence
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def component_must_match_layer(self) -> RootCauseHypothesis:
        if self.component is not None and self.component.layer is not self.layer:
            raise ValueError("hypothesis component layer must match hypothesis layer")
        return self


class ModelRuntimeProvenance(ContractModel):
    provider: Identifier
    model: Identifier
    harness: Identifier
    harness_version: Identifier | None = None


class RCAReport(ContractModel):
    incident_id: Identifier
    probable_root_cause: NonEmptyText
    root_cause_layer: Layer
    root_cause_component: ComponentRef
    timeline: tuple[TimelineEntry, ...] = Field(min_length=1)
    causal_chain: tuple[CausalEdge, ...] = Field(min_length=1)
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1)
    alternative_hypotheses: tuple[RootCauseHypothesis, ...] = Field(default_factory=tuple)
    confidence: Confidence
    uncertainty: NonEmptyText | None = None
    runtime: ModelRuntimeProvenance

    @model_validator(mode="after")
    def root_component_must_match_layer(self) -> RCAReport:
        if self.root_cause_component.layer is not self.root_cause_layer:
            raise ValueError("root-cause component layer must match root_cause_layer")
        return self


SCHEMA_MODELS: dict[str, type[ContractModel]] = {
    "raw-log-record": RawLogRecord,
    "normalized-event": NormalizedEvent,
    "topology-relationship": TopologyRelationship,
    "finding": Finding,
    "correlation": Correlation,
    "incident": Incident,
    "rca-report": RCAReport,
}


def contract_json_schema(name: str, model: type[ContractModel]) -> dict[str, Any]:
    """Return a versioned JSON Schema 2020-12 document for a contract."""
    schema = model.model_json_schema(mode="serialization")
    schema["$schema"] = SCHEMA_DIALECT
    schema["$id"] = f"https://cloudrca.dev/schemas/v{CONTRACT_VERSION}/{name}.schema.json"
    return schema
