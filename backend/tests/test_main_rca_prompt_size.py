"""Regression: the main RCA prompt for a correlated incident must fit the default prompt limit.

The synthetic Redis/VM incident (8 events, 27 correlation edges) produced an 83k-character prompt because
every edge repeated each event's full provenance, exceeding CLOUDRCA_MAIN_RCA_MAX_PROMPT_CHARACTERS=50000.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cloudrca_backend.api import ArtifactKind, JobStatus
from cloudrca_backend.main_rca_agent import MainRcaAgent, MainRcaRequest, build_main_rca_prompt
from cloudrca_backend.pipeline import AnalysisAgents, RcaPipeline
from cloudrca_backend.routing import load_routing_config
from cloudrca_backend.specialist_runtime import SpecialistRuntimeConfig

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "synthetic" / "redis_vm_cpu_incident.jsonl"
DEFAULT_LIMIT = 50_000


def test_synthetic_incident_rca_fits_the_default_prompt_limit(scripted_jcode: Any, agents_for: Any) -> None:
    transport = scripted_jcode()
    scripted: AnalysisAgents = agents_for(transport)
    config = SpecialistRuntimeConfig(
        jcode_binary="jcode", provider_profile="test", provider_base_url="http://localhost:11434/v1", model="rca-test",
        timeout_seconds=5, max_events=100, max_prompt_characters=DEFAULT_LIMIT, max_repair_attempts=1, prompt_version="test-v1",
    )
    agents = AnalysisAgents(specialists=scripted.specialists, main_rca=MainRcaAgent(config, transport))
    captured: list[MainRcaRequest] = []

    class Capturing:
        def analyze(self, request: MainRcaRequest) -> Any:
            captured.append(request)
            return agents.main_rca.analyze(request)

    pipeline = RcaPipeline(
        lambda _: (FIXTURE.name, FIXTURE.read_bytes()),
        lambda: AnalysisAgents(specialists=agents.specialists, main_rca=Capturing()),
        load_routing_config(ROOT / "config" / "routing" / "aiops2025.yaml"),
    )
    result = pipeline({"dataset_id": "synthetic"})

    assert result.status is JobStatus.COMPLETED
    assert [payload["analysis_outcome"] for kind, payload in result.artifacts if kind is ArtifactKind.INCIDENT] == ["rca_reported"]
    prompt = build_main_rca_prompt(captured[0], "test-v1")
    assert len(prompt) <= DEFAULT_LIMIT
    # Every ID the report may cite is still present in the compact prompt.
    graph = captured[0].graph
    assert len(graph.edges) >= 20
    for reference_id in {ref.reference_id for edge in graph.edges for ref in edge.evidence} | {
        relationship for edge in graph.edges for relationship in edge.topology_relationship_ids
    }:
        assert reference_id in prompt
    for finding in captured[0].incident.findings:
        assert finding.finding_id in prompt
