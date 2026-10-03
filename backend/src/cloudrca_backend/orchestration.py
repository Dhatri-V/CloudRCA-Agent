"""Deterministic, sequential orchestration for the A1/A2/A3 specialists."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Protocol

import structlog
from pydantic import BaseModel, Field

from .contracts import ContractModel, Identifier, Layer
from .routing import RoutedEvent, RoutingLayer
from .specialist_runtime import (
    SpecialistExecutionMetadata,
    SpecialistFailureCode,
    SpecialistRuntimeError,
    SpecialistStructuredRunResult,
)


class SpecialistStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class WorkflowStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


LAYER_ORDER = (Layer.DATABASE, Layer.VM, Layer.HYPERVISOR)


class AnalysisBatch(ContractModel):
    """Caller-owned identifiers and routed events for one analysis workflow."""

    run_id: Identifier
    session_id: Identifier
    incident_id: Identifier
    trace_id: Identifier
    events: tuple[RoutedEvent, ...] = Field(min_length=1)


class SpecialistExecution(ContractModel):
    """One visible terminal state for an applicable specialist layer."""

    layer: Layer
    specialist: Identifier
    status: SpecialistStatus
    attempt_count: int = Field(ge=0)
    output: dict[str, object] | None = None
    metadata: SpecialistExecutionMetadata | None = None
    error_code: SpecialistFailureCode | None = None
    error_message: str | None = None


class SpecialistWorkflowResult(ContractModel):
    """Deterministically ordered specialist results for one run or replay."""

    run_id: Identifier
    session_id: Identifier
    incident_id: Identifier
    trace_id: Identifier
    status: WorkflowStatus
    executions: tuple[SpecialistExecution, ...] = Field(min_length=1)
    replay_of_run_id: Identifier | None = None


class AuditRecord(ContractModel):
    """Safe audit view: identifiers, event references, statuses, and public outputs only."""

    result: SpecialistWorkflowResult
    event_ids: tuple[Identifier, ...] = Field(min_length=1)
    event_layers: tuple[Layer, ...] = Field(min_length=1)


class OrchestrationInputError(ValueError):
    """Raised before dispatch for empty, duplicate, or non-definitively-routed batches."""


class SpecialistAgent(Protocol):
    def analyze(
        self, events: tuple[RoutedEvent, ...], *, correlation_id: str | None = None
    ) -> SpecialistStructuredRunResult[BaseModel]:
        """Run its own validated specialist analysis."""


class ExecutionAuditStore:
    """Small in-memory audit/replay store until Issue #18 adds durable persistence."""

    def __init__(self) -> None:
        self._records: dict[str, AuditRecord] = {}
        self._batches: dict[str, AnalysisBatch] = {}

    def save(self, batch: AnalysisBatch, result: SpecialistWorkflowResult) -> AuditRecord:
        record = AuditRecord(
            result=result,
            event_ids=tuple(item.event.event_id for item in batch.events),
            event_layers=tuple(item.event.layer for item in batch.events),
        )
        self._records[result.run_id] = record
        self._batches[result.run_id] = batch
        return record

    def get(self, run_id: str) -> AuditRecord:
        return self._records[run_id]

    def batch_for_replay(self, run_id: str) -> AnalysisBatch:
        return self._batches[run_id]


