"""Issue #17 report rendering and evidence-integrity tests."""

from datetime import datetime, timezone

import pytest
from cloudrca_backend.contracts import (
    ComponentRef,
    Confidence,
    EvidenceReference,
    NormalizedEvent,
    Provenance,
    RCAReport,
    TimelineEntry,
)
from cloudrca_backend.reporting import ReportContext, render_json, render_markdown
from pydantic import ValidationError

UTC = datetime(2025, 1, 1, tzinfo=timezone.utc)


def report() -> RCAReport:
    evidence = EvidenceReference(evidence_id="e-1", kind="event", reference_id="event-1")
    component = ComponentRef(component_id="host-1", kind="host", layer="host_hypervisor")
    return RCAReport(incident_id="incident-1", probable_root_cause="Host queue saturation", root_cause_layer="host_hypervisor", root_cause_component=component, timeline=(TimelineEntry(timestamp=UTC, summary="Queue rose", event_ids=("event-1",)),), causal_chain=({"cause_id": "host-1", "effect_id": "event-1", "summary": "Host delayed guest IO", "confidence": {"score": 0.8, "level": "high"}, "evidence": [evidence]},), evidence=(evidence,), confidence=Confidence(score=0.8, level="high"), runtime={"provider": "zai", "model": "glm-5.3", "harness": "jcode"})


def context() -> ReportContext:
    event = NormalizedEvent(event_id="event-1", timestamp=UTC, source="test", layer="host_hypervisor", severity="critical", host_id="host-1", message="queue saturated", provenance=Provenance(kind="observed", source="test"))
    return ReportContext(report=report(), events=(event,))


def test_json_and_markdown_render_from_one_validated_model() -> None:
    value = context()
    assert '"incident_id": "incident-1"' in render_json(value)
    markdown = render_markdown(value)
    assert "**Severity:** critical" in markdown
    assert "2025-01-01T00:00:00Z" in markdown
    assert "[event] event-1" in markdown


def test_dangling_citation_and_missing_timeline_event_block_rendering() -> None:
    bad = report().model_copy(update={"evidence": (EvidenceReference(evidence_id="e", kind="event", reference_id="missing"),)})
    with pytest.raises(ValidationError, match="dangling"):
        ReportContext(report=bad, events=context().events)
    bad_timeline = report().model_copy(update={"timeline": (TimelineEntry(timestamp=UTC, summary="missing", event_ids=("missing",)),)})
    with pytest.raises(ValidationError, match="timeline"):
        ReportContext(report=bad_timeline, events=context().events)
