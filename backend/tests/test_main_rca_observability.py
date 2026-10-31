"""Main RCA validation failures are logged safely, and repair prompts stay bounded."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

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
)

SECRET_MARKER = "RAW-RESPONSE-TEXT-MUST-NOT-BE-LOGGED"


def config(max_prompt_characters: int = 100_000) -> SpecialistRuntimeConfig:
    return SpecialistRuntimeConfig(
        jcode_binary="jcode", provider_profile="cloudrca-test", provider_base_url="http://localhost:11434/v1", model="rca-test",
        timeout_seconds=10, max_events=10, max_prompt_characters=max_prompt_characters, max_repair_attempts=1, prompt_version="v1",
    )


def request() -> MainRcaRequest:
    event = EvidenceReference(evidence_id="evidence-1", kind="event", reference_id="event-1")
    component = ComponentRef(component_id="vm-1", kind="vm", layer="vm_guest_os")
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
    incident = CandidateIncident(
        incident_id="incident-1", time_window=TimeWindow(start=moment, end=moment), source_events=(event,),
        affected_components=(component,), affected_layers={Layer.VM},
        rationales=(GroupingRationale(reason=GroupingReasonType.SINGLETON, event_ids=("event-1",)),),
    )
    return MainRcaRequest(incident=incident, graph=CorrelationGraph(incident_id="incident-1", nodes=()))


def report(reference: str = "event-1") -> str:
    citation = [{"evidence_id": "cite-1", "kind": "event", "reference_id": reference}]
    return json.dumps({
        "incident_id": "incident-1", "probable_root_cause": "VM CPU saturation", "root_cause_layer": "vm_guest_os",
        "root_cause_component": {"component_id": "vm-1", "kind": "vm", "layer": "vm_guest_os"},
        "timeline": [{"timestamp": "2026-01-01T00:00:00Z", "summary": "CPU rose", "event_ids": ["event-1"]}],
        "causal_chain": [{"cause_id": "vm-1", "effect_id": "event-1", "summary": "CPU starvation", "confidence": {"score": 0.7, "level": "medium"}, "evidence": citation}],
        "evidence": citation, "confidence": {"score": 0.7, "level": "medium"}, "uncertainty": None,
        "runtime": {"provider": "x", "model": "x", "harness": "x"},
    })


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **values: Any) -> None:
        self.events.append((event, values))

    def named(self, name: str) -> list[dict[str, Any]]:
        return [values for event, values in self.events if event == name]


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
                return JCodeResponse(text=transport.replies.pop(0), session_id="s", model="rca-test")

        yield Session()


def test_schema_failure_logs_types_and_paths_only_then_repair_succeeds() -> None:
    invalid = json.dumps({"incident_id": "incident-1", "probable_root_cause": SECRET_MARKER, "padding": "z" * 9_000})
    logger, transport = Recorder(), Transport([invalid, report()])
    result = MainRcaAgent(config(), transport, logger=logger).analyze(request())

    assert result.metadata.repair_count == 1
    [failure] = logger.named("main_rca_validation_failed")
    assert failure["attempt"] == 1 and failure["response_characters"] == len(invalid)
    assert {"type": "missing", "loc": "root_cause_layer"} in failure["issues"]
    assert {"type": "extra_forbidden", "loc": "<unexpected_field>"} in failure["issues"]
    assert all(set(issue) == {"type", "loc"} for issue in failure["issues"])
    assert SECRET_MARKER not in json.dumps(logger.events)


def test_semantic_failures_log_issue_types_without_identifiers() -> None:
    logger = Recorder()
    with pytest.raises(MainRcaError) as error:
        MainRcaAgent(config(), Transport([report("invented-id-123"), report("invented-id-456")]), logger=logger).analyze(request())
    assert error.value.code is SpecialistFailureCode.INVALID_OUTPUT
    failures = logger.named("main_rca_validation_failed")
    assert [item["attempt"] for item in failures] == [1, 2]
    assert failures[0]["issues"] == [
        {"type": "unknown_evidence_reference", "loc": "evidence.0.reference_id"},
        {"type": "unknown_evidence_reference", "loc": "causal_chain.0.evidence.0.reference_id"},
    ]
    assert "invented-id" not in json.dumps(logger.events)


def test_repair_prompt_echoes_only_a_bounded_excerpt_of_the_rejected_reply() -> None:
    huge = "{" + "q" * 30_000
    transport = Transport([huge, report()])
    MainRcaAgent(config(), transport, logger=Recorder()).analyze(request())
    original, repair = transport.prompts
    assert len(repair) - len(original) < 5_000  # was ~30,000 extra characters before the excerpt bound
    assert "[truncated" in repair


def test_oversized_repair_prompt_is_logged_with_sizes() -> None:
    logger, transport = Recorder(), Transport(["not json"])
    probe = Transport([report()])
    MainRcaAgent(config(), probe, logger=Recorder()).analyze(request())
    limit = len(probe.prompts[0]) + 10  # the original prompt fits; the repair prompt cannot
    with pytest.raises(MainRcaError) as error:
        MainRcaAgent(config(limit), transport, logger=logger).analyze(request())
    assert error.value.code is SpecialistFailureCode.CONTEXT_LIMIT
    [event] = logger.named("main_rca_repair_prompt_too_large")
    assert event["repair_prompt_characters"] > event["max_prompt_characters"] == limit
    assert set(event) == {"correlation_id", "prompt_characters", "repair_prompt_characters", "max_prompt_characters"}


def test_unexpected_field_names_from_the_model_are_never_logged() -> None:
    key = "x" * 500 + SECRET_MARKER
    logger = Recorder()
    with pytest.raises(MainRcaError):
        MainRcaAgent(config(), Transport([json.dumps({key: 1}), json.dumps({key: 1})]), logger=logger).analyze(request())
    issues = [issue for failure in logger.named("main_rca_validation_failed") for issue in failure["issues"]]
    assert {"type": "extra_forbidden", "loc": "<unexpected_field>"} in issues
    assert all(len(issue["loc"]) <= 120 for issue in issues)
    assert SECRET_MARKER not in json.dumps(logger.events) and "xxxxxxxx" not in json.dumps(logger.events)


def test_falsy_injected_logger_is_still_used() -> None:
    class FalsyRecorder(Recorder):
        def __len__(self) -> int:
            return 0

    logger = FalsyRecorder()
    MainRcaAgent(config(), Transport(["not json", report()]), logger=logger).analyze(request())
    assert logger.named("main_rca_validation_failed")