class SpecialistOrchestrator:
    """Coordinate existing specialists without duplicating their runtime or validation."""

    def __init__(
        self,
        specialists: Mapping[Layer, SpecialistAgent],
        *,
        audit_store: ExecutionAuditStore | None = None,
        max_attempts: int = 1,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        self._specialists = dict(specialists)
        self._audit_store = audit_store or ExecutionAuditStore()
        self._max_attempts = max_attempts
        self._cancelled = cancelled
        self._logger = structlog.get_logger("cloudrca.orchestration")

    @staticmethod
    def _partitions(batch: AnalysisBatch) -> dict[Layer, tuple[RoutedEvent, ...]]:
        event_ids = [item.event.event_id for item in batch.events]
        if len(set(event_ids)) != len(event_ids):
            raise OrchestrationInputError("analysis batch event IDs must be unique")
        expected = {Layer.DATABASE: RoutingLayer.DATABASE, Layer.VM: RoutingLayer.VM, Layer.HYPERVISOR: RoutingLayer.HYPERVISOR}
        partitions: dict[Layer, list[RoutedEvent]] = {layer: [] for layer in LAYER_ORDER}
        invalid = [item.event.event_id for item in batch.events if item.routing.layer not in expected.values()]
        if invalid:
            raise OrchestrationInputError(f"analysis batch requires definitively routed events: {', '.join(invalid)}")
        for item in batch.events:
            layer = next(layer for layer, routed_layer in expected.items() if item.routing.layer is routed_layer)
            partitions[layer].append(item)
        return {layer: tuple(events) for layer, events in partitions.items() if events}

    @staticmethod
    def _status(executions: tuple[SpecialistExecution, ...]) -> WorkflowStatus:
        statuses = {item.status for item in executions}
        if statuses == {SpecialistStatus.SUCCESS}:
            return WorkflowStatus.SUCCESS
        if SpecialistStatus.SUCCESS in statuses:
            return WorkflowStatus.PARTIAL
        if SpecialistStatus.CANCELLED in statuses:
            return WorkflowStatus.CANCELLED
        return WorkflowStatus.FAILED

    def _cancelled_execution(self, layer: Layer) -> SpecialistExecution:
        return SpecialistExecution(
            layer=layer,
            specialist=f"A{LAYER_ORDER.index(layer) + 1}",
            status=SpecialistStatus.CANCELLED,
            attempt_count=0,
            error_message="workflow cancellation requested",
        )

    def _execute(self, layer: Layer, events: tuple[RoutedEvent, ...], trace_id: str) -> SpecialistExecution:
        specialist = self._specialists.get(layer)
        if specialist is None:
            return SpecialistExecution(
                layer=layer,
                specialist=f"A{LAYER_ORDER.index(layer) + 1}",
                status=SpecialistStatus.FAILED,
                attempt_count=0,
                error_message="applicable specialist is not configured",
            )
        for attempt in range(1, self._max_attempts + 1):
            if self._cancelled():
                return self._cancelled_execution(layer)
            try:
                result = specialist.analyze(events, correlation_id=trace_id)
            except SpecialistRuntimeError as exc:
                if exc.code is SpecialistFailureCode.TIMEOUT:
                    status = SpecialistStatus.TIMEOUT
                else:
                    status = SpecialistStatus.FAILED
                if attempt == self._max_attempts:
                    return SpecialistExecution(
                        layer=layer,
                        specialist=f"A{LAYER_ORDER.index(layer) + 1}",
                        status=status,
                        attempt_count=attempt,
                        metadata=exc.metadata,
                        error_code=exc.code,
                        error_message=str(exc),
                    )
            else:
                if self._cancelled():
                    return self._cancelled_execution(layer)
                return SpecialistExecution(
                    layer=layer,
                    specialist=f"A{LAYER_ORDER.index(layer) + 1}",
                    status=SpecialistStatus.SUCCESS,
                    attempt_count=attempt,
                    output=result.output.model_dump(mode="json"),
                    metadata=result.metadata,
                )
        raise AssertionError("bounded specialist execution must return")

    def run(self, batch: AnalysisBatch, *, replay_of_run_id: str | None = None) -> SpecialistWorkflowResult:
        partitions = self._partitions(batch)
        executions = tuple(
            self._cancelled_execution(layer) if self._cancelled() else self._execute(layer, partitions[layer], batch.trace_id)
            for layer in LAYER_ORDER
            if layer in partitions
        )
        result = SpecialistWorkflowResult(
            run_id=batch.run_id,
            session_id=batch.session_id,
            incident_id=batch.incident_id,
            trace_id=batch.trace_id,
            status=self._status(executions),
            executions=executions,
            replay_of_run_id=replay_of_run_id,
        )
        self._audit_store.save(batch, result)
        self._logger.info(
            "specialist_workflow_completed",
            run_id=result.run_id,
            session_id=result.session_id,
            incident_id=result.incident_id,
            trace_id=result.trace_id,
            status=result.status.value,
            layers=[item.layer.value for item in result.executions],
        )
        return result

    def replay(
        self,
        run_id: str,
        *,
        replay_run_id: str,
        session_id: str,
        incident_id: str,
        trace_id: str,
    ) -> SpecialistWorkflowResult:
        original = self._audit_store.batch_for_replay(run_id)
        batch = AnalysisBatch(
            run_id=replay_run_id,
            session_id=session_id,
            incident_id=incident_id,
            trace_id=trace_id,
            events=original.events,
        )
        return self.run(batch, replay_of_run_id=run_id)

    def audit_record(self, run_id: str) -> AuditRecord:
        return self._audit_store.get(run_id)
