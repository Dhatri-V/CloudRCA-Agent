"""A2 VM-agent behavior using the shared Qwen runtime fake transport."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cloudrca_backend.contracts import Layer, NormalizedEvent, Provenance
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
from cloudrca_backend.vm_agent import VMAgent, VMAgentInputError

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
        return JCodeResponse(text=self._actions.pop(0), session_id="a2-session", model="qwen-test", usage=TokenUsage())


class FakeTransport:
    def __init__(self, actions: list[str]) -> None:
        self.actions = actions
        self.prompts: list[str] = []

    @contextmanager
    def session(self, config: SpecialistRuntimeConfig, correlation_id: str) -> Iterator[JCodeSession]:
        del config, correlation_id
        yield FakeSession(self.actions, self.prompts)


def runtime(response: str | list[str], *, repairs: int = 1) -> tuple[VMAgent, FakeTransport]:
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
        prompt_version="vm-v1",
    )
    return VMAgent(SpecialistRuntime(config, transport)), transport


def routed(
    event_id: str,
    message: str,
    *,
    vm_id: str | None = "vm-worker-03",
    service: str = "node",
    layer: Layer = Layer.VM,
    source: str = "vm.test",
    timestamp: datetime = UTC_TIME,
) -> RoutedEvent:
    event = NormalizedEvent(
        event_id=event_id,
        timestamp=timestamp,
        source=source,
        layer=layer,
        severity="error",
        service=service,
        vm_id=vm_id,
        message=message,
        provenance=Provenance(kind="observed", source="test", record_id=event_id),
    )
    return route_and_resolve(event, ROUTING_CONFIG)


def analysis(
    event_ids: tuple[str, ...], error_type: str = "cpu_pressure", *, vm_id: str | None = "vm-worker-03", confidence: float = 0.8
) -> str:
    finding: dict[str, object] = {
        "error_type": error_type,
        "severity": "error",
        "time_range": {"start": "2025-04-29T06:03:08Z", "end": "2025-04-29T06:03:08Z"},
        "symptoms": [
            {"category": error_type, "statement": "The cited event reports a VM symptom.", "evidence_event_ids": event_ids}
        ],
        "observations": [
            {"category": error_type, "statement": "The cited event reports a VM error.", "evidence_event_ids": event_ids}
        ],
        "hypotheses": [
            {
                "type": "hypothesis",
                "category": error_type,
                "statement": "This may reflect the reported VM failure.",
                "evidence_event_ids": event_ids,
                "confidence": {"score": confidence, "level": "medium"},
            }
        ],
        "confidence": {"score": confidence, "level": "medium"},
    }
    if vm_id is not None:
        finding["component"] = {"component_id": vm_id, "kind": "vm", "layer": "vm_guest_os"}
    return json.dumps({"findings": [finding]})


@pytest.mark.parametrize(
    ("error_type", "message"),
    [
        ("cpu_pressure", "cpu pressure detected"),
        ("memory_pressure", "memory pressure detected"),
        ("swap_pressure", "swap usage exceeded threshold"),
        ("disk_failure", "disk I/O error"),
        ("filesystem_failure", "filesystem mounted read-only"),
        ("network_failure", "network interface unavailable"),
        ("process_failure", "worker process exited"),
        ("kernel_failure", "kernel panic detected"),
        ("service_failure", "kubelet service failed"),
    ],
)
def test_golden_vm_incident_cases(error_type: str, message: str) -> None:
    event = routed("event-1", message)
    agent, transport = runtime(analysis((event.event.event_id,), error_type))
    result = agent.analyze((event,))
    finding = result.output.findings[0]
    assert finding.error_type.value == error_type
    assert finding.component is not None and finding.component.component_id == "vm-worker-03"
    assert finding.symptoms[0].evidence_event_ids == ("event-1",)
    assert finding.observations[0].evidence_event_ids == ("event-1",)
    assert finding.hypotheses[0].type.value == "hypothesis"
    assert "Supported incident types" in transport.prompts[0]


def test_healthy_vm_input_returns_explicit_empty_finding_set() -> None:
    event = routed("event-1", "node completed health check")
    agent, _ = runtime('{"findings": []}')
    assert agent.analyze((event,)).output.findings == ()


def test_missing_vm_identifier_is_preserved_as_no_component() -> None:
    event = routed("event-1", "cpu pressure detected", vm_id=None)
    agent, _ = runtime(analysis(("event-1",), vm_id=None))
    assert agent.analyze((event,)).output.findings[0].component is None


def test_contradictory_evidence_is_retained_as_an_observation() -> None:
    first = routed("event-pressure", "memory pressure detected")
    second = routed("event-clear", "memory pressure cleared")
    response = json.loads(analysis(("event-pressure", "event-clear"), "memory_pressure", confidence=0.3))
    response["findings"][0]["observations"] = [
        {
            "statement": "event-pressure reports pressure while event-clear reports it cleared.",
            "category": "memory_pressure",
            "evidence_event_ids": ["event-pressure", "event-clear"],
        }
    ]
    response["findings"][0]["hypotheses"][0].update(
        {
            "statement": "The pressure may have been transient.",
            "category": "memory_pressure",
            "evidence_event_ids": ["event-pressure", "event-clear"],
            "confidence": {"score": 0.3, "level": "medium"},
        }
    )
    response["findings"][0]["evidence_state"] = "contradictory"
    response["findings"][0]["contradiction_event_ids"] = ["event-pressure", "event-clear"]
    agent, _ = runtime(json.dumps(response))
    finding = agent.analyze((first, second)).output.findings[0]
    assert finding.confidence.score == 0.3
    assert "cleared" in finding.observations[0].statement


def test_malformed_model_response_fails_safely() -> None:
    agent, _ = runtime("not-json", repairs=0)
    with pytest.raises(SpecialistRuntimeError) as raised:
        agent.analyze((routed("event-1", "cpu pressure detected"),))
    assert raised.value.code is SpecialistFailureCode.INVALID_OUTPUT


def test_malformed_first_response_is_repaired() -> None:
    event = routed("event-1", "cpu pressure detected")
    agent, transport = runtime(["not-json", analysis(("event-1",))])
    assert agent.analyze((event,)).metadata.repair_count == 1
    assert len(transport.prompts) == 2


@pytest.mark.parametrize(
    "event",
    [
        routed("event-database", "database error", vm_id=None, service="redis", layer=Layer.DATABASE),
        routed("event-host", "host error", vm_id=None, service="libvirt", layer=Layer.HYPERVISOR, source="libvirt"),
    ],
)
def test_non_vm_routed_event_is_rejected_before_qwen_execution(event: RoutedEvent) -> None:
    agent, transport = runtime('{"findings": []}')
    with pytest.raises(VMAgentInputError, match="VM-routed"):
        agent.analyze((event,))
    assert transport.prompts == []


def test_ambiguous_routed_event_is_rejected_before_qwen_execution() -> None:
    agent, transport = runtime('{"findings": []}')
    with pytest.raises(VMAgentInputError, match="VM-routed"):
        agent.analyze((routed("event-ambiguous", "component failure", vm_id=None, source="libvirt"),))
    assert transport.prompts == []


@pytest.mark.parametrize("claim", ["symptoms", "observations", "hypotheses"])
def test_invalid_claim_evidence_fails_safely(claim: str) -> None:
    response = json.loads(analysis(("event-1",)))
    response["findings"][0][claim][0]["evidence_event_ids"] = ["outside-input"]
    agent, _ = runtime(json.dumps(response), repairs=0)
    with pytest.raises(SpecialistRuntimeError) as raised:
        agent.analyze((routed("event-1", "cpu pressure detected"),))
    assert raised.value.code is SpecialistFailureCode.INVALID_OUTPUT


def test_fabricated_vm_identifier_fails_safely() -> None:
    response = json.loads(analysis(("event-1",)))
    response["findings"][0]["component"]["component_id"] = "invented-vm"
    agent, _ = runtime(json.dumps(response), repairs=0)
    with pytest.raises(SpecialistRuntimeError) as raised:
        agent.analyze((routed("event-1", "cpu pressure detected"),))
    assert raised.value.code is SpecialistFailureCode.INVALID_OUTPUT


def test_same_vm_evidence_is_allowed() -> None:
    first = routed("event-1", "cpu pressure detected")
    second = routed("event-2", "cpu pressure remains high")
    agent, _ = runtime(analysis(("event-1", "event-2")))
    assert agent.analyze((first, second)).output.findings[0].component is not None


def test_conflicting_vm_evidence_fails_safely() -> None:
    first = routed("event-1", "cpu pressure detected", vm_id="vm-a")
    second = routed("event-2", "cpu pressure detected", vm_id="vm-b")
    response = json.loads(analysis(("event-1", "event-2"), vm_id="vm-a"))
    agent, _ = runtime(json.dumps(response), repairs=0)
    with pytest.raises(SpecialistRuntimeError) as raised:
        agent.analyze((first, second))
    assert raised.value.code is SpecialistFailureCode.INVALID_OUTPUT


def test_mismatched_component_kind_fails_safely() -> None:
    response = json.loads(analysis(("event-1",)))
    response["findings"][0]["component"]["kind"] = "database"
    agent, _ = runtime(json.dumps(response), repairs=0)
    with pytest.raises(SpecialistRuntimeError) as raised:
        agent.analyze((routed("event-1", "cpu pressure detected"),))
    assert raised.value.code is SpecialistFailureCode.INVALID_OUTPUT


@pytest.mark.parametrize("category", ["database_failure", "hypervisor_failure"])
def test_cross_layer_claim_category_fails_safely(category: str) -> None:
    response = json.loads(analysis(("event-1",)))
    response["findings"][0]["symptoms"][0]["category"] = category
    agent, _ = runtime(json.dumps(response), repairs=0)
    with pytest.raises(SpecialistRuntimeError) as raised:
        agent.analyze((routed("event-1", "cpu pressure detected"),))
    assert raised.value.code is SpecialistFailureCode.INVALID_OUTPUT


def test_contradictory_evidence_above_confidence_ceiling_fails_safely() -> None:
    first = routed("event-pressure", "memory pressure detected")
    second = routed("event-clear", "memory pressure cleared")
    response = json.loads(analysis(("event-pressure", "event-clear"), "memory_pressure", confidence=0.9))
    response["findings"][0]["evidence_state"] = "contradictory"
    response["findings"][0]["contradiction_event_ids"] = ["event-pressure", "event-clear"]
    agent, _ = runtime(json.dumps(response), repairs=0)
    with pytest.raises(SpecialistRuntimeError) as raised:
        agent.analyze((first, second))
    assert raised.value.code is SpecialistFailureCode.INVALID_OUTPUT


@pytest.mark.parametrize(
    "change",
    [
        lambda finding: finding.pop("time_range"),
        lambda finding: finding["time_range"].update({"start": "2025-04-28T06:03:08Z"}),
        lambda finding: finding["time_range"].update({"end": "2025-04-29T06:03:07Z"}),
        lambda finding: finding.update({"time_range": {"start": "2025-05-01T06:03:08Z", "end": "2025-05-01T06:03:08Z"}}),
    ],
)
def test_unsupported_time_range_fails_safely(change: object) -> None:
    response = json.loads(analysis(("event-1",)))
    change(response["findings"][0])  # type: ignore[operator]
    agent, _ = runtime(json.dumps(response), repairs=0)
    with pytest.raises(SpecialistRuntimeError) as raised:
        agent.analyze((routed("event-1", "cpu pressure detected"),))
    assert raised.value.code is SpecialistFailureCode.INVALID_OUTPUT


@pytest.mark.parametrize(
    "change",
    [
        lambda finding: finding["confidence"].update({"score": 1.1}),
        lambda finding: finding.pop("confidence"),
        lambda finding: finding["hypotheses"][0]["confidence"].update({"score": -0.1}),
        lambda finding: finding["hypotheses"][0].pop("confidence"),
    ],
)
def test_invalid_or_missing_confidence_fails_safely(change: object) -> None:
    response = json.loads(analysis(("event-1",)))
    change(response["findings"][0])  # type: ignore[operator]
    agent, _ = runtime(json.dumps(response), repairs=0)
    with pytest.raises(SpecialistRuntimeError) as raised:
        agent.analyze((routed("event-1", "cpu pressure detected"),))
    assert raised.value.code is SpecialistFailureCode.INVALID_OUTPUT


def test_unknown_routed_event_is_rejected_before_qwen_execution() -> None:
    event = NormalizedEvent(
        event_id="event-unknown",
        timestamp=UTC_TIME,
        source="custom",
        layer=Layer.VM,
        severity="error",
        message="unclassified event",
        provenance=Provenance(kind="observed", source="test", record_id="event-unknown"),
    )
    agent, transport = runtime('{"findings": []}')
    with pytest.raises(VMAgentInputError, match="VM-routed"):
        agent.analyze((route_and_resolve(event, ROUTING_CONFIG),))
    assert transport.prompts == []


def test_empty_or_duplicate_input_is_rejected_before_qwen_execution() -> None:
    agent, transport = runtime('{"findings": []}')
    with pytest.raises(VMAgentInputError, match="at least one"):
        agent.analyze(())
    event = routed("event-duplicate", "cpu pressure detected")
    with pytest.raises(VMAgentInputError, match="unique"):
        agent.analyze((event, event))
    assert transport.prompts == []


def test_invalid_response_after_repair_fails_safely() -> None:
    agent, _ = runtime(["not-json", "still-not-json"], repairs=1)
    with pytest.raises(SpecialistRuntimeError) as raised:
        agent.analyze((routed("event-1", "cpu pressure detected"),))
    assert raised.value.code is SpecialistFailureCode.INVALID_OUTPUT
