"""The main RCA prompt lists a bounded set of the strongest correlation edges and discloses the rest.

Regression: events on one instance share identifiers, so the correlation graph grows quadratically. An 18-event
Splitwise incident produced 128 edges and an 89k-character prompt (limit 50,000).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cloudrca_backend.correlation import CorrelationConfig, CrossLayerCorrelator
from cloudrca_backend.incident_grouping import GroupingConfig, IncidentGrouper
from cloudrca_backend.main_rca_agent import (
    MAX_PROMPT_EDGES,
    MainRcaRequest,
    build_main_rca_prompt,
    validate_main_rca_report,
)
from cloudrca_backend.normalization import normalize_file
from cloudrca_backend.pipeline import incident_topology
from cloudrca_backend.routing import load_routing_config, route_and_resolve
from cloudrca_backend.specialist_runtime import SpecialistRuntimeConfig

ROOT = Path(__file__).resolve().parents[2]
ROUTING = load_routing_config(ROOT / "config" / "routing" / "aiops2025.yaml")
CONFIG = SpecialistRuntimeConfig(
    jcode_binary="jcode", provider_profile="cloudrca-test", provider_base_url="http://localhost:11434/v1", model="rca",
    timeout_seconds=5, max_events=100, max_prompt_characters=200_000, max_repair_attempts=1, prompt_version="v2",
)


def dense_incident(tmp_path: Path, count: int = 40) -> MainRcaRequest:
    """One instance emitting heartbeats and pool snapshots every 15 s: every pair shares the instance."""
    start = datetime(2026, 10, 31, 10, 0, tzinfo=timezone.utc)
    records = []
    for index in range(count):
        timestamp = (start + timedelta(seconds=15 * index)).isoformat().replace("+00:00", "Z")
        if index % 2:
            records.append({"cloudrca_record": "app.pool_snapshot", "timestamp": timestamp, "source": "test", "event_id": f"pool-{index}",
                            "service": "splitwise-api", "instance_id": "i-dense", "db_pool_size": 5, "db_pool_checked_out": index % 6})
        else:
            records.append({"cloudrca_record": "host.heartbeat", "timestamp": timestamp, "source": "test", "event_id": f"hb-{index}",
                            "instance_id": "i-dense", "load_1m": 0.5 + index / 10, "cpu_count": 2})
    path = tmp_path / "dense.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records))
    routed = tuple(route_and_resolve(event, ROUTING) for event in normalize_file(path).events)
    [incident] = IncidentGrouper(GroupingConfig()).group(routed).incidents
    return MainRcaRequest(
        incident=incident, graph=CrossLayerCorrelator(CorrelationConfig()).correlate(incident, routed),
        topology=incident_topology(routed), events=tuple(item.event for item in routed),
    )


def graph_payload(prompt: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(next(line for line in prompt.splitlines() if line.startswith("INPUT=")).removeprefix("INPUT="))
    return data["correlation_graph"]  # type: ignore[no-any-return]


def test_dense_incidents_list_only_the_strongest_edges_and_disclose_the_rest(tmp_path: Path) -> None:
    request = dense_incident(tmp_path)
    total = len(request.graph.edges)
    assert total > MAX_PROMPT_EDGES
    prompt = build_main_rca_prompt(request, "v2")
    graph = graph_payload(prompt)
    summary = graph["edge_summary"]
    assert (summary["edge_count"], summary["listed"], summary["omitted"]) == (total, MAX_PROMPT_EDGES, total - MAX_PROMPT_EDGES)
    assert sum(summary["by_type"].values()) == total and set(summary["type_meanings"]) == set(summary["by_type"])
    listed_scores = sorted((edge["score"] for edge in graph["edges"]), reverse=True)
    strongest = sorted((round(edge.score, 3) for edge in request.graph.edges), reverse=True)[:MAX_PROMPT_EDGES]
    assert listed_scores == strongest
    assert len(prompt) < 50_000


def test_edges_carry_endpoints_only_while_topology_ids_stay_listed_once(tmp_path: Path) -> None:
    request = dense_incident(tmp_path)
    prompt = build_main_rca_prompt(request, "v2")
    graph = graph_payload(prompt)
    assert all(set(edge) == {"source", "target", "type", "score"} for edge in graph["edges"])
    relationship_ids = {relationship for edge in request.graph.edges for relationship in edge.topology_relationship_ids}
    assert relationship_ids and all(prompt.count(identifier) == 1 for identifier in relationship_ids)


def test_omitted_edges_remain_valid_citations(tmp_path: Path) -> None:
    request = dense_incident(tmp_path)
    listed = {(edge["source"], edge["target"]) for edge in graph_payload(build_main_rca_prompt(request, "v2"))["edges"]}
    omitted = next(edge for edge in request.graph.edges if (edge.source_id.removeprefix("event:"), edge.target_id.removeprefix("event:")) not in listed)
    event_id = omitted.source_id.removeprefix("event:")
    component = request.incident.affected_components[0]
    citation = [{"evidence_id": "c1", "kind": "event", "reference_id": event_id}]
    report = {
        "incident_id": request.incident.incident_id, "probable_root_cause": "test", "root_cause_layer": component.layer.value,
        "root_cause_component": component.model_dump(mode="json", exclude={"schema_version"}),
        "timeline": [{"timestamp": "2026-10-31T10:00:00Z", "summary": "t", "event_ids": [event_id]}],
        "causal_chain": [{"cause_id": component.component_id, "effect_id": event_id, "summary": "s", "confidence": {"score": 0.5, "level": "medium"}, "evidence": citation}],
        "evidence": citation, "confidence": {"score": 0.5, "level": "medium"}, "uncertainty": "test",
        "alternative_hypotheses": [{"summary": "alt", "layer": "application", "confidence": {"score": 0.2, "level": "low"}, "evidence": citation}],
        "runtime": {"provider": "x", "model": "x", "harness": "x"},
    }
    assert validate_main_rca_report(json.dumps(report), request, CONFIG).evidence[0].reference_id == event_id


def test_small_graphs_are_listed_in_full() -> None:
    routed = tuple(route_and_resolve(event, ROUTING) for event in normalize_file(ROOT / "tests/fixtures/synthetic/redis_vm_cpu_incident.jsonl").events)
    [incident] = IncidentGrouper(GroupingConfig()).group(routed).incidents
    request = MainRcaRequest(incident=incident, graph=CrossLayerCorrelator(CorrelationConfig()).correlate(incident, routed), events=tuple(item.event for item in routed))
    graph = graph_payload(build_main_rca_prompt(request, "v2"))
    assert len(graph["edges"]) == len(request.graph.edges) == graph["edge_summary"]["edge_count"]
    assert graph["edge_summary"]["omitted"] == 0
