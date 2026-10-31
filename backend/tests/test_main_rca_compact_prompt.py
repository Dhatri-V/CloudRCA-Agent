"""The main RCA prompt states each fact once: compact events, ID-only edges, and per-reason grouping summaries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cloudrca_backend.correlation import CorrelationConfig, CrossLayerCorrelator
from cloudrca_backend.incident_grouping import GroupingConfig, IncidentGrouper
from cloudrca_backend.main_rca_agent import MainRcaRequest, build_main_rca_prompt
from cloudrca_backend.normalization import normalize_file
from cloudrca_backend.pipeline import incident_topology
from cloudrca_backend.routing import load_routing_config, route_and_resolve

ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC = ROOT / "tests" / "fixtures" / "synthetic" / "redis_vm_cpu_incident.jsonl"
ROUTING = load_routing_config(ROOT / "config" / "routing" / "aiops2025.yaml")


def request(with_events: bool) -> MainRcaRequest:
    routed = tuple(route_and_resolve(event, ROUTING) for event in normalize_file(SYNTHETIC).events)
    [incident] = IncidentGrouper(GroupingConfig()).group(routed).incidents
    return MainRcaRequest(
        incident=incident, graph=CrossLayerCorrelator(CorrelationConfig()).correlate(incident, routed),
        topology=incident_topology(routed), events=tuple(item.event for item in routed) if with_events else (),
    )


def payload(prompt: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(next(line for line in prompt.splitlines() if line.startswith("INPUT=")).removeprefix("INPUT="))
    return data


def test_events_appear_once_with_their_facts_and_no_raw_records() -> None:
    rca_request = request(with_events=True)
    prompt = build_main_rca_prompt(rca_request, "v2")
    data = payload(prompt)
    events = {item["event_id"]: item for item in data["events"]}
    assert set(events) == {item.event_id for item in rca_request.events}
    for event in rca_request.events:
        compact = events[event.event_id]
        assert compact["message"] == event.message and compact["layer"] == event.layer.value
        assert compact["provenance"]["record_id"] == event.provenance.record_id
    assert "raw_record" not in prompt
    assert all(set(item) == {"evidence_id", "kind", "reference_id"} for item in data["incident"]["source_events"])


def test_edges_reference_events_by_id_and_rationales_are_summarized() -> None:
    rca_request = request(with_events=True)
    data = payload(build_main_rca_prompt(rca_request, "v2"))
    edges = data["correlation_graph"]["edges"]
    assert len(edges) == len(rca_request.graph.edges) > 20
    event_ids = {item.event_id for item in rca_request.events}
    assert all(edge["source"] in event_ids and edge["target"] in event_ids and "evidence_reference_ids" not in edge for edge in edges)
    rationales = data["incident"]["rationales"]
    assert sum(item["event_pairs"] for item in rationales) == len(rca_request.incident.rationales)
    assert {item["reason"] for item in rationales} == {item.reason.value for item in rca_request.incident.rationales}


def test_every_citeable_identifier_survives_compaction() -> None:
    rca_request = request(with_events=True)
    prompt = build_main_rca_prompt(rca_request, "v2")
    citeable = {item.reference_id for item in rca_request.incident.source_events}
    citeable |= {item.relationship_id for item in rca_request.topology.relationships}
    citeable |= {relationship for edge in rca_request.graph.edges for relationship in edge.topology_relationship_ids}
    assert all(identifier in prompt for identifier in citeable)


def test_correlated_incident_prompt_keeps_headroom_below_the_default_limit() -> None:
    # Regression guard: this incident's prompt was 45,059 characters before compaction (limit 50,000).
    assert len(build_main_rca_prompt(request(with_events=True), "v2")) < 35_000


def test_without_events_the_source_event_provenance_is_kept() -> None:
    data = payload(build_main_rca_prompt(request(with_events=False), "v2"))
    assert data["events"] == []
    assert all("provenance" in item for item in data["incident"]["source_events"])
