"""Production dataset-to-report RCA pipeline executed by the FastAPI analysis job runner."""

from __future__ import annotations

import os
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4

import structlog
from pydantic import ValidationError

from .api import AnalysisResult, ArtifactKind, JobStatus
from .contracts import EvidenceKind, EvidenceReference, Layer, Severity
from .correlation import CorrelationConfig, CorrelationGraph, CrossLayerCorrelator
from .database_agent import DatabaseAgent
from .hypervisor_agent import HypervisorAgent
from .incident_grouping import CandidateIncident, GroupingConfig, IncidentGrouper
from .main_rca_agent import MainRcaAgent, MainRcaError, MainRcaRequest, MainRcaResult, load_main_rca_runtime_config
from .normalization import SourceRecord, normalize_batch, read_aiops2025_records
from .orchestration import (
    AnalysisBatch,
    SpecialistAgent,
    SpecialistOrchestrator,
    SpecialistStatus,
    SpecialistWorkflowResult,
    WorkflowStatus,
)
from .reporting import ReportContext, ReportIntegrityError, render_markdown
from .routing import RoutedEvent, RoutingConfig, RoutingLayer, load_routing_config, route_and_resolve
from .settings import SettingsError, load_settings
from .specialist_runtime import JCodeCliTransport, SpecialistRuntime, load_specialist_runtime_config
from .vm_agent import VMAgent

DEFINITIVE_LAYERS = frozenset({RoutingLayer.DATABASE, RoutingLayer.VM, RoutingLayer.HYPERVISOR})
SEVERITY_ORDER = tuple(Severity)

DatasetLoader = Callable[[str], tuple[str, bytes] | None]


class RcaAnalyzer(Protocol):
    def analyze(self, request: MainRcaRequest) -> MainRcaResult:
        """Produce one validated RCA report for a candidate incident."""
        ...


@dataclass(frozen=True, slots=True)
class AnalysisAgents:
    """LLM-backed stages; built per job so configuration errors fail that job, not the API."""

    specialists: Mapping[Layer, SpecialistAgent]
    main_rca: RcaAnalyzer


AgentFactory = Callable[[], AnalysisAgents]


class IncidentOutcome(StrEnum):
    REPORTED = "rca_reported"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    SPECIALIST_FAILED = "specialist_failed"
    RCA_FAILED = "rca_failed"
    NOT_ANALYZED = "not_analyzed"


SUCCESSFUL_OUTCOMES = frozenset({IncidentOutcome.REPORTED, IncidentOutcome.INSUFFICIENT_EVIDENCE})


@dataclass(frozen=True, slots=True)
class IncidentAnalysis:
    incident: CandidateIncident
    events: tuple[RoutedEvent, ...]
    outcome: IncidentOutcome
    explanation: str
    workflow: SpecialistWorkflowResult | None = None
    graph: CorrelationGraph | None = None
    report: dict[str, object] | None = None


def production_agents(environ: Mapping[str, str] | None = None) -> AnalysisAgents:
    """Build the configured Qwen specialists and GLM main RCA agent from the environment."""
    values = os.environ if environ is None else environ
    settings = load_settings(values)
    transport = JCodeCliTransport()
    runtime = SpecialistRuntime(load_specialist_runtime_config(settings, values), transport)
    return AnalysisAgents(
        # Agents return their own analysis models; the orchestrator only serializes ``output``.
        specialists={
            Layer.DATABASE: cast(SpecialistAgent, DatabaseAgent(runtime)),
            Layer.VM: cast(SpecialistAgent, VMAgent(runtime)),
            Layer.HYPERVISOR: cast(SpecialistAgent, HypervisorAgent(runtime)),
        },
        main_rca=MainRcaAgent(load_main_rca_runtime_config(settings, values), transport),
    )


