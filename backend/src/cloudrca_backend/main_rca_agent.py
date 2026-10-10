"""GLM-backed, evidence-constrained main RCA agent."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contracts import ModelRuntimeProvenance, RCAReport
from .correlation import CorrelationGraph
from .incident_grouping import CandidateIncident
from .knowledge import RetrievedChunk
from .orchestration import SpecialistWorkflowResult, WorkflowStatus
from .specialist_runtime import (
    JCodeConfigurationError,
    JCodeProviderError,
    JCodeTimeoutError,
    JCodeTransport,
    SpecialistFailureCode,
    SpecialistRuntimeConfig,
    TokenUsage,
    _repair_prompt,
)


class MainRcaRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    incident: CandidateIncident
    graph: CorrelationGraph
    workflow: SpecialistWorkflowResult | None = None
    knowledge: tuple[RetrievedChunk, ...] = ()
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


class MainRcaError(RuntimeError):
    def __init__(self, code: SpecialistFailureCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def build_main_rca_prompt(request: MainRcaRequest, prompt_version: str) -> str:
    """Build bounded, canonical context; correlation edges are candidates, not causes."""
    payload = {
        "incident": request.incident.model_dump(mode="json"),
        "correlation_graph": request.graph.model_dump(mode="json"),
        "specialist_workflow": request.workflow.model_dump(mode="json") if request.workflow else None,
        "knowledge": [
            {"chunk_id": item.chunk.chunk_id, "source": item.chunk.source, "section": item.chunk.section, "text": item.chunk.text}
            for item in request.knowledge
        ],
    }
    return "\n".join((
        f"CLOUDRCA_MAIN_RCA_PROMPT_VERSION={prompt_version}",
        "Correlation edges are candidates only; make causal claims only when cited evidence supports them.",
        "Every evidence reference must use an ID from INPUT. Disclose partial specialist execution in uncertainty.",
        "Return exactly one JSON object validating against RCA_REPORT_SCHEMA.",
        f"RCA_REPORT_SCHEMA={json.dumps(RCAReport.model_json_schema(mode='validation'), sort_keys=True, separators=(',', ':'))}",
        f"INPUT={json.dumps(payload, sort_keys=True, separators=(',', ':'))}",
    ))


def _allowed_ids(request: MainRcaRequest) -> set[str]:
    allowed = {item.reference_id for item in request.incident.source_events}
    allowed.update(item.chunk.chunk_id for item in request.knowledge)
    allowed.update(item.finding_id for item in request.incident.findings)
    for edge in request.graph.edges:
        allowed.update(reference.reference_id for reference in edge.evidence)
        allowed.update(edge.topology_relationship_ids)
    return allowed


def validate_main_rca_report(raw: str, request: MainRcaRequest, config: SpecialistRuntimeConfig) -> RCAReport:
    try:
        report = RCAReport.model_validate_json(raw)
    except ValidationError as exc:
        raise ValueError(json.dumps(exc.errors(include_input=False, include_url=False), sort_keys=True)) from exc
    if report.incident_id != request.incident.incident_id:
        raise ValueError("report incident_id does not match input incident")
    allowed = _allowed_ids(request)
    references = [*report.evidence, *(item for edge in report.causal_chain for item in edge.evidence), *(item for hypothesis in report.alternative_hypotheses for item in hypothesis.evidence)]
    if any(reference.reference_id not in allowed for reference in references):
        raise ValueError("report references evidence outside the supplied context")
    components = {item.component_id for item in request.incident.affected_components}
    if report.root_cause_component.component_id not in components:
        raise ValueError("root cause component is not affected by the incident")
    partial = request.workflow is not None and request.workflow.status is not WorkflowStatus.SUCCESS
    if partial and not report.uncertainty:
        raise ValueError("partial specialist execution requires explicit uncertainty")
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

    def __init__(self, config: SpecialistRuntimeConfig, transport: JCodeTransport, clock: Callable[[], float] = time.perf_counter) -> None:
        self._config, self._transport, self._clock = config, transport, clock

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
                        report = validate_main_rca_report(response.text, request, self._config)
                        break
                    except ValueError as exc:
                        if repairs >= self._config.max_repair_attempts:
                            raise MainRcaError(SpecialistFailureCode.INVALID_OUTPUT, str(exc)) from exc
                        repairs += 1
                        current = _repair_prompt(prompt, response.text, str(exc), self._config.prompt_version, "RCAReport")
                        if len(current) > self._config.max_prompt_characters:
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
