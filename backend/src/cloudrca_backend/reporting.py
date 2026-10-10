"""Validated JSON and Markdown views of canonical RCA reports."""

from __future__ import annotations

import json
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import EvidenceReference, NormalizedEvent, RCAReport, Severity
from .correlation import CorrelationGraph


class ReportIntegrityError(ValueError):
    """Raised when a report cannot be safely rendered."""


class ReportContext(BaseModel):
    """All evidence required to render and validate one canonical report."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    report: RCAReport
    events: tuple[NormalizedEvent, ...] = Field(min_length=1)
    graph: CorrelationGraph | None = None
    evidence: tuple[EvidenceReference, ...] = ()

    @model_validator(mode="after")
    def references_must_resolve(self) -> ReportContext:
        event_ids = {event.event_id for event in self.events}
        if len(event_ids) != len(self.events):
            raise ReportIntegrityError("report context event IDs must be unique")
        if self.graph and self.graph.incident_id != self.report.incident_id:
            raise ReportIntegrityError("correlation graph incident does not match report")
        if self.graph and any(node.event_id not in event_ids for node in self.graph.nodes):
            raise ReportIntegrityError("correlation graph references missing events")
        available = event_ids | {item.reference_id for item in self.evidence}
        if self.graph:
            available.update(reference.reference_id for edge in self.graph.edges for reference in edge.evidence)
            available.update(identifier for edge in self.graph.edges for identifier in edge.topology_relationship_ids)
        if any(event_id not in event_ids for entry in self.report.timeline for event_id in entry.event_ids):
            raise ReportIntegrityError("report timeline references missing events")
        if any(reference.reference_id not in available for reference in _references(self.report)):
            raise ReportIntegrityError("report has dangling evidence citations")
        return self


def _references(report: RCAReport) -> Iterable[EvidenceReference]:
    yield from report.evidence
    yield from (reference for edge in report.causal_chain for reference in edge.evidence)
    yield from (reference for hypothesis in report.alternative_hypotheses for reference in hypothesis.evidence)


def render_json(context: ReportContext) -> str:
    """Render the validated canonical model as deterministic JSON."""
    return json.dumps(context.report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def render_markdown(context: ReportContext) -> str:
    """Render reviewable Markdown from the same canonical RCAReport model."""
    report = context.report
    severity = max((event.severity for event in context.events), key=lambda item: list(Severity).index(item))
    lines = [
        f"# RCA report: {report.incident_id}",
        "",
        f"**Severity:** {severity.value}",
        f"**Root cause:** {report.probable_root_cause}",
        f"**Root layer:** {report.root_cause_layer.value}",
        "",
        "## UTC timeline",
    ]
    lines.extend(f"- `{entry.timestamp.isoformat().replace('+00:00', 'Z')}` — {entry.summary} ({', '.join(entry.event_ids)})" for entry in report.timeline)
    lines.extend(("", "## Causal chain"))
    lines.extend(f"- {edge.summary} [hypothesis; evidence: {', '.join(reference.reference_id for reference in edge.evidence)}]" for edge in report.causal_chain)
    lines.extend(("", "## Alternatives"))
    lines.extend(f"- {item.summary} ({item.confidence.level.value})" for item in report.alternative_hypotheses or ())
    lines.extend(("", "## Evidence"))
    lines.extend(f"- [{reference.kind.value}] {reference.reference_id}" for reference in report.evidence)
    if report.uncertainty:
        lines.extend(("", "## Uncertainty", report.uncertainty))
    return "\n".join(lines) + "\n"
