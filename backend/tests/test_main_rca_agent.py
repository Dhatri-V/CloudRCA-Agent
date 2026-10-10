"""Issue #16 main RCA agent tests using the existing fake JCode boundary."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

import pytest
from cloudrca_backend.contracts import ComponentRef, EvidenceReference, Layer, TimeWindow
from cloudrca_backend.correlation import CorrelationGraph
from cloudrca_backend.incident_grouping import CandidateIncident, GroupingRationale, GroupingReasonType
from cloudrca_backend.main_rca_agent import MainRcaAgent, MainRcaError, MainRcaRequest
from cloudrca_backend.specialist_runtime import (
    JCodeResponse,
    JCodeSession,
    SpecialistFailureCode,
    SpecialistRuntimeConfig,
    TokenUsage,
)


def config() -> SpecialistRuntimeConfig:
    return SpecialistRuntimeConfig(jcode_binary="jcode", provider_profile="zai", provider_base_url="http://localhost:11434/v1", model="glm-5.3", timeout_seconds=10, max_events=10, max_prompt_characters=100_000, max_repair_attempts=1, prompt_version="main-v1")


def request() -> MainRcaRequest:
    event = EvidenceReference(evidence_id="evidence-1", kind="event", reference_id="event-1")
    component = ComponentRef(component_id="host-1", kind="host", layer="host_hypervisor")
    incident = CandidateIncident(incident_id="incident-1", time_window=TimeWindow(start=datetime(2025, 1, 1, tzinfo=timezone.utc), end=datetime(2025, 1, 1, tzinfo=timezone.utc)), source_events=(event,), affected_components=(component,), affected_layers={Layer.HYPERVISOR}, rationales=(GroupingRationale(reason=GroupingReasonType.SINGLETON, event_ids=("event-1",)),))
    return MainRcaRequest(incident=incident, graph=CorrelationGraph(incident_id="incident-1", nodes=()))


def report(reference: str = "event-1", uncertainty: str | None = None) -> str:
    return json.dumps({"incident_id": "incident-1", "probable_root_cause": "Host storage contention", "root_cause_layer": "host_hypervisor", "root_cause_component": {"component_id": "host-1", "kind": "host", "layer": "host_hypervisor"}, "timeline": [{"timestamp": "2025-01-01T00:00:00Z", "summary": "Host queue rose", "event_ids": ["event-1"]}], "causal_chain": [{"cause_id": "host-1", "effect_id": "event-1", "summary": "Host delay affected the event", "confidence": {"score": 0.8, "level": "high"}, "evidence": [{"evidence_id": "cite-1", "kind": "event", "reference_id": reference}]}], "evidence": [{"evidence_id": "cite-1", "kind": "event", "reference_id": reference}], "confidence": {"score": 0.8, "level": "high"}, "uncertainty": uncertainty, "runtime": {"provider": "bad", "model": "bad", "harness": "bad"}})


class Session:
    def __init__(self, actions: list[str | Exception]) -> None: self.actions = actions
    def complete(self, prompt: str, timeout_seconds: float) -> JCodeResponse:
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        return JCodeResponse(text=action, session_id="session-1", model="glm-5.3", usage=TokenUsage(input_tokens=2, output_tokens=3))


class Transport:
    def __init__(self, actions: list[str | Exception]) -> None: self.actions = actions
    @contextmanager
    def session(self, runtime_config: SpecialistRuntimeConfig, correlation_id: str) -> Iterator[JCodeSession]:
        del runtime_config, correlation_id
        yield Session(self.actions)


def test_golden_report_is_valid_and_runtime_is_configured() -> None:
    result = MainRcaAgent(config(), Transport([report()])).analyze(request())
    assert result.report.runtime.model == "glm-5.3"
    assert result.metadata.usage == TokenUsage(input_tokens=2, output_tokens=3)


def test_invalid_citation_repairs_then_fails_closed() -> None:
    result = MainRcaAgent(config(), Transport([report("unknown"), report()])).analyze(request())
    assert result.metadata.repair_count == 1
    with pytest.raises(MainRcaError, match="outside"):
        MainRcaAgent(config(), Transport([report("unknown"), report("unknown")])).analyze(request())


def test_timeout_is_typed() -> None:
    from cloudrca_backend.specialist_runtime import JCodeTimeoutError
    with pytest.raises(MainRcaError) as error:
        MainRcaAgent(config(), Transport([JCodeTimeoutError("late")])).analyze(request())
    assert error.value.code is SpecialistFailureCode.TIMEOUT
