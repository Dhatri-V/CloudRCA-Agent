"""GLM-backed, evidence-constrained main RCA agent."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from uuid import uuid4

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contracts import Layer, ModelRuntimeProvenance, NormalizedEvent, RCAReport, Severity
from .correlation import CorrelationGraph
from .incident_grouping import CandidateIncident
from .knowledge import RetrievedChunk
from .orchestration import SpecialistWorkflowResult, WorkflowStatus
from .routing import TopologyResolution
from .settings import Settings, SettingsError
from .specialist_runtime import (
    EventLogger,
    JCodeConfigurationError,
    JCodeProviderError,
    JCodeTimeoutError,
    JCodeTransport,
    SpecialistFailureCode,
    SpecialistRuntimeConfig,
    TokenUsage,
    _integer,
    _number,
    _repair_prompt,
    _required,
    unwrap_code_fence,
)


class MainRcaRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    incident: CandidateIncident
    graph: CorrelationGraph
    workflow: SpecialistWorkflowResult | None = None
    knowledge: tuple[RetrievedChunk, ...] = ()
    topology: TopologyResolution = Field(default_factory=TopologyResolution)
    events: tuple[NormalizedEvent, ...] = ()
    correlation_id: str | None = Field(default=None, min_length=1, max_length=256)


@dataclass(frozen=True, slots=True)
class MainRcaMetadata:
    correlation_id: str
    provider_profile: str
    model: str
    prompt_version: str
    duration_ms: float
    repair_count: int
    usage: TokenUsage
    session_id: str | None


@dataclass(frozen=True, slots=True)
class MainRcaResult:
    report: RCAReport
    metadata: MainRcaMetadata


REPAIR_EXCERPT_CHARACTERS = 2_000
MAX_LOGGED_LOC_CHARACTERS = 120


class MainRcaValidationError(ValueError):
    """A rejected RCA report with safe, content-free issue records (error type and field path only)."""

    def __init__(self, message: str, issues: tuple[dict[str, str], ...]) -> None:
        super().__init__(message)
        self.issues = issues


def _issue(error_type: str, loc: str) -> dict[str, str]:
    return {"type": error_type, "loc": loc[:MAX_LOGGED_LOC_CHARACTERS]}


def _schema_issue(error: Mapping[str, object]) -> dict[str, str]:
    """Error type and field path only; a model-chosen unexpected key is replaced with a fixed placeholder."""
    parts = [str(part) for part in error["loc"]] if isinstance(error["loc"], (list, tuple)) else []
    if error["type"] == "extra_forbidden" and parts:
        parts[-1] = "<unexpected_field>"
    return _issue(str(error["type"]), ".".join(parts))


class MainRcaError(RuntimeError):
    def __init__(self, code: SpecialistFailureCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def load_main_rca_runtime_config(settings: Settings, environ: Mapping[str, str]) -> SpecialistRuntimeConfig:
    """Load GLM/JCode settings while retaining only the credential variable name."""
    api_key_env = environ.get("CLOUDRCA_GLM_API_KEY_ENV", "").strip() or None
    try:
        config = SpecialistRuntimeConfig(
            jcode_binary=settings.jcode_binary,
            provider_profile=_required(environ, "CLOUDRCA_GLM_PROVIDER_PROFILE"),
            provider_base_url=_required(environ, "CLOUDRCA_GLM_BASE_URL"),
            model=_required(environ, "CLOUDRCA_GLM_MODEL"),
            api_key_env=api_key_env,
            timeout_seconds=_number(environ, "CLOUDRCA_MAIN_RCA_TIMEOUT_SECONDS", 60),
            max_events=_integer(environ, "CLOUDRCA_MAIN_RCA_MAX_EVENTS", 100),
            max_prompt_characters=_integer(environ, "CLOUDRCA_MAIN_RCA_MAX_PROMPT_CHARACTERS", 50_000),
            max_repair_attempts=_integer(environ, "CLOUDRCA_MAIN_RCA_MAX_REPAIR_ATTEMPTS", 1),
            prompt_version=environ.get("CLOUDRCA_MAIN_RCA_PROMPT_VERSION", "main-rca-v2").strip(),
        )
    except ValidationError as exc:
        raise SettingsError(f"Invalid main RCA configuration: {exc}") from exc
    if config.api_key_env and not environ.get(config.api_key_env, "").strip():
        raise SettingsError(f"Missing provider credential environment variable: {config.api_key_env}")
    return config


def _event_ref(node_id: str) -> str:
    return node_id.removeprefix("event:")


MAX_PROMPT_EDGES = 40
EDGE_TYPE_MEANINGS = {
    "topology_candidate": "events within the UTC window on components joined by an observed topology relationship (see topology)",
    "temporal_candidate": "events within the UTC window that share a component identifier",
    "weak_temporal_candidate": "events within the UTC window with only weak shared context",
}


def _compact_graph(graph: CorrelationGraph, max_edges: int = MAX_PROMPT_EDGES, *, include_nodes: bool = True) -> dict[str, object]:
    """The highest-scoring candidate edges, with counts for any omitted.

    Events on one instance share identifiers, so the graph grows quadratically with incident size; listing every
    pair would crowd out the evidence itself. Omitted edges stay valid citations (validation uses the full graph).
    """
    ranked = sorted(graph.edges, key=lambda edge: (-edge.score, edge.source_id, edge.target_id))
    listed = ranked[:max_edges]
    by_type: dict[str, int] = {}
    for edge in graph.edges:
        by_type[edge.edge_type.value] = by_type.get(edge.edge_type.value, 0) + 1
    return {
        "incident_id": graph.incident_id,
        # Nodes repeat each event's ID, layer, and time; they are omitted when INPUT.events lists the events.
        **({"nodes": [item.model_dump(mode="json", exclude={"provenance", "schema_version", "node_id"}) for item in graph.nodes]} if include_nodes else {}),
        "edge_summary": {
            "edge_count": len(graph.edges),
            "listed": len(listed),
            "omitted": len(graph.edges) - len(listed),
            "by_type": dict(sorted(by_type.items())),
            "type_meanings": {name: EDGE_TYPE_MEANINGS[name] for name in sorted(by_type) if name in EDGE_TYPE_MEANINGS},
        },
        "edges": [
            {"source": _event_ref(edge.source_id), "target": _event_ref(edge.target_id), "type": edge.edge_type.value, "score": round(edge.score, 3)}
            for edge in listed
        ],
    }


def _compact_event(event: NormalizedEvent) -> dict[str, object]:
    """One event's facts once: identifiers, message, timing, and provenance, without the raw source record."""
    ids = {name: getattr(event, name) for name in ("service", "host_id", "vm_id", "pod_id", "database_id") if getattr(event, name)}
    attributes = event.metadata.get("source_attributes") or {}
    # Attributes already stated in the message as key=value add nothing; every other attribute is kept.
    extra = {
        key: value
        for key, value in (attributes.items() if isinstance(attributes, dict) else ())
        if not (isinstance(value, (int, float)) and not isinstance(value, bool) and f"{key}={value:g}" in event.message)
    }
    provenance: dict[str, object] = {"kind": event.provenance.kind.value, "record_id": event.provenance.record_id}
    if event.provenance.source != event.source:
        provenance["source"] = event.provenance.source
    compact: dict[str, object] = {
        "event_id": event.event_id,
        "timestamp": event.timestamp.isoformat().replace("+00:00", "Z"),
        "layer": event.layer.value,
        "severity": event.severity.value,
        "source": event.source,
        "message": event.message,
        **ids,
        "record_kind": event.metadata.get("record_kind"),
        "provenance": provenance,
    }
    if extra:
        compact["source_attributes"] = extra
    return compact