def load_dataset_records(filename: str, content: bytes) -> tuple[SourceRecord, ...]:
    """Parse stored dataset bytes with the existing AIOps2025 reader, keeping the upload name as provenance."""
    with tempfile.TemporaryDirectory(prefix="cloudrca-dataset-") as temporary:
        path = Path(temporary) / (Path(filename).name or "dataset")
        path.write_bytes(content)
        return read_aiops2025_records(path, relative_to=temporary)


def _severity(events: tuple[RoutedEvent, ...]) -> Severity:
    return max((item.event.severity for item in events), key=SEVERITY_ORDER.index)


def _failed(error: str, artifacts: tuple[tuple[ArtifactKind, dict[str, object]], ...] = ()) -> AnalysisResult:
    return AnalysisResult(status=JobStatus.FAILED, artifacts=artifacts, error=error)


class RcaPipeline:
    """Run normalization, routing, specialists, grouping, correlation, main RCA, and reporting for one dataset."""

    def __init__(
        self,
        dataset_loader: DatasetLoader,
        agent_factory: AgentFactory,
        routing_config: RoutingConfig,
        *,
        grouping_config: GroupingConfig | None = None,
        correlation_config: CorrelationConfig | None = None,
        max_incidents: int = 10,
    ) -> None:
        if max_incidents < 1:
            raise ValueError("max_incidents must be at least one")
        self._dataset_loader = dataset_loader
        self._agent_factory = agent_factory
        self._routing_config = routing_config
        self._grouper = IncidentGrouper(grouping_config or GroupingConfig())
        self._correlator = CrossLayerCorrelator(correlation_config or CorrelationConfig())
        self._max_incidents = max_incidents
        self._logger = structlog.get_logger("cloudrca.pipeline")

    def __call__(self, payload: dict[str, object]) -> AnalysisResult:
        dataset_id = payload.get("dataset_id")
        if not isinstance(dataset_id, str) or not dataset_id:
            return _failed("analysis job does not reference a dataset")
        loaded = self._dataset_loader(dataset_id)
        if loaded is None:
            return _failed("dataset not found")
        filename, content = loaded

        batch = normalize_batch(load_dataset_records(filename, content))
        rejections = Counter(item.reason.value for item in batch.rejected)
        summary: dict[str, object] = {
            "stage": "normalization",
            "dataset_id": dataset_id,
            "normalization": batch.statistics.model_dump(mode="json"),
            "rejection_reasons": dict(sorted(rejections.items())),
        }
        if not batch.events:
            reasons = ", ".join(f"{reason}={count}" for reason, count in sorted(rejections.items())) or "dataset is empty"
            return _failed(f"no supported events after normalization ({reasons})", ((ArtifactKind.RUN, summary),))

        routed = tuple(route_and_resolve(event, self._routing_config) for event in batch.events)
        definitive = tuple(item for item in routed if item.routing.layer in DEFINITIVE_LAYERS)
        summary.update(stage="routing", routing=dict(sorted(Counter(item.routing.layer.value for item in routed).items())))
        event_artifacts = tuple(
            (ArtifactKind.EVENT, {**item.event.model_dump(mode="json"), "routing": item.routing.model_dump(mode="json")})
            for item in routed
        )
        if not definitive:
            return _failed("no events could be routed to a supported layer", ((ArtifactKind.RUN, summary), *event_artifacts))

        candidates = self._grouper.group(definitive).incidents
        by_id = {item.event.event_id: item for item in definitive}
        ranked = sorted(
            candidates,
            key=lambda incident: (
                -SEVERITY_ORDER.index(_severity(tuple(by_id[ref.reference_id] for ref in incident.source_events))),
                incident.time_window.start,
                incident.incident_id,
            ),
        )
        configuration_error: str | None = None
        agents: AnalysisAgents | None = None
        try:
            agents = self._agent_factory()
        except SettingsError as exc:
            configuration_error = f"LLM runtime is not configured: {exc}"

        analyses: list[IncidentAnalysis] = []
        for index, incident in enumerate(ranked):
            events = tuple(by_id[ref.reference_id] for ref in incident.source_events)
            if agents is None:
                analyses.append(IncidentAnalysis(incident, events, IncidentOutcome.NOT_ANALYZED, configuration_error or "not analyzed"))
            elif index >= self._max_incidents:
                analyses.append(
                    IncidentAnalysis(
                        incident, events, IncidentOutcome.NOT_ANALYZED,
                        f"Not analyzed: the per-job limit of {self._max_incidents} incidents was reached.",
                    )
                )
            else:
                analyses.append(self._analyze(incident, events, agents, dataset_id))

        outcomes = Counter(item.outcome.value for item in analyses)
        summary.update(stage="completed", incidents=len(analyses), outcomes=dict(sorted(outcomes.items())))
        status, error = self._status(analyses, configuration_error)
        summary["status"] = status.value
        artifacts = [(ArtifactKind.RUN, summary), *event_artifacts]
        for analysis in analyses:
            artifacts.extend(self._artifacts(analysis, dataset_id))
        self._logger.info("rca_pipeline_completed", dataset_id=dataset_id, status=status.value, outcomes=dict(outcomes))
        return AnalysisResult(status=status, artifacts=tuple(artifacts), error=error)

    def _analyze(
        self, incident: CandidateIncident, events: tuple[RoutedEvent, ...], agents: AnalysisAgents, dataset_id: str
    ) -> IncidentAnalysis:
        batch = AnalysisBatch(
            run_id=str(uuid4()), session_id=dataset_id, incident_id=incident.incident_id, trace_id=str(uuid4()), events=events
        )
        workflow = SpecialistOrchestrator(agents.specialists).run(batch)
        incident = self._grouper.attach_findings(incident, workflow)
        graph = self._correlator.correlate(incident, events)
        failed_codes = sorted(
            {(item.error_code.value if item.error_code else item.status.value) for item in workflow.executions if item.status is not SpecialistStatus.SUCCESS}
        )
        if workflow.status in {WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}:
            return IncidentAnalysis(
                incident, events, IncidentOutcome.SPECIALIST_FAILED,
                f"Analysis incomplete: specialist analysis failed ({', '.join(failed_codes)}).", workflow, graph,
            )
        if not incident.findings:
            if workflow.status is WorkflowStatus.PARTIAL:
                return IncidentAnalysis(
                    incident, events, IncidentOutcome.SPECIALIST_FAILED,
                    f"Analysis incomplete: no findings and some specialists failed ({', '.join(failed_codes)}).", workflow, graph,
                )
            layers = ", ".join(sorted(layer.value for layer in incident.affected_layers))
            return IncidentAnalysis(
                incident, events, IncidentOutcome.INSUFFICIENT_EVIDENCE,
                f"Insufficient evidence: specialists reported no anomaly for {len(events)} event(s) on {layers}; "
                "no root cause was inferred.",
                workflow, graph,
            )
        try:
            result = agents.main_rca.analyze(
                MainRcaRequest(incident=incident, graph=graph, workflow=workflow, correlation_id=batch.trace_id)
            )
            finding_references = tuple(
                EvidenceReference(evidence_id=f"evidence-{item.finding_id}", kind=EvidenceKind.FINDING, reference_id=item.finding_id)
                for item in incident.findings
            )
            context = ReportContext(
                report=result.report, events=tuple(item.event for item in events), graph=graph, evidence=finding_references
            )
            report: dict[str, object] = {**result.report.model_dump(mode="json"), "markdown": render_markdown(context)}
        except MainRcaError as exc:
            return IncidentAnalysis(
                incident, events, IncidentOutcome.RCA_FAILED, f"Analysis incomplete: main RCA failed ({exc.code.value}).", workflow, graph
            )
        except (ReportIntegrityError, ValidationError):
            return IncidentAnalysis(
                incident, events, IncidentOutcome.RCA_FAILED,
                "Analysis incomplete: the RCA report failed evidence integrity validation.", workflow, graph,
            )
        explanation = str(result.report.uncertainty or "")
        if workflow.status is WorkflowStatus.PARTIAL and not explanation:
            explanation = f"Some specialists failed ({', '.join(failed_codes)})."
        return IncidentAnalysis(incident, events, IncidentOutcome.REPORTED, explanation, workflow, graph, report)

    @staticmethod
    def _status(analyses: list[IncidentAnalysis], configuration_error: str | None) -> tuple[JobStatus, str | None]:
        if configuration_error:
            return JobStatus.FAILED, configuration_error
        succeeded = sum(item.outcome in SUCCESSFUL_OUTCOMES for item in analyses)
        if succeeded == 0:
            return JobStatus.FAILED, "no incident could be analyzed; see incident analysis outcomes"
        if succeeded < len(analyses):
            return JobStatus.PARTIAL, f"{len(analyses) - succeeded} of {len(analyses)} incidents were not fully analyzed"
        return JobStatus.COMPLETED, None

    @staticmethod
    def _artifacts(analysis: IncidentAnalysis, dataset_id: str) -> list[tuple[ArtifactKind, dict[str, object]]]:
        incident, report = analysis.incident, analysis.report
        layers = sorted(layer.value for layer in incident.affected_layers)
        layer = str(report["root_cause_layer"]) if report else layers[0] if len(layers) == 1 else "cross_layer"
        payload: dict[str, object] = {
            **incident.model_dump(mode="json"),
            "dataset_id": dataset_id,
            "status": "open",
            "severity": _severity(analysis.events).value,
            "timestamp": incident.time_window.start.isoformat(),
            "layer": layer,
            "event_ids": [ref.reference_id for ref in incident.source_events],
            "analysis_outcome": analysis.outcome.value,
            "summary": str(report["probable_root_cause"]) if report else analysis.explanation,
            "uncertainty": analysis.explanation or None,
        }
        if analysis.workflow is not None:
            payload["specialist_workflow"] = {
                "status": analysis.workflow.status.value,
                "executions": [
                    {
                        "layer": item.layer.value,
                        "specialist": item.specialist,
                        "status": item.status.value,
                        "attempt_count": item.attempt_count,
                        "error_code": item.error_code.value if item.error_code else None,
                    }
                    for item in analysis.workflow.executions
                ],
            }
        if report:
            payload.update(
                {key: report[key] for key in ("timeline", "causal_chain", "confidence", "root_cause_component", "alternative_hypotheses")}
            )
        artifacts: list[tuple[ArtifactKind, dict[str, object]]] = [(ArtifactKind.INCIDENT, payload)]
        for finding in incident.findings:
            body = finding.finding
            artifacts.append(
                (
                    ArtifactKind.FINDING,
                    {
                        "finding_id": finding.finding_id,
                        "incident_id": incident.incident_id,
                        "layer": finding.layer.value,
                        "specialist": finding.specialist,
                        "summary": f"{finding.specialist} {finding.layer.value}: {body.get('error_type', 'finding')}",
                        "severity": body.get("severity"),
                        "finding": body,
                    },
                )
            )
        if analysis.graph is not None:
            artifacts.append((ArtifactKind.GRAPH, analysis.graph.model_dump(mode="json")))
        if report:
            artifacts.append((ArtifactKind.REPORT, report))
        return artifacts


def build_production_pipeline(dataset_loader: DatasetLoader, environ: Mapping[str, str] | None = None) -> RcaPipeline:
    """Assemble the pipeline from environment configuration; LLM agents are created per job."""
    values = os.environ if environ is None else environ
    max_incidents = values.get("CLOUDRCA_PIPELINE_MAX_INCIDENTS", "10").strip()
    try:
        limit = int(max_incidents)
    except ValueError as exc:
        raise SettingsError(f"Invalid integer pipeline configuration: CLOUDRCA_PIPELINE_MAX_INCIDENTS={max_incidents!r}") from exc
    return RcaPipeline(
        dataset_loader,
        lambda: production_agents(values),
        load_routing_config(values.get("CLOUDRCA_ROUTING_CONFIG", "config/routing/aiops2025.yaml")),
        max_incidents=limit,
    )
