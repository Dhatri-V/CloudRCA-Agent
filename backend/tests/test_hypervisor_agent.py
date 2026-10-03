"""A3 hypervisor-agent behavior using the shared Qwen runtime fake transport."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cloudrca_backend.contracts import (
    ComponentRef,
    EvidenceReference,
    Layer,
    NormalizedEvent,
    Provenance,
    RelationshipType,
    TopologyRelationship,
)
from cloudrca_backend.hypervisor_agent import HypervisorAgent, HypervisorAgentInputError
from cloudrca_backend.routing import RoutedEvent, TopologyResolution, load_routing_config, route_and_resolve
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
        return JCodeResponse(text=self._actions.pop(0), session_id="a3-session", model="qwen-test", usage=TokenUsage())


class FakeTransport:
    def __init__(self, actions: list[str]) -> None:
        self.actions = actions
        self.prompts: list[str] = []

    @contextmanager
    def session(self, config: SpecialistRuntimeConfig, correlation_id: str) -> Iterator[JCodeSession]:
        del config, correlation_id
        yield FakeSession(self.actions, self.prompts)


def runtime(response: str | list[str], *, repairs: int = 1) -> tuple[HypervisorAgent, FakeTransport]:
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
        prompt_version="hypervisor-v1",
    )
    return HypervisorAgent(SpecialistRuntime(config, transport)), transport


def routed(
    event_id: str,
    message: str,
    *,
    host_id: str | None = "host-01",
    vm_id: str | None = None,
    layer: Layer = Layer.HYPERVISOR,
    timestamp: datetime = UTC_TIME,
) -> RoutedEvent:
    event = NormalizedEvent(
        event_id=event_id,
        timestamp=timestamp,
        source="libvirt.test" if host_id else "custom.test",
        layer=layer,
        severity="error",
        service="libvirt",
        host_id=host_id,
        vm_id=vm_id,
        message=message,
        provenance=Provenance(kind="observed", source="test", record_id=event_id),
    )
    return route_and_resolve(event, ROUTING_CONFIG)


def analysis(
    event_ids: tuple[str, ...], error_type: str = "resource_contention", *, host_id: str | None = "host-01", confidence: float = 0.8
) -> str:
    finding: dict[str, object] = {
        "error_type": error_type,
        "severity": "error",
        "time_range": {"start": "2025-04-29T06:03:08Z", "end": "2025-04-29T06:03:08Z"},
        "symptoms": [{"category": error_type, "statement": "The cited event reports a host symptom.", "evidence_event_ids": event_ids}],
        "observations": [{"category": error_type, "statement": "The cited event reports a host error.", "evidence_event_ids": event_ids}],
        "hypotheses": [{"type": "hypothesis", "category": error_type, "statement": "This may reflect the reported host failure.", "evidence_event_ids": event_ids, "confidence": {"score": confidence, "level": "medium"}}],
        "confidence": {"score": confidence, "level": "medium"},
    }
    if host_id is not None:
        finding["component"] = {"component_id": host_id, "kind": "host", "layer": "host_hypervisor"}
    return json.dumps({"findings": [finding]})


@pytest.mark.parametrize(
    ("error_type", "message"),
    [
        ("host_cpu_pressure", "host CPU pressure detected"),
        ("host_memory_pressure", "host memory pressure detected"),
        ("storage_failure", "host storage failure"),
        ("io_failure", "host I/O timeout"),
        ("network_failure", "host network unavailable"),
        ("virtualization_failure", "libvirt virtualization failure"),
        ("lifecycle_failure", "VM lifecycle operation failed"),
        ("resource_contention", "host resource contention detected"),
    ],
)
def test_golden_hypervisor_incident_cases(error_type: str, message: str) -> None:
    event = routed("event-1", message)
    agent, transport = runtime(analysis(("event-1",), error_type))
    finding = agent.analyze((event,)).output.findings[0]
    assert finding.error_type.value == error_type
    assert finding.component is not None and finding.component.component_id == "host-01"
    assert finding.observations[0].evidence_event_ids == ("event-1",)
    assert "Do not diagnose guest-OS/VM failures" in transport.prompts[0]


def test_healthy_input_returns_explicit_empty_finding_set() -> None:
    agent, _ = runtime('{"findings": []}')
    assert agent.analyze((routed("event-1", "host health check completed"),)).output.findings == ()


def test_missing_host_identifier_preserves_no_component() -> None:
    event = routed("event-1", "host contention detected", host_id=None)
    agent, _ = runtime(analysis(("event-1",), host_id=None))
    assert agent.analyze((event,)).output.findings[0].component is None


@pytest.mark.parametrize("claim", ["symptoms", "observations", "hypotheses"])
def test_invalid_claim_evidence_fails_safely(claim: str) -> None:
    response = json.loads(analysis(("event-1",)))
    response["findings"][0][claim][0]["evidence_event_ids"] = ["outside-input"]
    agent, _ = runtime(json.dumps(response), repairs=0)
    with pytest.raises(SpecialistRuntimeError, match="invalid") as raised:
        agent.analyze((routed("event-1", "host contention detected"),))
    assert raised.value.code is SpecialistFailureCode.INVALID_OUTPUT


def test_fabricated_or_conflicting_host_identifier_fails_safely() -> None:
    fabricated = json.loads(analysis(("event-1",)))
    fabricated["findings"][0]["component"]["component_id"] = "invented-host"
    agent, _ = runtime(json.dumps(fabricated), repairs=0)
    with pytest.raises(SpecialistRuntimeError):
        agent.analyze((routed("event-1", "host failure"),))
    first = routed("event-1", "host failure", host_id="host-a")
    second = routed("event-2", "host failure", host_id="host-b")
    agent, _ = runtime(analysis(("event-1", "event-2"), host_id="host-a"), repairs=0)
    with pytest.raises(SpecialistRuntimeError):
        agent.analyze((first, second))


@pytest.mark.parametrize(
    "change",
    [
        lambda finding: finding.pop("time_range"),
        lambda finding: finding["time_range"].update({"start": "2025-04-28T06:03:08Z"}),
        lambda finding: finding["time_range"].update({"end": "2025-04-29T06:03:07Z"}),
    ],
)
def test_unsupported_time_range_fails_safely(change: object) -> None:
    response = json.loads(analysis(("event-1",)))
    change(response["findings"][0])  # type: ignore[operator]
    agent, _ = runtime(json.dumps(response), repairs=0)
    with pytest.raises(SpecialistRuntimeError):
        agent.analyze((routed("event-1", "host failure"),))


@pytest.mark.parametrize(
    "change",
    [
        lambda finding: finding["confidence"].update({"score": 1.1}),
        lambda finding: finding.pop("confidence"),
        lambda finding: finding["hypotheses"][0].pop("confidence"),
    ],
)
def test_invalid_or_missing_confidence_fails_safely(change: object) -> None:
    response = json.loads(analysis(("event-1",)))
    change(response["findings"][0])  # type: ignore[operator]
    agent, _ = runtime(json.dumps(response), repairs=0)
    with pytest.raises(SpecialistRuntimeError):
        agent.analyze((routed("event-1", "host failure"),))


def test_contradictory_evidence_above_confidence_ceiling_fails_safely() -> None:
    response = json.loads(analysis(("event-1", "event-2"), "host_memory_pressure", confidence=0.9))
    response["findings"][0].update({"evidence_state": "contradictory", "contradiction_event_ids": ["event-1", "event-2"]})
    agent, _ = runtime(json.dumps(response), repairs=0)
    with pytest.raises(SpecialistRuntimeError):
        agent.analyze((routed("event-1", "pressure"), routed("event-2", "pressure cleared")))


def test_routing_empty_and_duplicate_input_rejected_before_qwen_execution() -> None:
    agent, transport = runtime('{"findings": []}')
    with pytest.raises(HypervisorAgentInputError, match="at least one"):
        agent.analyze(())
    event = routed("event-duplicate", "host failure")
    with pytest.raises(HypervisorAgentInputError, match="unique"):
        agent.analyze((event, event))
    database = NormalizedEvent(event_id="database", timestamp=UTC_TIME, source="redis", layer=Layer.DATABASE, severity="error", database_id="db", message="database failure", provenance=Provenance(kind="observed", source="test", record_id="database"))
    vm = NormalizedEvent(event_id="vm", timestamp=UTC_TIME, source="node", layer=Layer.VM, severity="error", vm_id="vm-1", message="vm failure", provenance=Provenance(kind="observed", source="test", record_id="vm"))
    ambiguous = NormalizedEvent(event_id="ambiguous", timestamp=UTC_TIME, source="libvirt", layer=Layer.HYPERVISOR, severity="error", service="redis", message="failure", provenance=Provenance(kind="observed", source="test", record_id="ambiguous"))
    unknown = NormalizedEvent(event_id="unknown", timestamp=UTC_TIME, source="custom", layer=Layer.HYPERVISOR, severity="error", message="failure", provenance=Provenance(kind="observed", source="test", record_id="unknown"))
    for item in (database, vm, ambiguous, unknown):
        with pytest.raises(HypervisorAgentInputError, match="hypervisor-routed"):
            agent.analyze((route_and_resolve(item, ROUTING_CONFIG),))
    assert transport.prompts == []


def _with_vm_topology(event: RoutedEvent, *, vm_id: str = "vm-7", host_id: str = "host-01") -> RoutedEvent:
    relationship = TopologyRelationship(
        relationship_id="topology-vm-host",
        source=ComponentRef(component_id=vm_id, kind="vm", layer="vm_guest_os"),
        target=ComponentRef(component_id=host_id, kind="host", layer="host_hypervisor"),
        relationship_type=RelationshipType.RUNS_ON,
        provenance=Provenance(kind="synthetic", source="test", record_id="topology-vm-host"),
        evidence=(EvidenceReference(evidence_id="topology-evidence", kind="topology", reference_id="topology-vm-host"),),
    )
    return event.model_copy(update={"topology": TopologyResolution(relationships=(relationship,))})


def test_valid_host_vm_impact_requires_existing_topology() -> None:
    event = _with_vm_topology(routed("event-1", "host contention"))
    response = json.loads(analysis(("event-1",)))
    response["findings"][0]["impacts"] = [{"affected_component": {"component_id": "vm-7", "kind": "vm", "layer": "vm_guest_os"}, "statement": "The host issue may affect vm-7.", "topology_relationship_ids": ["topology-vm-host"]}]
    agent, _ = runtime(json.dumps(response))
    assert agent.analyze((event,)).output.findings[0].impacts[0].affected_component.component_id == "vm-7"


@pytest.mark.parametrize("change", [
    lambda impact: impact.update({"topology_relationship_ids": ["missing-topology"]}),
    lambda impact: impact["affected_component"].update({"component_id": "invented-vm"}),
])
def test_topology_mismatch_or_fabricated_impact_fails_safely(change: object) -> None:
    event = _with_vm_topology(routed("event-1", "host contention"))
    response = json.loads(analysis(("event-1",)))
    impact = {"affected_component": {"component_id": "vm-7", "kind": "vm", "layer": "vm_guest_os"}, "statement": "Host may affect VM.", "topology_relationship_ids": ["topology-vm-host"]}
    change(impact)  # type: ignore[operator]
    response["findings"][0]["impacts"] = [impact]
    agent, _ = runtime(json.dumps(response), repairs=0)
    with pytest.raises(SpecialistRuntimeError):
        agent.analyze((event,))


def test_impact_without_topology_and_malformed_responses_fail_safely() -> None:
    response = json.loads(analysis(("event-1",)))
    response["findings"][0]["impacts"] = [{"affected_component": {"component_id": "vm-7", "kind": "vm", "layer": "vm_guest_os"}, "statement": "Host may affect VM.", "topology_relationship_ids": ["missing-topology"]}]
    agent, _ = runtime(json.dumps(response), repairs=0)
    with pytest.raises(SpecialistRuntimeError):
        agent.analyze((routed("event-1", "host failure"),))
    event = routed("event-2", "host failure")
    agent, _ = runtime("not-json", repairs=0)
    with pytest.raises(SpecialistRuntimeError) as raised:
        agent.analyze((event,))
    assert raised.value.code is SpecialistFailureCode.INVALID_OUTPUT
    agent, transport = runtime(["not-json", analysis(("event-2",))])
    assert agent.analyze((event,)).metadata.repair_count == 1
    assert len(transport.prompts) == 2
    agent, _ = runtime(["not-json", "still-not-json"], repairs=1)
    with pytest.raises(SpecialistRuntimeError) as raised:
        agent.analyze((event,))
    assert raised.value.code is SpecialistFailureCode.INVALID_OUTPUT
