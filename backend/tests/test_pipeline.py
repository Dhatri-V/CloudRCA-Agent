"""Production RCA pipeline tests using committed AIOps2025 fixtures and a scripted JCode boundary."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from cloudrca_backend.api import AnalysisResult, ArtifactKind, JobStatus
from cloudrca_backend.pipeline import AnalysisAgents, RcaPipeline, build_production_pipeline
from cloudrca_backend.routing import load_routing_config
from cloudrca_backend.settings import SettingsError
from cloudrca_backend.specialist_runtime import JCodeProviderError, JCodeTimeoutError

ROUTING = load_routing_config(Path(__file__).resolve().parents[2] / "config" / "routing" / "aiops2025.yaml")


def pipeline(content: bytes, factory: Callable[[], AnalysisAgents], filename: str = "aiops.jsonl", **options: Any) -> RcaPipeline:
    return RcaPipeline(lambda dataset_id: (filename, content) if dataset_id == "dataset-1" else None, factory, ROUTING, **options)


def artifacts(result: AnalysisResult, kind: ArtifactKind) -> list[dict[str, object]]:
    return [payload for item, payload in result.artifacts if item is kind]


def run(content: bytes, factory: Callable[[], AnalysisAgents], **options: Any) -> AnalysisResult:
    return pipeline(content, factory, **options)({"dataset_id": "dataset-1"})


def test_complete_pipeline_produces_evidence_backed_artifacts(aiops_jsonl: bytes, scripted_jcode: Any, agents_for: Any) -> None:
    transport = scripted_jcode()
    result = run(aiops_jsonl, lambda: agents_for(transport))

    assert result.status is JobStatus.COMPLETED and result.error is None
    summary = artifacts(result, ArtifactKind.RUN)[0]
    assert summary["normalization"] == {"input": 14, "accepted": 10, "duplicates": 0, "rejected": 4}
    assert summary["routing"] == {"database": 6, "vm_guest_os": 4}

    events = {str(item["event_id"]): item for item in artifacts(result, ArtifactKind.EVENT)}
    assert len(events) == 10
    # Each specialist received only events routed to its own layer.
    for stage, layer in (("database", "database"), ("vm", "vm_guest_os")):
        assert transport.prompts[stage]
        assert all(event["routing"]["layer"] == layer for prompt in transport.prompts[stage] for event in prompt["events"])
    assert transport.prompts["hypervisor"] == []

    incidents = artifacts(result, ArtifactKind.INCIDENT)
    outcomes = sorted(str(item["analysis_outcome"]) for item in incidents)
    assert outcomes == ["insufficient_evidence", "rca_reported", "rca_reported"]
    for incident in incidents:
        assert set(incident["event_ids"]) <= set(events)  # type: ignore[arg-type]
    vm_incident = next(item for item in incidents if item["layer"] == "vm_guest_os")
    assert "Insufficient evidence" in str(vm_incident["summary"]) and "no root cause was inferred" in str(vm_incident["uncertainty"])

    reports = artifacts(result, ArtifactKind.REPORT)
    assert len(reports) == 2
    findings = {str(item["finding_id"]) for item in artifacts(result, ArtifactKind.FINDING)}
    for report in reports:
        cited = {str(item["reference_id"]) for item in report["evidence"]}  # type: ignore[attr-defined]
        assert cited & set(events) and cited <= set(events) | findings
        assert report["runtime"] == {"provider": "test", "model": "glm-test", "harness": "jcode", "harness_version": None, "schema_version": "1.0.0"}
        assert str(report["markdown"]).startswith(f"# RCA report: {report['incident_id']}")
    assert len(artifacts(result, ArtifactKind.GRAPH)) == 3
    # The main RCA saw the specialist findings attached to its incident.
    assert all(prompt["incident"]["findings"] for prompt in transport.prompts["main_rca"])


@pytest.mark.parametrize(
    ("content", "filename", "message"),
    [
        (b"", "empty.jsonl", "dataset is empty"),
        (b"{not json}\n[1]\n", "broken.jsonl", "MALFORMED_RECORD=2"),
        (b'{"event": "ok"}', "events.json", "UNSUPPORTED_RECORD=1"),
        (b"a,b\n1,2\n", "events.csv", "UNSUPPORTED_RECORD=1"),
    ],
)
def test_unusable_datasets_fail_with_safe_reason(content: bytes, filename: str, message: str, scripted_jcode: Any, agents_for: Any) -> None:
    transport = scripted_jcode()
    result = pipeline(content, lambda: agents_for(transport), filename)({"dataset_id": "dataset-1"})
    assert result.status is JobStatus.FAILED
    assert result.error is not None and "no supported events" in result.error and message in result.error
    assert not artifacts(result, ArtifactKind.INCIDENT)
    assert all(not prompts for prompts in transport.prompts.values())


def test_missing_dataset_reference_fails(scripted_jcode: Any, agents_for: Any) -> None:
    analysis = pipeline(b"", lambda: agents_for(scripted_jcode()))
    assert analysis({}).error == "analysis job does not reference a dataset"
    assert analysis({"dataset_id": "unknown"}).error == "dataset not found"


def test_duplicate_records_are_counted_not_reanalyzed(aiops_jsonl: bytes, scripted_jcode: Any, agents_for: Any) -> None:
    result = run(aiops_jsonl + b"\n" + aiops_jsonl, lambda: agents_for(scripted_jcode()))
    summary = artifacts(result, ArtifactKind.RUN)[0]
    assert summary["normalization"] == {"input": 28, "accepted": 10, "duplicates": 10, "rejected": 8}
    assert len(artifacts(result, ArtifactKind.EVENT)) == 10


def test_missing_llm_configuration_fails_job_but_keeps_deterministic_evidence(aiops_jsonl: bytes) -> None:
    def unconfigured() -> AnalysisAgents:
        raise SettingsError("Missing provider credential environment variable: ZHIPU_API_KEY")

    result = run(aiops_jsonl, unconfigured)
    assert result.status is JobStatus.FAILED
    assert result.error == "LLM runtime is not configured: Missing provider credential environment variable: ZHIPU_API_KEY"
    incidents = artifacts(result, ArtifactKind.INCIDENT)
    assert incidents and all(item["analysis_outcome"] == "not_analyzed" for item in incidents)
    assert len(artifacts(result, ArtifactKind.EVENT)) == 10 and not artifacts(result, ArtifactKind.REPORT)


def test_specialist_timeouts_fail_job_without_reports(aiops_jsonl: bytes, scripted_jcode: Any, agents_for: Any) -> None:
    transport = scripted_jcode({"database": [JCodeTimeoutError("late")] * 2, "vm": [JCodeTimeoutError("late")]})
    result = run(aiops_jsonl, lambda: agents_for(transport))
    assert result.status is JobStatus.FAILED
    incidents = artifacts(result, ArtifactKind.INCIDENT)
    assert {item["analysis_outcome"] for item in incidents} == {"specialist_failed"}
    assert all("timeout" in str(item["summary"]) for item in incidents)
    assert not artifacts(result, ArtifactKind.REPORT) and not artifacts(result, ArtifactKind.FINDING)
    assert transport.prompts["main_rca"] == []


def test_invalid_rca_output_yields_partial_job(aiops_jsonl: bytes, scripted_jcode: Any, agents_for: Any) -> None:
    transport = scripted_jcode({"main_rca": ["not json"] * 2})
    result = run(aiops_jsonl, lambda: agents_for(transport))
    assert result.status is JobStatus.PARTIAL
    assert result.error == "1 of 3 incidents were not fully analyzed"
    outcomes = sorted(str(item["analysis_outcome"]) for item in artifacts(result, ArtifactKind.INCIDENT))
    assert outcomes == ["insufficient_evidence", "rca_failed", "rca_reported"]
    failed = next(item for item in artifacts(result, ArtifactKind.INCIDENT) if item["analysis_outcome"] == "rca_failed")
    assert failed["summary"] == "Analysis incomplete: main RCA failed (invalid_structured_output)."
    assert len(artifacts(result, ArtifactKind.REPORT)) == 1


def test_incident_limit_marks_job_partial(aiops_jsonl: bytes, scripted_jcode: Any, agents_for: Any) -> None:
    result = run(aiops_jsonl, lambda: agents_for(scripted_jcode()), max_incidents=1)
    assert result.status is JobStatus.PARTIAL
    outcomes = [str(item["analysis_outcome"]) for item in artifacts(result, ArtifactKind.INCIDENT)]
    assert outcomes.count("not_analyzed") == 2


def test_provider_error_details_are_not_persisted(aiops_jsonl: bytes, scripted_jcode: Any, agents_for: Any) -> None:
    secret = "sk-test-should-never-be-stored"
    transport = scripted_jcode({"database": [JCodeProviderError(f"provider rejected key {secret}")] * 2})
    result = run(aiops_jsonl, lambda: agents_for(transport))
    serialized = json.dumps([payload for _, payload in result.artifacts]) + str(result.error)
    assert secret not in serialized
    assert "provider_or_jcode_failure" in serialized


def test_production_pipeline_reads_limits_from_environment(tmp_path: Path) -> None:
    routing = str(Path(__file__).resolve().parents[2] / "config" / "routing" / "aiops2025.yaml")
    with pytest.raises(SettingsError, match="CLOUDRCA_PIPELINE_MAX_INCIDENTS"):
        build_production_pipeline(lambda _: None, {"CLOUDRCA_PIPELINE_MAX_INCIDENTS": "many", "CLOUDRCA_ROUTING_CONFIG": routing})
    analysis = build_production_pipeline(lambda _: ("a.jsonl", b'{"time": "x"}'), {"CLOUDRCA_ROUTING_CONFIG": routing})
    result = analysis({"dataset_id": "dataset-1"})
    assert result.status is JobStatus.FAILED and result.error is not None
