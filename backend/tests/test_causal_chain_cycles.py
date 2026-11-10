"""Main RCA validation rejects cyclic causal chains (regression from a live concurrency-scenario report)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from cloudrca_backend.correlation import CorrelationConfig, CrossLayerCorrelator
from cloudrca_backend.incident_grouping import GroupingConfig, IncidentGrouper
from cloudrca_backend.main_rca_agent import (
    MainRcaAgent,
    MainRcaRequest,
    MainRcaValidationError,
    validate_main_rca_report,
)
from cloudrca_backend.normalization import normalize_file
from cloudrca_backend.pipeline import incident_topology
from cloudrca_backend.routing import load_routing_config, route_and_resolve
from cloudrca_backend.specialist_runtime import JCodeResponse, JCodeSession, SpecialistRuntimeConfig

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "synthetic" / "splitwise_concurrency_sqlite_lock.jsonl"
CONFIG = SpecialistRuntimeConfig(
    jcode_binary="jcode", provider_profile="cloudrca-test", provider_base_url="http://localhost:11434/v1", model="rca",
    timeout_seconds=5, max_events=100, max_prompt_characters=200_000, max_repair_attempts=1, prompt_version="v2",
)


def request() -> MainRcaRequest:
    routed = tuple(route_and_resolve(event, load_routing_config(ROOT / "config/routing/aiops2025.yaml")) for event in normalize_file(FIXTURE).events)
    [incident] = IncidentGrouper(GroupingConfig()).group(routed).incidents
    return MainRcaRequest(
        incident=incident, graph=CrossLayerCorrelator(CorrelationConfig()).correlate(incident, routed),
        topology=incident_topology(routed), events=tuple(item.event for item in routed),
    )


def event_id(rca_request: MainRcaRequest, record_id: str) -> str:
    return next(event.event_id for event in rca_request.events if event.provenance.record_id == record_id)


def report(rca_request: MainRcaRequest, edges: list[tuple[str, str]]) -> str:
    """A cross-layer report whose causal chain is ``edges`` (record IDs); everything else is valid."""
    ids = {record: event_id(rca_request, record) for edge in edges for record in edge}
    service = next(item for item in rca_request.incident.affected_components if item.component_id == "splitwise-api")
    first = ids[edges[0][0]]
    citation = [{"evidence_id": "c-first", "kind": "event", "reference_id": first}]
    return json.dumps({
        "incident_id": rca_request.incident.incident_id,
        "probable_root_cause": "test",
        "root_cause_layer": "application",
        "root_cause_component": service.model_dump(mode="json", exclude={"schema_version"}),
        "timeline": [{"timestamp": "2026-10-31T11:05:00Z", "summary": "t", "event_ids": [first]}],
        "causal_chain": [
            {
                "cause_id": ids[cause], "effect_id": ids[effect], "summary": f"{cause} -> {effect}",
                "confidence": {"score": 0.6, "level": "medium"},
                "evidence": [{"evidence_id": f"c-{index}", "kind": "event", "reference_id": ids[cause]}],
            }
            for index, (cause, effect) in enumerate(edges)
        ],
        "evidence": citation,
        "alternative_hypotheses": [{"summary": "VM pressure", "layer": "vm_guest_os", "confidence": {"score": 0.2, "level": "low"}, "evidence": citation}],
        "confidence": {"score": 0.6, "level": "medium"},
        "uncertainty": "test",
        "runtime": {"provider": "x", "model": "x", "harness": "x"},
    })


REQUESTS, POOL, ERROR, CPU = "i-synthetic-concurrency-requests-11:05", "i-synthetic-concurrency-pool-11:05", "i-synthetic-concurrency-db-error-1", "i-synthetic-concurrency-cpu-11:05"


def issues(rca_request: MainRcaRequest, edges: list[tuple[str, str]]) -> list[dict[str, str]]:
    with pytest.raises(MainRcaValidationError) as error:
        validate_main_rca_report(report(rca_request, edges), rca_request, CONFIG)
    return list(error.value.issues)


def test_live_observed_two_edge_cycle_is_rejected_and_only_cycle_edges_are_flagged() -> None:
    rca_request = request()
    # The live concurrency report contained requests -> pool and pool -> requests, then pool -> error.
    found = issues(rca_request, [(REQUESTS, POOL), (POOL, REQUESTS), (POOL, ERROR)])
    assert found == [{"type": "cyclic_causal_chain", "loc": "causal_chain.0"}, {"type": "cyclic_causal_chain", "loc": "causal_chain.1"}]


def test_longer_cycles_are_rejected() -> None:
    rca_request = request()
    found = issues(rca_request, [(CPU, REQUESTS), (REQUESTS, POOL), (POOL, ERROR), (ERROR, REQUESTS)])
    assert [item["loc"] for item in found] == ["causal_chain.1", "causal_chain.2", "causal_chain.3"]  # the CPU edge feeds in but is not on the cycle


@pytest.mark.parametrize(
    "edges",
    [
        [(REQUESTS, POOL), (POOL, ERROR)],  # linear
        [(REQUESTS, POOL), (REQUESTS, ERROR), (POOL, ERROR)],  # converging paths (a DAG), not a cycle
        [(CPU, REQUESTS), (CPU, POOL), (REQUESTS, ERROR), (POOL, ERROR)],  # diamond
    ],
)
def test_acyclic_chains_are_accepted(edges: list[tuple[str, str]]) -> None:
    rca_request = request()
    assert len(validate_main_rca_report(report(rca_request, edges), rca_request, CONFIG).causal_chain) == len(edges)


def test_self_edges_remain_rejected_by_the_contract() -> None:
    rca_request = request()
    with pytest.raises(MainRcaValidationError):
        validate_main_rca_report(report(rca_request, [(POOL, POOL)]), rca_request, CONFIG)


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **values: Any) -> None:
        self.events.append((event, values))


class Transport:
    def __init__(self, replies: list[str]) -> None:
        self.replies, self.prompts = replies, []

    @contextmanager
    def session(self, runtime_config: SpecialistRuntimeConfig, correlation_id: str) -> Iterator[JCodeSession]:
        del runtime_config, correlation_id
        transport = self

        class Session:
            def complete(self, prompt: str, timeout_seconds: float) -> JCodeResponse:
                transport.prompts.append(prompt)
                return JCodeResponse(text=transport.replies.pop(0), session_id="s", model="rca")

        yield Session()


def test_cycle_is_logged_without_content_and_repaired_by_the_existing_round() -> None:
    rca_request = request()
    logger = Recorder()
    transport = Transport([report(rca_request, [(REQUESTS, POOL), (POOL, REQUESTS)]), report(rca_request, [(REQUESTS, POOL), (POOL, ERROR)])])
    result = MainRcaAgent(CONFIG, transport, logger=logger).analyze(rca_request)
    assert result.metadata.repair_count == 1
    [failure] = [values for name, values in logger.events if name == "main_rca_validation_failed"]
    assert {item["type"] for item in failure["issues"]} == {"cyclic_causal_chain"}
    assert "acyclic" in transport.prompts[1]  # the repair prompt carries the validation error
    assert rca_request.events[0].event_id not in json.dumps(logger.events)


def test_prompt_states_the_acyclic_rule() -> None:
    from cloudrca_backend.main_rca_agent import build_main_rca_prompt

    assert "The causal_chain must be acyclic" in build_main_rca_prompt(request(), "v2")
