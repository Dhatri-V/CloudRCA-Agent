"""Issue #11 deterministic specialist orchestration tests."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import cast

import pytest
from cloudrca_backend.contracts import Layer, NormalizedEvent, Provenance
from cloudrca_backend.orchestration import (
    AnalysisBatch,
    OrchestrationInputError,
    SpecialistOrchestrator,
    SpecialistStatus,
    WorkflowStatus,
)
from cloudrca_backend.routing import RoutedEvent, load_routing_config, route_and_resolve
from cloudrca_backend.specialist_runtime import (
    SpecialistExecutionMetadata,
    SpecialistFailureCode,
    SpecialistRuntimeError,
    SpecialistStructuredRunResult,
)
from pydantic import BaseModel, ValidationError

UTC_TIME = datetime(2025, 4, 29, 6, 3, 8, tzinfo=timezone.utc)
ROUTING_CONFIG = load_routing_config("config/routing/aiops2025.yaml")


class Output(BaseModel):
    finding: str


def metadata(layer: Layer, *, success: bool = True, error: SpecialistFailureCode | None = None) -> SpecialistExecutionMetadata:
    return SpecialistExecutionMetadata(
        correlation_id="trace-1",
        specialist_layer=layer,
        provider_profile="test",
        model="qwen-test",
        prompt_version="test-v1",
        duration_ms=1,
        repair_count=0,
        success=success,
        session_id="jcode-session",
        error_code=error,
        error_message="failed" if error else None,
    )


class Agent:
    def __init__(self, layer: Layer, actions: list[object]) -> None:
        self.layer = layer
        self.actions = actions
        self.calls: list[tuple[tuple[RoutedEvent, ...], str | None]] = []

    def analyze(
        self, events: tuple[RoutedEvent, ...], *, correlation_id: str | None = None
    ) -> SpecialistStructuredRunResult[Output]:
        self.calls.append((events, correlation_id))
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        if callable(action):
            action()
            action = "success"
        return SpecialistStructuredRunResult(
            output=Output(finding=cast(str, action)), metadata=metadata(self.layer)
        )


def routed(event_id: str, layer: Layer, *, source: str | None = None) -> RoutedEvent:
    identifiers = {
        Layer.DATABASE: {"database_id": "db-1", "source": source or "redis"},
        Layer.VM: {"vm_id": "vm-1", "source": source or "node"},
        Layer.HYPERVISOR: {"host_id": "host-1", "source": source or "libvirt"},
    }[layer]
    event = NormalizedEvent(
        event_id=event_id,
        timestamp=UTC_TIME,
        source=cast(str, identifiers.pop("source")),
        layer=layer,
        severity="error",
        message="test failure",
        provenance=Provenance(kind="observed", source="test", record_id=event_id),
        **identifiers,
    )
    return route_and_resolve(event, ROUTING_CONFIG)


def batch(*events: RoutedEvent, run_id: str = "run-1") -> AnalysisBatch:
    return AnalysisBatch(
        run_id=run_id,
        session_id="session-1",
        incident_id="incident-1",
        trace_id="trace-1",
        events=events,
    )


def agents(*, database: list[object] | None = None, vm: list[object] | None = None, host: list[object] | None = None) -> tuple[SpecialistOrchestrator, dict[Layer, Agent]]:
    configured = {
        Layer.DATABASE: Agent(Layer.DATABASE, database or ["database"]),
        Layer.VM: Agent(Layer.VM, vm or ["vm"]),
        Layer.HYPERVISOR: Agent(Layer.HYPERVISOR, host or ["hypervisor"]),
    }
    return SpecialistOrchestrator(configured), configured


def test_three_agent_run_partitions_events_and_preserves_identifiers() -> None:
    orchestrator, configured = agents()
    result = orchestrator.run(batch(routed("db", Layer.DATABASE), routed("vm", Layer.VM), routed("host", Layer.HYPERVISOR)))
    assert result.status is WorkflowStatus.SUCCESS
    assert [item.layer for item in result.executions] == [Layer.DATABASE, Layer.VM, Layer.HYPERVISOR]
    assert (result.run_id, result.session_id, result.incident_id, result.trace_id) == ("run-1", "session-1", "incident-1", "trace-1")
    assert [agent.calls[0][0][0].event.event_id for agent in configured.values()] == ["db", "vm", "host"]
    assert all(agent.calls[0][1] == "trace-1" for agent in configured.values())


@pytest.mark.parametrize("layer", list(Layer))
def test_single_layer_runs_only_its_specialist(layer: Layer) -> None:
    orchestrator, configured = agents()
    result = orchestrator.run(batch(routed("event-1", layer)))
    assert [item.layer for item in result.executions] == [layer]
    assert [configured_layer for configured_layer, agent in configured.items() if agent.calls] == [layer]


def test_aggregate_order_is_deterministic_not_input_or_completion_order() -> None:
    orchestrator, _ = agents()
    result = orchestrator.run(batch(routed("host", Layer.HYPERVISOR), routed("db", Layer.DATABASE), routed("vm", Layer.VM)))
    assert [item.layer for item in result.executions] == [Layer.DATABASE, Layer.VM, Layer.HYPERVISOR]


def failure(layer: Layer, code: SpecialistFailureCode = SpecialistFailureCode.PROVIDER) -> SpecialistRuntimeError:
    return SpecialistRuntimeError(code, "provider failed", metadata(layer, success=False, error=code))


def test_partial_failure_timeout_and_multiple_failures_remain_visible() -> None:
    orchestrator, _ = agents(vm=[failure(Layer.VM)])
    result = orchestrator.run(batch(routed("db", Layer.DATABASE), routed("vm", Layer.VM), routed("host", Layer.HYPERVISOR)))
    assert result.status is WorkflowStatus.PARTIAL
    assert [item.status for item in result.executions] == [SpecialistStatus.SUCCESS, SpecialistStatus.FAILED, SpecialistStatus.SUCCESS]
    orchestrator, _ = agents(database=[failure(Layer.DATABASE, SpecialistFailureCode.TIMEOUT)], vm=[failure(Layer.VM)])
    result = orchestrator.run(batch(routed("db", Layer.DATABASE), routed("vm", Layer.VM)))
    assert result.status is WorkflowStatus.FAILED
    assert [item.status for item in result.executions] == [SpecialistStatus.TIMEOUT, SpecialistStatus.FAILED]


def test_cancellation_before_and_during_execution_remains_visible() -> None:
    cancelled = {"value": True}
    orchestrator = SpecialistOrchestrator({}, cancelled=lambda: cancelled["value"])
    result = orchestrator.run(batch(routed("db", Layer.DATABASE)))
    assert result.status is WorkflowStatus.CANCELLED
    assert result.executions[0].status is SpecialistStatus.CANCELLED
    cancelled["value"] = False
    host_agent = Agent(Layer.HYPERVISOR, [lambda: cancelled.update(value=True)])
    orchestrator = SpecialistOrchestrator({Layer.HYPERVISOR: host_agent}, cancelled=lambda: cancelled["value"])
    result = orchestrator.run(batch(routed("host", Layer.HYPERVISOR)))
    assert result.status is WorkflowStatus.CANCELLED
    assert result.executions[0].status is SpecialistStatus.CANCELLED


def test_bounded_retry_has_one_final_result_without_duplicates() -> None:
    database = Agent(Layer.DATABASE, [failure(Layer.DATABASE), "recovered"])
    orchestrator = SpecialistOrchestrator({Layer.DATABASE: database}, max_attempts=2)
    result = orchestrator.run(batch(routed("db", Layer.DATABASE)))
    assert len(result.executions) == 1
    assert result.executions[0].status is SpecialistStatus.SUCCESS
    assert result.executions[0].attempt_count == 2
    assert len(database.calls) == 2


def test_replay_and_audit_preserve_safe_provenance_without_raw_secrets() -> None:
    orchestrator, _ = agents(database=["original", "replay"])
    event = routed("db", Layer.DATABASE)
    secret_event = event.model_copy(update={"event": event.event.model_copy(update={"message": "token=secret-value"})})
    original = orchestrator.run(batch(secret_event, run_id="original-run"))
    replay = orchestrator.replay(
        original.run_id,
        replay_run_id="replay-run",
        session_id="replay-session",
        incident_id="incident-1",
        trace_id="replay-trace",
    )
    record = orchestrator.audit_record("original-run")
    assert replay.replay_of_run_id == "original-run"
    assert replay.run_id == "replay-run"
    assert record.event_ids == ("db",)
    assert "CLOUDRCA_QWEN_API_KEY" not in record.model_dump_json()
    assert "secret-value" not in record.model_dump_json()


def test_duplicate_and_ambiguous_or_unknown_events_are_rejected_before_dispatch() -> None:
    orchestrator, configured = agents()
    duplicate = routed("same", Layer.DATABASE)
    with pytest.raises(OrchestrationInputError, match="unique"):
        orchestrator.run(batch(duplicate, duplicate))
    ambiguous_event = NormalizedEvent(event_id="ambiguous", timestamp=UTC_TIME, source="libvirt", layer=Layer.HYPERVISOR, severity="error", service="redis", message="failure", provenance=Provenance(kind="observed", source="test", record_id="ambiguous"))
    unknown_event = NormalizedEvent(event_id="unknown", timestamp=UTC_TIME, source="custom", layer=Layer.VM, severity="error", message="failure", provenance=Provenance(kind="observed", source="test", record_id="unknown"))
    for event in (ambiguous_event, unknown_event):
        with pytest.raises(OrchestrationInputError, match="definitively"):
            orchestrator.run(batch(route_and_resolve(event, ROUTING_CONFIG)))
    assert not any(agent.calls for agent in configured.values())
    with pytest.raises(ValidationError):
        batch()
