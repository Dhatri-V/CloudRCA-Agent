"""GLM-backed, evidence-constrained main RCA agent."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from uuid import uuid4

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contracts import ModelRuntimeProvenance, RCAReport
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
            prompt_version=environ.get("CLOUDRCA_MAIN_RCA_PROMPT_VERSION", "main-rca-v1").strip(),
        )
    except ValidationError as exc:
        raise SettingsError(f"Invalid main RCA configuration: {exc}") from exc
    if config.api_key_env and not environ.get(config.api_key_env, "").strip():
        raise SettingsError(f"Missing provider credential environment variable: {config.api_key_env}")
    return config


def _compact_graph(graph: CorrelationGraph) -> dict[str, object]:
    """Reference events by ID only; their full provenance appears once, in incident.source_events."""
    return {
        "incident_id": graph.incident_id,
        "nodes": [item.model_dump(mode="json", exclude={"provenance", "schema_version"}) for item in graph.nodes],
        "edges": [
            {
                **edge.model_dump(mode="json", exclude={"evidence", "schema_version"}),
                "evidence_reference_ids": [reference.reference_id for reference in edge.evidence],
            }
            for edge in graph.edges
        ],
    }


def _compact_topology(topology: TopologyResolution) -> dict[str, object]:
    """Observed structure with its evidence IDs, plus explicitly unavailable relationships."""
    component = lambda item: {"component_id": item.component_id, "kind": item.kind.value, "layer": item.layer.value}  # noqa: E731
    return {
        "relationships": [
            {
                "relationship_id": item.relationship_id,
                "relationship_type": item.relationship_type.value,
                "source": component(item.source),
                "target": component(item.target),
                "provenance_kind": item.provenance.kind.value,
                "evidence_reference_ids": [reference.reference_id for reference in item.evidence],
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


def build_main_rca_prompt(request: MainRcaRequest, prompt_version: str) -> str:
    """Build bounded, canonical context; correlation edges are candidates, not causes."""
    payload = {
        "incident": request.incident.model_dump(mode="json"),
        "correlation_graph": _compact_graph(request.graph),
        "specialist_workflow": _compact_workflow(request.workflow) if request.workflow else None,
        "topology": _compact_topology(request.topology),
        "knowledge": [
            {"chunk_id": item.chunk.chunk_id, "source": item.chunk.source, "section": item.chunk.section, "text": item.chunk.text}
            for item in request.knowledge
        ],
    }
    return "\n".join((
        f"CLOUDRCA_MAIN_RCA_PROMPT_VERSION={prompt_version}",
        "Correlation edges are candidates only; make causal claims only when cited evidence supports them.",
        "Event details and provenance are in incident.source_events; graph nodes and edges reference events by ID.",
        "topology lists observed structure (what runs on or depends on what) with its supporting evidence; it is not "
        "evidence that one component caused another's failure. topology.unavailable lists relationships that cannot be observed.",
        "Every evidence reference must use an ID from INPUT. Disclose partial specialist execution in uncertainty.",
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
