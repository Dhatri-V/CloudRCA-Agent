"""A1 database-agent behavior using the shared Qwen runtime fake transport."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cloudrca_backend.contracts import Layer, NormalizedEvent, Provenance
from cloudrca_backend.database_agent import DatabaseAgent, DatabaseAgentInputError
from cloudrca_backend.routing import RoutedEvent, load_routing_config, route_and_resolve
from cloudrca_backend.specialist_runtime import (
    JCodeResponse,
    JCodeSession,
    SpecialistFailureCode,
    SpecialistRuntime,
    SpecialistRuntimeConfig,
    SpecialistRuntimeError,
    TokenUsage,
)

ROOT = Path(__file__).parents[2]
ROUTING_CONFIG = load_routing_config(ROOT / "config" / "routing" / "aiops2025.yaml")
UTC_TIME = datetime(2025, 4, 29, 6, 3, 8, tzinfo=timezone.utc)


class FakeSession:
    def __init__(self, actions: list[str], prompts: list[str]) -> None:
        self._actions = actions
        self._prompts = prompts

    def complete(self, prompt: str, timeout_seconds: float) -> JCodeResponse:
        del timeout_seconds
        self._prompts.append(prompt)
        return JCodeResponse(text=self._actions.pop(0), session_id="a1-session", model="qwen-test", usage=TokenUsage())


class FakeTransport:
    def __init__(self, actions: list[str]) -> None:
        self.actions = actions
        self.prompts: list[str] = []

    @contextmanager
    def session(self, config: SpecialistRuntimeConfig, correlation_id: str) -> Iterator[JCodeSession]:
        del config, correlation_id
        yield FakeSession(self.actions, self.prompts)


def runtime(response: str | list[str], *, repairs: int = 1) -> tuple[DatabaseAgent, FakeTransport]:
    transport = FakeTransport([response] if isinstance(response, str) else response)
    config = SpecialistRuntimeConfig(
        jcode_binary="jcode",
        provider_profile="qwen-test",
        provider_base_url="http://localhost:11434/v1",
        model="qwen-test",
        timeout_seconds=10,
        max_events=10,
        max_prompt_characters=100_000,
        max_repair_attempts=repairs,
        prompt_version="database-v1",
    )
    return DatabaseAgent(SpecialistRuntime(config, transport)), transport


def routed(
    event_id: str, message: str, *, database: bool = True, timestamp: datetime = UTC_TIME
) -> RoutedEvent:
    event = NormalizedEvent(
        event_id=event_id,
        timestamp=timestamp,
        source="database.test" if database else "node.test",
        layer=Layer.DATABASE if database else Layer.VM,
        severity="error",
        service="redis" if database else "node",
        database_id="redis-1" if database else None,
        vm_id="vm-1" if not database else None,
        message=message,
        provenance=Provenance(kind="observed", source="test", record_id=event_id),
    )
    return route_and_resolve(event, ROUTING_CONFIG)


def analysis(event_ids: tuple[str, ...], error_type: str = "connection_failure", *, confidence: float = 0.8) -> str:
    return json.dumps(
        {
            "findings": [
                {
                    "error_type": error_type,
                    "severity": "error",
                    "component": {"component_id": "redis-1", "kind": "database", "layer": "database"},
                    "time_range": {"start": "2025-04-29T06:03:08Z", "end": "2025-04-29T06:03:08Z"},
                    "symptoms": ["Database request failed"],
                    "observations": [
                        {"statement": "The cited event records a database error.", "evidence_event_ids": event_ids}
                    ],
                    "hypotheses": [
                        {
                            "type": "hypothesis",
                            "statement": "This may reflect the reported database failure.",
                            "evidence_event_ids": event_ids,
                            "confidence": {"score": confidence, "level": "medium"},
                        }
                    ],
                    "confidence": {"score": confidence, "level": "medium"},
                }
            ]
        }
    )


@pytest.mark.parametrize(
    ("error_type", "message"),
    [
        ("connection_failure", "database connection refused"),
        ("deadlock", "transaction deadlock detected"),
        ("query_failure_or_performance", "slow query exceeded timeout"),
        ("replication_failure", "replication lag exceeds threshold"),
        ("storage_failure", "database disk full"),
        ("availability_failure", "database unavailable"),
    ],
)
def test_golden_database_incident_cases(error_type: str, message: str) -> None:
    event = routed("event-1", message)
    agent, transport = runtime(analysis((event.event.event_id,), error_type))
    result = agent.analyze((event,))
    assert result.output.findings[0].error_type.value == error_type
    assert result.output.findings[0].observations[0].evidence_event_ids == ("event-1",)
    assert result.output.findings[0].hypotheses[0].type.value == "hypothesis"
    assert "Supported incident types" in transport.prompts[0]
    assert "Observations must be directly supported" in transport.prompts[0]


def test_no_anomaly_returns_explicit_empty_finding_set() -> None:
    event = routed("event-1", "redis completed health check")
    agent, _ = runtime('{"findings": []}')
    assert agent.analyze((event,)).output.findings == ()


def test_contradictory_evidence_is_retained_as_observation_not_a_confirmed_cause() -> None:
    first = routed("event-failed", "redis connection failed")
    second = routed("event-ok", "redis connection restored")
    response = json.loads(analysis(("event-failed", "event-ok"), confidence=0.3))
    response["findings"][0]["observations"] = [
        {
            "statement": "event-failed reports a connection failure while event-ok reports it restored.",
            "evidence_event_ids": ["event-failed", "event-ok"],
        }
    ]
    response["findings"][0]["hypotheses"] = [
        {
            "type": "hypothesis",
            "statement": "The failure may have been transient.",
            "evidence_event_ids": ["event-failed", "event-ok"],
            "confidence": {"score": 0.3, "level": "medium"},
        }
    ]
    agent, _ = runtime(json.dumps(response))
    finding = agent.analyze((first, second)).output.findings[0]
    assert finding.confidence.score == 0.3
    assert "restored" in finding.observations[0].statement


def test_malformed_model_response_fails_without_a_finding() -> None:
    event = routed("event-1", "redis connection failed")
    agent, _ = runtime("not-json", repairs=0)
    with pytest.raises(SpecialistRuntimeError) as raised:
        agent.analyze((event,))
    assert raised.value.code is SpecialistFailureCode.INVALID_OUTPUT


def test_non_database_routed_event_is_rejected_before_qwen_execution() -> None:
    agent, transport = runtime('{"findings": []}')
    with pytest.raises(DatabaseAgentInputError, match="database-routed"):
        agent.analyze((routed("event-vm", "cpu pressure", database=False),))
    assert transport.prompts == []


def test_unsupported_event_evidence_fails_safely() -> None:
    event = routed("event-1", "redis connection failed")
    response = json.loads(analysis(("event-1",)))
    response["findings"][0]["observations"][0]["evidence_event_ids"] = ["outside-input"]
    agent, _ = runtime(json.dumps(response), repairs=0)
    with pytest.raises(SpecialistRuntimeError) as raised:
        agent.analyze((event,))
    assert raised.value.code is SpecialistFailureCode.INVALID_OUTPUT


def test_hypothesis_with_unsupported_event_evidence_fails_safely() -> None:
    event = routed("event-1", "redis connection failed")
    response = json.loads(analysis(("event-1",)))
    response["findings"][0]["hypotheses"][0]["evidence_event_ids"] = ["outside-input"]
    agent, _ = runtime(json.dumps(response), repairs=0)
    with pytest.raises(SpecialistRuntimeError) as raised:
        agent.analyze((event,))
    assert raised.value.code is SpecialistFailureCode.INVALID_OUTPUT


def test_invented_database_component_fails_safely() -> None:
    event = routed("event-1", "redis connection failed")
    response = json.loads(analysis(("event-1",)))
    response["findings"][0]["component"]["component_id"] = "invented-db"
    agent, _ = runtime(json.dumps(response), repairs=0)
    with pytest.raises(SpecialistRuntimeError) as raised:
        agent.analyze((event,))
    assert raised.value.code is SpecialistFailureCode.INVALID_OUTPUT


def test_overly_broad_time_range_fails_safely() -> None:
    event = routed("event-1", "redis connection failed")
    response = json.loads(analysis(("event-1",)))
    response["findings"][0]["time_range"]["start"] = "2025-04-28T06:03:08Z"
    agent, _ = runtime(json.dumps(response), repairs=0)
    with pytest.raises(SpecialistRuntimeError) as raised:
        agent.analyze((event,))
    assert raised.value.code is SpecialistFailureCode.INVALID_OUTPUT


def test_malformed_first_response_is_repaired() -> None:
    event = routed("event-1", "redis connection failed")
    agent, transport = runtime(["not-json", analysis(("event-1",))])
    result = agent.analyze((event,))
    assert result.metadata.repair_count == 1
    assert len(transport.prompts) == 2


def test_ambiguous_routed_event_is_rejected_before_qwen_execution() -> None:
    event = NormalizedEvent(
        event_id="event-ambiguous",
        timestamp=UTC_TIME,
        source="libvirt",
        layer=Layer.DATABASE,
        severity="error",
        service="redis",
        message="component failure",
        provenance=Provenance(kind="observed", source="test", record_id="event-ambiguous"),
    )
    agent, transport = runtime('{"findings": []}')
    with pytest.raises(DatabaseAgentInputError, match="database-routed"):
        agent.analyze((route_and_resolve(event, ROUTING_CONFIG),))
    assert transport.prompts == []