VERBATIM_EVENT_LIMIT = 40
NOTABLE_HEAD, NOTABLE_TAIL = 3, 2  # occurrences of one repeated warning/error kept verbatim at each end
_NOTABLE_SEVERITIES = frozenset({Severity.WARNING, Severity.ERROR, Severity.CRITICAL})


def _cited_event_ids(value: object) -> set[str]:
    """Event IDs cited anywhere inside specialist finding payloads (evidence_event_ids lists)."""
    if isinstance(value, dict):
        found = {item for item in value.get("evidence_event_ids", []) if isinstance(item, str)} if isinstance(value.get("evidence_event_ids"), list) else set()
        return found.union(*(_cited_event_ids(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_cited_event_ids(item) for item in value))
    return set()


def _measures(event: NormalizedEvent) -> dict[str, float]:
    attributes = event.metadata.get("source_attributes")
    if not isinstance(attributes, dict):
        return {}
    return {key: float(value) for key, value in attributes.items() if isinstance(value, (int, float)) and not isinstance(value, bool)}


def _compact_number(value: float) -> float | int:
    """Lossless and compact: integral values print without a trailing .0."""
    return int(value) if float(value).is_integer() else round(value, 3)


def _series_key(event: NormalizedEvent) -> tuple[str, ...]:
    attributes = event.metadata.get("source_attributes")
    metric = attributes.get("metric_name") if isinstance(attributes, dict) else None
    ids = tuple(getattr(event, name) or "" for name in ("service", "vm_id", "pod_id", "database_id", "host_id"))
    return (str(event.metadata.get("record_kind") or event.source), event.layer.value, str(metric or ""), *ids)


def _summarize_events(request: MainRcaRequest) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    """Large incidents: distinct warnings/errors stay verbatim; repeats and routine records become value series.

    Every condensed value is kept with its UTC time, and each series names real event IDs (first, last, and the
    minimum and maximum of each measure) so conclusions stay citeable. Validation still accepts any supplied event.
    """
    ordered = sorted(request.events, key=lambda item: (item.timestamp, item.event_id))
    if len(ordered) <= VERBATIM_EVENT_LIMIT:
        return [_compact_event(item) for item in ordered], [], {}
    cited = set().union(*(_cited_event_ids(item.finding) for item in request.incident.findings)) if request.incident.findings else set()
    groups: dict[tuple[str, ...], list[NormalizedEvent]] = {}
    notable: dict[tuple[str, ...], list[NormalizedEvent]] = {}
    verbatim: dict[str, NormalizedEvent] = {}
    for event in ordered:
        if event.severity in _NOTABLE_SEVERITIES or event.metadata.get("record_kind") == "app.error":
            notable.setdefault((*_series_key(event), event.severity.value, event.message), []).append(event)
        else:
            groups.setdefault(_series_key(event), []).append(event)
    # A repeated warning/error (same kind, component, severity, and message) keeps its first and last occurrences
    # verbatim; the occurrences in between join a series that keeps each one's time and numeric fields.
    for key, members in notable.items():
        for event in members[:NOTABLE_HEAD] + members[-NOTABLE_TAIL:]:
            verbatim[event.event_id] = event
        if len(members) > NOTABLE_HEAD + NOTABLE_TAIL:
            groups.setdefault(key, []).extend(members[NOTABLE_HEAD:-NOTABLE_TAIL])
    series: list[dict[str, object]] = []
    for key, members in groups.items():
        repeated_notable = len(key) > len(_series_key(members[0]))
        if not repeated_notable:  # routine series endpoints stay verbatim for context (notable head/tail already are)
            for endpoint in (members[0], members[-1]):
                verbatim[endpoint.event_id] = endpoint
        inner = [event for event in members if event.event_id not in verbatim]
        if not inner:
            continue
        sample = inner[0]
        entry: dict[str, object] = {
            "record_kind": key[0], "layer": key[1], **({"metric_name": key[2]} if key[2] else {}),
            **({"severity": key[-2], "message": key[-1], "repeated_notable": True} if repeated_notable else {}),
            **{name: getattr(sample, name) for name in ("service", "vm_id", "pod_id", "database_id", "host_id") if getattr(sample, name)},
            "count": len(inner),
            # Specialist-cited events inside a series keep their ID, indexed into offsets_s/values, so they stay citeable.
            **({"cited_event_ids": {event.event_id: index for index, event in enumerate(inner) if event.event_id in cited}}
               if any(event.event_id in cited for event in inner) else {}),
            "first": {"timestamp": inner[0].timestamp.isoformat().replace("+00:00", "Z"), "event_id": inner[0].event_id},
            "last": {"timestamp": inner[-1].timestamp.isoformat().replace("+00:00", "Z"), "event_id": inner[-1].event_id},
        }
        # Times are whole-second offsets (milliseconds kept when present) from first.timestamp.
        entry["offsets_s"] = [_compact_number((event.timestamp - inner[0].timestamp).total_seconds()) for event in inner]
        names = sorted({name for event in inner for name in _measures(event)})
        if names:
            columns = {name: [_measures(event).get(name) for event in inner] for name in names}
            # A measure with one value throughout is stated once; every other measure keeps one value per event.
            constant = {name: _compact_number(column[0]) for name, column in columns.items() if column[0] is not None and len(set(column)) == 1}
            entry["values"] = {name: [None if value is None else _compact_number(value) for value in column] for name, column in columns.items() if name not in constant}
            if constant:
                entry["constant"] = constant
            extremes: dict[str, dict[str, str]] = {}
            for name in names:
                present = [event for event in inner if name in _measures(event)]
                if present and name not in constant:
                    extremes[name] = {
                        "min_event_id": min(present, key=lambda event: _measures(event)[name]).event_id,
                        "max_event_id": max(present, key=lambda event: _measures(event)[name]).event_id,
                    }
            entry["extremes"] = extremes
        else:
            messages: dict[str, int] = {}
            for event in inner:
                messages[event.message] = messages.get(event.message, 0) + 1
            entry["messages"] = dict(sorted(messages.items(), key=lambda item: -item[1])[:5])
            entry["distinct_messages"] = len(messages)
        series.append(entry)
    condensed = sum(int(str(item["count"])) for item in series)
    listed = [_compact_event(event) for event in sorted(verbatim.values(), key=lambda item: (item.timestamp, item.event_id))]
    summary: dict[str, object] = {
        "total_events": len(ordered), "verbatim_events": len(listed), "condensed_events": condensed, "series": len(series),
        "note": "Each distinct warning/error (first and last occurrences of repeats) and each series' endpoints are "
        "listed in events; every other event is in event_series with its values and UTC time, and specialist-cited "
        "events there are named in cited_event_ids. Cite the series' event IDs when relying on a series.",
    }
    return listed, series, summary


def _compact_incident(request: MainRcaRequest) -> dict[str, object]:
    """With events supplied, source events become ID references and event facts appear once in ``events``."""
    incident = request.incident.model_dump(mode="json")
    # Pairwise grouping rationales repeat the correlation edges; keep why the incident was grouped, per reason.
    pairs: dict[str, int] = {}
    topology_ids: dict[str, set[str]] = {}
    for rationale in request.incident.rationales:
        pairs[rationale.reason.value] = pairs.get(rationale.reason.value, 0) + 1
        topology_ids.setdefault(rationale.reason.value, set()).update(rationale.topology_relationship_ids)
    incident["rationales"] = [
        {"reason": reason, "event_pairs": count, "topology_relationship_ids": sorted(topology_ids[reason])}
        for reason, count in pairs.items()
    ]
    if request.events:
        # Every source event is listed once in INPUT.events; repeating its ID here would scale the prompt for nothing.
        incident.pop("source_events")
        incident["event_count"] = len(request.incident.source_events)
    return incident


TOPOLOGY_EVIDENCE_LIMIT = 5


def _bounded_evidence(reference_ids: list[str], listed: set[str]) -> dict[str, object]:
    """Up to TOPOLOGY_EVIDENCE_LIMIT supporting IDs (preferring events listed verbatim) plus the full count.

    Every event on one instance supports its runs_on relationship, so the full list grows with the incident.
    """
    if len(reference_ids) <= TOPOLOGY_EVIDENCE_LIMIT:
        return {"evidence_reference_ids": reference_ids}
    preferred = [item for item in reference_ids if item in listed] + [item for item in reference_ids if item not in listed]
    return {"evidence_reference_ids": preferred[:TOPOLOGY_EVIDENCE_LIMIT], "evidence_count": len(reference_ids)}


def _compact_topology(topology: TopologyResolution, listed: set[str] | None = None) -> dict[str, object]:
    """Observed structure with bounded evidence IDs, plus explicitly unavailable relationships."""
    component = lambda item: {"component_id": item.component_id, "kind": item.kind.value, "layer": item.layer.value}  # noqa: E731
    return {
        "relationships": [
            {
                "relationship_id": item.relationship_id,
                "relationship_type": item.relationship_type.value,
                "source": component(item.source),
                "target": component(item.target),
                "provenance_kind": item.provenance.kind.value,
                **_bounded_evidence([reference.reference_id for reference in item.evidence], listed or set()),
            }
            for item in topology.relationships
        ],
        "unavailable": [
            {"source": component(item.source), "target_kind": item.target_kind.value, "relationship_type": item.relationship_type.value, "reason": item.reason}
            for item in topology.unavailable
        ],
    }


def _compact_workflow(workflow: SpecialistWorkflowResult) -> dict[str, object]:
    """Execution outcomes only; evidence-bound specialist findings are already in incident.findings."""
    return {
        "status": workflow.status.value,
        "executions": [
            {"layer": item.layer.value, "specialist": item.specialist, "status": item.status.value, "error_code": item.error_code}
            for item in workflow.executions
        ],
    }


def _excerpt(text: str, limit: int = REPAIR_EXCERPT_CHARACTERS) -> str:
    """Bound text echoed into a repair prompt; the full original context is already resent."""
    return text if len(text) <= limit else f"{text[:limit]}... [truncated {len(text) - limit} characters]"


SPECIALIST_FOR_LAYER = {Layer.DATABASE: "A1", Layer.VM: "A2", Layer.HYPERVISOR: "A3"}
REASONING_RULES = (
    "Cross-layer reasoning rules:",
    "1. Separate the component that REPORTED each error (where it was logged or measured) from the component most "
    "likely RESPONSIBLE for it. Never attribute a failure to the reporting layer by default; for example a database "
    "error may be caused by application behaviour (concurrent writes, long transactions) or by VM/OS resource pressure.",
    "2. Weigh application, database, VM/OS, and infrastructure explanations. For each candidate cite supporting and "
    "contradicting evidence; put non-selected candidates in alternative_hypotheses.",
    "3. Temporal overlap and topology do not prove causation. Every causal_chain edge needs evidence that the cause "
    "preceded and plausibly produced the effect (for example slower database operations during resource pressure). "
    "The causal_chain must be acyclic: an effect never leads back to its own cause.",
    "Each causal_chain edge and the report-level evidence must cite at least one actual event ID from INPUT.events; "
    "metric, log, and event are valid evidence kinds when their reference_id is an event ID. Findings and topology "
    "may supplement but cannot replace those event citations.",
    "4. If the evidence cannot distinguish the candidates, say so in uncertainty, lower confidence, and name the "
    "missing telemetry that would distinguish them.",
    "5. Never claim evidence for layers listed in telemetry_coverage.unavailable or with no events.",
)


def telemetry_coverage(request: MainRcaRequest) -> dict[str, object]:
    """Which layers have evidence and specialist analysis, and which cannot be observed at all."""
    counts: dict[str, int] = {layer.value: 0 for layer in Layer}
    for node in request.graph.nodes:
        counts[node.layer.value] += 1
    executions = {item.layer: item for item in request.workflow.executions} if request.workflow else {}
    layers: dict[str, object] = {}
    for layer in Layer:
        present = layer in request.incident.affected_layers
        execution = executions.get(layer)
        layers[layer.value] = {
            "has_evidence": present,
            "event_count": counts[layer.value] if request.graph.nodes else None,
            "specialist": SPECIALIST_FOR_LAYER.get(layer, "none (evidence only)"),
            "specialist_status": execution.status.value if execution else "not_run",
        }
    unavailable = sorted(
        {f"{item.source.kind.value} {item.source.component_id} -> {item.target_kind.value}: {item.reason}" for item in request.topology.unavailable}
    )
    return {"layers": layers, "unavailable": unavailable}


def build_main_rca_prompt(request: MainRcaRequest, prompt_version: str) -> str:
    """Build bounded, canonical context; correlation edges are candidates, not causes."""
    events, series, event_summary = _summarize_events(request)
    payload = {
        "incident": _compact_incident(request),
        "events": events,
        "correlation_graph": _compact_graph(request.graph, include_nodes=not request.events),
        "specialist_workflow": _compact_workflow(request.workflow) if request.workflow else None,
        "topology": _compact_topology(request.topology, {str(item["event_id"]) for item in events}),
        **({"event_series": series, "event_summary": event_summary} if event_summary else {}),
        "telemetry_coverage": telemetry_coverage(request),
        "knowledge": [
            {"chunk_id": item.chunk.chunk_id, "source": item.chunk.source, "section": item.chunk.section, "text": item.chunk.text}
            for item in request.knowledge
        ],
    }
    return "\n".join((
        f"CLOUDRCA_MAIN_RCA_PROMPT_VERSION={prompt_version}",
        "Correlation edges are candidates only; make causal claims only when cited evidence supports them. "
        "correlation_graph.edges lists the highest-scoring candidates; edge_summary counts any omitted.",
        "Event details are in events (or incident.source_events provenance when events is empty); graph edges and "
        "topology reference events by ID.",
        "For large incidents, event_summary and event_series condense routine records: every value is kept with its "
        "UTC time (offsets_s from first.timestamp; constant states unchanging measures once); cite the series' "
        "first/last/min/max event IDs when a conclusion depends on a series.",
        "topology lists observed structure (what runs on or depends on what) with its supporting evidence; it is not "
        "evidence that one component caused another's failure; evidence_count gives the full support when only some IDs are listed. "
        "topology.unavailable lists relationships that cannot be observed.",
        *REASONING_RULES,
        "Every evidence reference must use an ID from INPUT. Disclose partial specialist execution in uncertainty.",
        "When the incident spans more than one layer, alternative_hypotheses and uncertainty are required.",
        "Return exactly one JSON object validating against RCA_REPORT_SCHEMA.",
        f"RCA_REPORT_SCHEMA={json.dumps(RCAReport.model_json_schema(mode='validation'), sort_keys=True, separators=(',', ':'))}",
        f"INPUT={json.dumps(payload, sort_keys=True, separators=(',', ':'))}",
    ))


def _allowed_ids(request: MainRcaRequest) -> set[str]:
    allowed = {item.reference_id for item in request.incident.source_events}
    allowed.update(item.chunk.chunk_id for item in request.knowledge)
    allowed.update(item.finding_id for item in request.incident.findings)
    allowed.update(item.relationship_id for item in request.topology.relationships)
    for edge in request.graph.edges:
        allowed.update(reference.reference_id for reference in edge.evidence)
        allowed.update(edge.topology_relationship_ids)
    return allowed


def _cyclic_edge_indices(report: RCAReport) -> list[int]:
    """Indices of causal_chain edges that lie on a cycle (the edge's cause is reachable from its effect)."""
    successors: dict[str, set[str]] = {}
    for edge in report.causal_chain:
        successors.setdefault(edge.cause_id, set()).add(edge.effect_id)

    def reachable(start: str, target: str) -> bool:
        seen, pending = {start}, [start]
        while pending:
            node = pending.pop()
            if node == target:
                return True
            for following in successors.get(node, ()):
                if following not in seen:
                    seen.add(following)
                    pending.append(following)
        return False

    return [index for index, edge in enumerate(report.causal_chain) if reachable(edge.effect_id, edge.cause_id)]


def validate_main_rca_report(raw: str, request: MainRcaRequest, config: SpecialistRuntimeConfig) -> RCAReport:
    try:
        report = RCAReport.model_validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False)
        issues = tuple(_schema_issue(item) for item in errors)
        raise MainRcaValidationError(json.dumps(errors, default=str, sort_keys=True), issues) from exc
    if report.incident_id != request.incident.incident_id:
        raise MainRcaValidationError(
            "report incident_id does not match input incident", (_issue("incident_id_mismatch", "incident_id"),)
        )
    allowed = _allowed_ids(request)
    references = [
        *((f"evidence.{index}", item) for index, item in enumerate(report.evidence)),
        *(
            (f"causal_chain.{edge_index}.evidence.{index}", item)
            for edge_index, edge in enumerate(report.causal_chain)
            for index, item in enumerate(edge.evidence)
        ),
        *(
            (f"alternative_hypotheses.{hypothesis_index}.evidence.{index}", item)
            for hypothesis_index, hypothesis in enumerate(report.alternative_hypotheses)
            for index, item in enumerate(hypothesis.evidence)
        ),
    ]
    unknown = tuple(
        _issue("unknown_evidence_reference", f"{path}.reference_id") for path, item in references if item.reference_id not in allowed
    )
    if unknown:
        raise MainRcaValidationError("report references evidence outside the supplied context", unknown)
    event_ids = {item.reference_id for item in request.incident.source_events}
    missing_event_evidence = []
    if not any(item.reference_id in event_ids for item in report.evidence):
        missing_event_evidence.append(_issue("missing_event_evidence", "evidence"))
    missing_event_evidence.extend(
        _issue("missing_event_evidence", f"causal_chain.{index}.evidence")
        for index, edge in enumerate(report.causal_chain)
        if not any(item.reference_id in event_ids for item in edge.evidence)
    )
    if missing_event_evidence:
        raise MainRcaValidationError(
            "report-level and causal-chain evidence must cite supplied events", tuple(missing_event_evidence)
        )
    cyclic = _cyclic_edge_indices(report)
    if cyclic:
        raise MainRcaValidationError(
            "causal_chain must be acyclic: an effect cannot lead back to its own cause",
            tuple(_issue("cyclic_causal_chain", f"causal_chain.{index}") for index in cyclic),
        )
    components = {item.component_id for item in request.incident.affected_components}
    if report.root_cause_component.component_id not in components:
        raise MainRcaValidationError(
            "root cause component is not affected by the incident",
            (_issue("root_cause_component_not_affected", "root_cause_component.component_id"),),
        )
    partial = request.workflow is not None and request.workflow.status is not WorkflowStatus.SUCCESS
    if partial and not report.uncertainty:
        raise MainRcaValidationError(
            "partial specialist execution requires explicit uncertainty", (_issue("missing_uncertainty", "uncertainty"),)
        )
    if len(request.incident.affected_layers) > 1:
        # Cross-layer incidents must show the competing explanations and what remains uncertain.
        gaps: list[dict[str, str]] = []
        if not report.alternative_hypotheses:
            gaps.append(_issue("missing_alternative_hypotheses", "alternative_hypotheses"))
        if not report.uncertainty:
            gaps.append(_issue("missing_uncertainty", "uncertainty"))
        if gaps:
            raise MainRcaValidationError(
                "cross-layer incidents require alternative hypotheses and explicit uncertainty", tuple(gaps)
            )
    return report.model_copy(
        update={
            "runtime": ModelRuntimeProvenance(
                provider=config.provider_profile,
                model=config.model,
                harness="jcode",
            )
        }
    )


