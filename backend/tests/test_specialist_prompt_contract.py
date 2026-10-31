"""Specialist prompts state every required finding field and carry one schema-valid example.

Regression: live qwen2.5-coder:7b runs of A2 repeatedly omitted the required ``severity`` field (and once
misspelled ``hypotheses``) because the fields were only described inside a ~5k-character JSON Schema.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from cloudrca_backend.contracts import Layer
from cloudrca_backend.database_agent import DatabaseAnalysis, DatabaseFinding, build_database_prompt
from cloudrca_backend.hypervisor_agent import HypervisorAnalysis, HypervisorFinding, build_hypervisor_prompt
from cloudrca_backend.normalization import normalize_file
from cloudrca_backend.routing import load_routing_config, route_and_resolve
from cloudrca_backend.specialist_runtime import SpecialistRequest
from cloudrca_backend.vm_agent import VMAnalysis, VMFinding, build_vm_prompt
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]
CASES = [
    (Layer.DATABASE, build_database_prompt, DatabaseFinding, DatabaseAnalysis),
    (Layer.VM, build_vm_prompt, VMFinding, VMAnalysis),
    (Layer.HYPERVISOR, build_hypervisor_prompt, HypervisorFinding, HypervisorAnalysis),
]


def request(layer: Layer) -> SpecialistRequest:
    batch = normalize_file(ROOT / "tests" / "fixtures" / "synthetic" / "redis_vm_cpu_incident.jsonl")
    routing = load_routing_config(ROOT / "config" / "routing" / "aiops2025.yaml")
    return SpecialistRequest(layer=layer, events=tuple(route_and_resolve(event, routing) for event in batch.events[:2]))


def line(prompt: str, prefix: str) -> str:
    return next(item for item in prompt.splitlines() if item.startswith(prefix))


@pytest.mark.parametrize(("layer", "builder", "finding_model", "analysis_model"), CASES)
def test_prompt_names_every_required_finding_field(
    layer: Layer, builder: Callable[[SpecialistRequest, str], str], finding_model: type[BaseModel], analysis_model: type[BaseModel]
) -> None:
    prompt = builder(request(layer), "v1")
    stated = line(prompt, "REQUIRED_FINDING_FIELDS=").split("=", 1)[1].split(".", 1)[0].split(",")
    required = [name for name, field in finding_model.model_fields.items() if field.is_required()]
    assert stated == required
    assert "severity" in stated and "confidence" in stated


@pytest.mark.parametrize(("layer", "builder", "finding_model", "analysis_model"), CASES)
def test_prompt_example_validates_against_the_real_schema(
    layer: Layer, builder: Callable[[SpecialistRequest, str], str], finding_model: type[BaseModel], analysis_model: type[BaseModel]
) -> None:
    specialist_request = request(layer)
    prompt = builder(specialist_request, "v1")
    example = line(prompt, "EXAMPLE_OUTPUT").split(")=", 1)[1]
    analysis = analysis_model.model_validate_json(example)  # the example is a valid output shape
    [finding] = json.loads(example)["findings"]
    assert all(name in finding for name, field in finding_model.model_fields.items() if field.is_required())
    assert len(analysis.findings) == 1  # type: ignore[attr-defined]
    # The example never contains real input IDs, so it cannot be mistaken for evidence.
    assert not any(item.event.event_id in example for item in specialist_request.events)
    # Instructions remain ahead of the schema and input.
    assert prompt.index("REQUIRED_FINDING_FIELDS=") < prompt.index("_ANALYSIS_SCHEMA=") < prompt.index("INPUT=")
