"""R1: application events are routed as evidence, never sent to A1/A2/A3, and always reach the main RCA."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from cloudrca_backend import pipeline as pipeline_module
from cloudrca_backend.api import ArtifactKind, JobStatus
from cloudrca_backend.contracts import Layer, NormalizedEvent, Provenance, ProvenanceKind, Severity
from cloudrca_backend.normalization import BatchStatistics, NormalizationBatch
from cloudrca_backend.pipeline import AnalysisAgents, RcaPipeline
from cloudrca_backend.routing import RoutingLayer, load_routing_config, route_event

ROUTING = load_routing_config(Path(__file__).resolve().parents[2] / "config" / "routing" / "aiops2025.yaml")
START = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)


def event(event_id: str, layer: Layer, seconds: int, message: str, **ids: str) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=event_id, timestamp=START + timedelta(seconds=seconds), source="test", layer=layer,
        severity=Severity.ERROR if layer is not Layer.VM else Severity.UNKNOWN, message=message,
        provenance=Provenance(kind=ProvenanceKind.OBSERVED, source="test", record_id=event_id), **ids,
    )


APP = event("app-500", Layer.APPLICATION, 60, "HTTP 500 on PUT /expenses/{expense_id}", service="splitwise-api", vm_id="i-0abc")
DB = event("db-locked", Layer.DATABASE, 55, "sqlite3.OperationalError: database is locked", database_id="splitwise-sqlite", vm_id="i-0abc")
VM = event("vm-cpu", Layer.VM, 0, "CPUUtilization=97.5", vm_id="i-0abc")


def run(monkeypatch: pytest.MonkeyPatch, events: tuple[NormalizedEvent, ...], agents: AnalysisAgents) -> Any:
    statistics = BatchStatistics(input=len(events), accepted=len(events), duplicates=0, rejected=0)
    monkeypatch.setattr(
        pipeline_module, "normalize_batch",
        lambda records: NormalizationBatch(events=events, duplicates=(), rejected=(), statistics=statistics),
    )
    return RcaPipeline(lambda _: ("injected.jsonl", b"{}"), lambda: agents, ROUTING)({"dataset_id": "d"})


def artifacts(result: Any, kind: ArtifactKind) -> list[dict[str, Any]]:
    return [payload for item, payload in result.artifacts if item is kind]


def test_application_events_route_to_the_application_layer_not_the_vm_specialist() -> None:
    assert route_event(APP, ROUTING).layer is RoutingLayer.APPLICATION
    assert route_event(DB, ROUTING).layer is RoutingLayer.DATABASE  # database identifier keeps precedence
    assert route_event(VM, ROUTING).layer is RoutingLayer.VM
    assert ROUTING.layers[Layer.APPLICATION].aliases == ()  # cannot change AIOps2025 alias routing


def test_application_evidence_reaches_main_rca_but_never_a_specialist(
    monkeypatch: pytest.MonkeyPatch, scripted_jcode: Any, agents_for: Any
) -> None:
    transport = scripted_jcode()
    result = run(monkeypatch, (VM, DB, APP), agents_for(transport))

    assert result.status is JobStatus.COMPLETED
    [incident] = artifacts(result, ArtifactKind.INCIDENT)
    assert sorted(incident["affected_layers"]) == ["application", "database", "vm_guest_os"]
    specialist_event_ids = {item["event_id"] for stage in ("database", "vm", "hypervisor") for prompt in transport.prompts[stage] for item in prompt["events"]}
    assert specialist_event_ids == {"db-locked", "vm-cpu"}
    [rca_input] = transport.prompts["main_rca"]
    assert "app-500" in {item["reference_id"] for item in rca_input["incident"]["source_events"]}
    components = {(item["component_id"], item["layer"]) for item in rca_input["incident"]["affected_components"]}
    assert ("splitwise-api", "application") in components


def test_main_rca_runs_on_application_evidence_when_specialists_find_nothing(
    monkeypatch: pytest.MonkeyPatch, scripted_jcode: Any, agents_for: Any
) -> None:
    transport = scripted_jcode({"database": [json.dumps({"findings": []})]})
    result = run(monkeypatch, (VM, DB, APP), agents_for(transport))
    [incident] = artifacts(result, ArtifactKind.INCIDENT)
    assert incident["analysis_outcome"] == "rca_reported"
    assert not artifacts(result, ArtifactKind.FINDING)
    assert len(transport.prompts["main_rca"]) == 1


def test_application_only_incident_skips_specialists_and_is_analyzed(
    monkeypatch: pytest.MonkeyPatch, scripted_jcode: Any, agents_for: Any
) -> None:
    app_only = (APP, event("app-500-b", Layer.APPLICATION, 75, "HTTP 500 on DELETE /expenses/{expense_id}", service="splitwise-api", vm_id="i-0abc"))
    transport = scripted_jcode()
    result = run(monkeypatch, app_only, agents_for(transport))
    [incident] = artifacts(result, ArtifactKind.INCIDENT)
    assert all(not transport.prompts[stage] for stage in ("database", "vm", "hypervisor"))
    assert "specialist_workflow" not in incident
    assert incident["analysis_outcome"] == "rca_reported" and len(transport.prompts["main_rca"]) == 1


def test_main_rca_may_name_the_application_as_root_cause(
    monkeypatch: pytest.MonkeyPatch, scripted_jcode: Any, agents_for: Any
) -> None:
    class ApplicationCause(scripted_jcode):  # type: ignore[misc, valid-type]
        def respond(self, prompt: str) -> str:
            text = super().respond(prompt)
            if "CLOUDRCA_MAIN_RCA_PROMPT_VERSION=" not in prompt:
                return text
            report = json.loads(text)
            report["root_cause_layer"] = "application"
            report["root_cause_component"] = {"component_id": "splitwise-api", "kind": "service", "layer": "application"}
            report["probable_root_cause"] = "Concurrent writes from splitwise-api (scripted test response)"
            return json.dumps(report)

    result = run(monkeypatch, (VM, DB, APP), agents_for(ApplicationCause()))
    [report] = artifacts(result, ArtifactKind.REPORT)
    assert report["root_cause_layer"] == "application"
    assert report["root_cause_component"]["component_id"] == "splitwise-api"


def test_incidents_without_application_events_keep_the_existing_insufficient_evidence_rule(
    monkeypatch: pytest.MonkeyPatch, scripted_jcode: Any, agents_for: Any
) -> None:
    transport = scripted_jcode({"database": [json.dumps({"findings": []})]})
    result = run(monkeypatch, (VM, DB), agents_for(transport))
    [incident] = artifacts(result, ArtifactKind.INCIDENT)
    assert incident["analysis_outcome"] == "insufficient_evidence"
    assert transport.prompts["main_rca"] == []
