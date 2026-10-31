"""R4: the main RCA is told how to reason across layers, sees telemetry coverage, and must show alternatives."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cloudrca_backend.correlation import CorrelationConfig, CrossLayerCorrelator
from cloudrca_backend.incident_grouping import GroupingConfig, IncidentGrouper
from cloudrca_backend.main_rca_agent import (
    MainRcaRequest,
    MainRcaValidationError,
    build_main_rca_prompt,
    load_main_rca_runtime_config,
    validate_main_rca_report,
)
from cloudrca_backend.normalization import normalize_file
from cloudrca_backend.pipeline import incident_topology
from cloudrca_backend.routing import AWS_HOST_UNAVAILABLE, load_routing_config, route_and_resolve
from cloudrca_backend.settings import Profile, Settings
from cloudrca_backend.specialist_runtime import SpecialistRuntimeConfig

ROOT = Path(__file__).resolve().parents[2]
ROUTING = load_routing_config(ROOT / "config" / "routing" / "aiops2025.yaml")
CONFIG = SpecialistRuntimeConfig(
    jcode_binary="jcode", provider_profile="cloudrca-test", provider_base_url="http://localhost:11434/v1", model="rca",
    timeout_seconds=5, max_events=100, max_prompt_characters=200_000, max_repair_attempts=1, prompt_version="v2",
)


def request_for(path: Path) -> MainRcaRequest:
    routed = tuple(route_and_resolve(event, ROUTING) for event in normalize_file(path).events)
    [incident] = IncidentGrouper(GroupingConfig()).group(routed).incidents
    graph = CrossLayerCorrelator(CorrelationConfig()).correlate(incident, routed)
    return MainRcaRequest(incident=incident, graph=graph, topology=incident_topology(routed))


SPLITWISE = ROOT / "tests" / "fixtures" / "splitwise" / "cloudrca_records.jsonl"
AIOPS_LOGS = ROOT / "tests" / "fixtures" / "aiops2025" / "logs.parquet"


def payload(prompt: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(next(line for line in prompt.splitlines() if line.startswith("INPUT=")).removeprefix("INPUT="))
    return data


def test_prompt_states_the_cross_layer_reasoning_rules() -> None:
    prompt = build_main_rca_prompt(request_for(SPLITWISE), "v2")
    for phrase in (
        "Separate the component that REPORTED each error",
        "Never attribute a failure to the reporting layer by default",
        "Weigh application, database, VM/OS, and infrastructure explanations",
        "do not prove causation",
        "name the missing telemetry",
        "Never claim evidence for layers listed in telemetry_coverage.unavailable",
        "must cite at least one actual event ID from INPUT.events",
        "alternative_hypotheses and uncertainty are required",
    ):
        assert phrase in prompt


def test_telemetry_coverage_reports_present_absent_and_unobservable_layers() -> None:
    coverage = payload(build_main_rca_prompt(request_for(SPLITWISE), "v2"))["telemetry_coverage"]
    layers = coverage["layers"]
    assert {name for name, item in layers.items() if item["has_evidence"]} == {"application", "database", "vm_guest_os"}
    assert layers["host_hypervisor"] == {"has_evidence": False, "event_count": 0, "specialist": "A3", "specialist_status": "not_run"}
    assert layers["application"]["specialist"] == "none (evidence only)"
    assert (layers["application"]["event_count"], layers["database"]["event_count"], layers["vm_guest_os"]["event_count"]) == (2, 1, 2)
    assert any(AWS_HOST_UNAVAILABLE in item for item in coverage["unavailable"])


def scripted_report(request: MainRcaRequest, *, alternatives: bool, uncertainty: bool) -> str:
    event_id = request.incident.source_events[0].reference_id
    component = next(item for item in request.incident.affected_components if item.layer.value == "vm_guest_os") if len(
        request.incident.affected_layers
    ) > 1 else request.incident.affected_components[0]
    citation = [{"evidence_id": "c1", "kind": "event", "reference_id": event_id}]
    report: dict[str, Any] = {
        "incident_id": request.incident.incident_id,
        "probable_root_cause": "test",
        "root_cause_layer": component.layer.value,
        "root_cause_component": component.model_dump(mode="json", exclude={"schema_version"}),
        "timeline": [{"timestamp": "2026-10-30T10:00:00Z", "summary": "t", "event_ids": [event_id]}],
        "causal_chain": [{"cause_id": component.component_id, "effect_id": event_id, "summary": "s", "confidence": {"score": 0.5, "level": "medium"}, "evidence": citation}],
        "evidence": citation,
        "confidence": {"score": 0.5, "level": "medium"},
        "uncertainty": "Lock-wait timings are not recorded." if uncertainty else None,
        "runtime": {"provider": "x", "model": "x", "harness": "x"},
    }
    if alternatives:
        report["alternative_hypotheses"] = [
            {"summary": "Concurrent writes", "layer": "application", "confidence": {"score": 0.3, "level": "low"}, "evidence": citation}
        ]
    return json.dumps(report)


@pytest.mark.parametrize(
    ("alternatives", "uncertainty", "issue_types"),
    [
        (False, True, ["missing_alternative_hypotheses"]),
        (True, False, ["missing_uncertainty"]),
        (False, False, ["missing_alternative_hypotheses", "missing_uncertainty"]),
    ],
)
def test_cross_layer_reports_must_show_alternatives_and_uncertainty(alternatives: bool, uncertainty: bool, issue_types: list[str]) -> None:
    request = request_for(SPLITWISE)
    with pytest.raises(MainRcaValidationError) as error:
        validate_main_rca_report(scripted_report(request, alternatives=alternatives, uncertainty=uncertainty), request, CONFIG)
    assert [issue["type"] for issue in error.value.issues] == issue_types


def test_cross_layer_report_with_alternatives_and_uncertainty_is_accepted() -> None:
    request = request_for(SPLITWISE)
    report = validate_main_rca_report(scripted_report(request, alternatives=True, uncertainty=True), request, CONFIG)
    assert report.root_cause_layer.value == "vm_guest_os" and report.alternative_hypotheses[0].layer.value == "application"


def test_report_and_each_causal_edge_need_event_evidence_not_only_topology() -> None:
    request = request_for(SPLITWISE)
    report = json.loads(scripted_report(request, alternatives=True, uncertainty=True))
    topology_id = request.topology.relationships[0].relationship_id
    topology = [{"evidence_id": "topology-only", "kind": "topology", "reference_id": topology_id}]
    report["evidence"] = topology
    report["causal_chain"][0]["evidence"] = topology
    with pytest.raises(MainRcaValidationError) as error:
        validate_main_rca_report(json.dumps(report), request, CONFIG)
    assert error.value.issues == (
        {"type": "missing_event_evidence", "loc": "evidence"},
        {"type": "missing_event_evidence", "loc": "causal_chain.0.evidence"},
    )


def test_metric_event_references_satisfy_direct_event_evidence_requirements() -> None:
    request = request_for(SPLITWISE)
    report = json.loads(scripted_report(request, alternatives=True, uncertainty=True))
    event_id = request.incident.source_events[0].reference_id
    metric = [{"evidence_id": "metric-event", "kind": "metric", "reference_id": event_id}]
    report["evidence"] = metric
    report["causal_chain"][0]["evidence"] = metric
    assert validate_main_rca_report(json.dumps(report), request, CONFIG).evidence[0].kind.value == "metric"


def test_single_layer_incidents_keep_the_previous_contract() -> None:
    request = request_for(AIOPS_LOGS)
    assert len(request.incident.affected_layers) == 1
    report = validate_main_rca_report(scripted_report(request, alternatives=False, uncertainty=False), request, CONFIG)
    assert report.alternative_hypotheses == ()


def test_prompt_version_label_is_bumped() -> None:
    settings = Settings(profile=Profile.TEST, data_dir=Path("."), knowledge_dir=Path("."), jcode_binary="jcode")
    environ = {"CLOUDRCA_GLM_PROVIDER_PROFILE": "cloudrca-test", "CLOUDRCA_GLM_BASE_URL": "http://localhost:1/v1", "CLOUDRCA_GLM_MODEL": "m"}
    assert load_main_rca_runtime_config(settings, environ).prompt_version == "main-rca-v2"