class MainRcaAgent:
    """Use the validated JCode transport for bounded GLM RCA generation."""

    def __init__(
        self,
        config: SpecialistRuntimeConfig,
        transport: JCodeTransport,
        clock: Callable[[], float] = time.perf_counter,
        logger: EventLogger | None = None,
    ) -> None:
        self._config, self._transport, self._clock = config, transport, clock
        self._logger = logger if logger is not None else structlog.get_logger("cloudrca.main_rca")

    def analyze(self, request: MainRcaRequest) -> MainRcaResult:
        correlation_id = request.correlation_id or str(uuid4())
        started, repairs, usage, session_id = self._clock(), 0, TokenUsage(), None
        prompt = build_main_rca_prompt(request, self._config.prompt_version)
        if len(prompt) > self._config.max_prompt_characters:
            raise MainRcaError(SpecialistFailureCode.CONTEXT_LIMIT, "main RCA prompt exceeds configured context limit")
        try:
            with self._transport.session(self._config, correlation_id) as session:
                current = prompt
                while True:
                    response = session.complete(current, self._config.timeout_seconds)
                    session_id, usage = response.session_id, usage.plus(response.usage)
                    try:
                        report = validate_main_rca_report(unwrap_code_fence(response.text), request, self._config)
                        break
                    except ValueError as exc:
                        # Log only issue types, field paths, and sizes: never the prompt, response, or evidence text.
                        issues = exc.issues if isinstance(exc, MainRcaValidationError) else ({"type": "invalid_report", "loc": ""},)
                        self._logger.info(
                            "main_rca_validation_failed",
                            correlation_id=correlation_id,
                            provider_profile=self._config.provider_profile,
                            model=self._config.model,
                            attempt=repairs + 1,
                            response_characters=len(response.text),
                            issue_count=len(issues),
                            issues=list(issues[:20]),
                        )
                        if repairs >= self._config.max_repair_attempts:
                            raise MainRcaError(SpecialistFailureCode.INVALID_OUTPUT, str(exc)) from exc
                        repairs += 1
                        current = _repair_prompt(
                            prompt,
                            _excerpt(response.text),
                            _excerpt(str(exc)),
                            self._config.prompt_version,
                            "RCAReport",
                        )
                        if len(current) > self._config.max_prompt_characters:
                            self._logger.info(
                                "main_rca_repair_prompt_too_large",
                                correlation_id=correlation_id,
                                prompt_characters=len(prompt),
                                repair_prompt_characters=len(current),
                                max_prompt_characters=self._config.max_prompt_characters,
                            )
                            raise MainRcaError(SpecialistFailureCode.CONTEXT_LIMIT, "main RCA repair prompt exceeds configured context limit")
        except MainRcaError:
            raise
        except JCodeTimeoutError as exc:
            raise MainRcaError(SpecialistFailureCode.TIMEOUT, str(exc)) from exc
        except JCodeConfigurationError as exc:
            raise MainRcaError(SpecialistFailureCode.CONFIGURATION, str(exc)) from exc
        except JCodeProviderError as exc:
            raise MainRcaError(SpecialistFailureCode.PROVIDER, str(exc)) from exc
        return MainRcaResult(report, MainRcaMetadata(correlation_id, self._config.provider_profile, self._config.model, self._config.prompt_version, max(0, (self._clock() - started) * 1000), repairs, usage, session_id))
