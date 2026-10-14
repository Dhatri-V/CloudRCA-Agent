"""Shared test doubles for pipeline tests: a scripted JCode boundary and AIOps2025 fixture datasets."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from cloudrca_backend.contracts import Layer
from cloudrca_backend.database_agent import DatabaseAgent
from cloudrca_backend.hypervisor_agent import HypervisorAgent
from cloudrca_backend.main_rca_agent import MainRcaAgent
from cloudrca_backend.pipeline import AnalysisAgents
from cloudrca_backend.specialist_runtime import (
    JCodeResponse,
    JCodeSession,
    SpecialistRuntime,
    SpecialistRuntimeConfig,
    TokenUsage,
)
from cloudrca_backend.vm_agent import VMAgent

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "aiops2025"
MARKERS = {
    "database": "CLOUDRCA_DATABASE_PROMPT_VERSION=",
    "vm": "CLOUDRCA_VM_PROMPT_VERSION=",
    "hypervisor": "CLOUDRCA_HYPERVISOR_PROMPT_VERSION=",
    "main_rca": "CLOUDRCA_MAIN_RCA_PROMPT_VERSION=",
}


def _input(prompt: str) -> dict[str, Any]:
    lines = [line for line in prompt.splitlines() if line.startswith("INPUT=")]
    payload: dict[str, Any] = json.loads(lines[-1].removeprefix("INPUT="))
    return payload


def _database_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    events = payload["events"]
    ids = [item["event_id"] for item in events]
    identifiers = events[0]["component_ids"]
    component_id = identifiers["database_id"] or identifiers["pod_id"]
    return {
        "findings": [
            {
                "error_type": "availability_failure",
                "severity": "error",
                "component": {"component_id": component_id, "kind": "database", "layer": "database"},
                "symptoms": ["Database-routed activity is present in the cited events."],
                "observations": [{"statement": "The cited database events were recorded.", "evidence_event_ids": ids}],
                "hypotheses": [
                    {
                        "type": "hypothesis",
                        "statement": "The database component may have been degraded.",
                        "evidence_event_ids": ids[:1],
                        "confidence": {"score": 0.4, "level": "low"},
                    }
                ],
                "confidence": {"score": 0.5, "level": "medium"},
            }
        ]
    }


def _report(payload: dict[str, Any]) -> dict[str, Any]:
    incident = payload["incident"]
    component = next(
        (item for item in incident["affected_components"] if item["layer"] == "database"), incident["affected_components"][0]
    )
    node = payload["correlation_graph"]["nodes"][0]
    evidence = [{"evidence_id": "cite-event", "kind": "event", "reference_id": node["event_id"]}]
    evidence += [{"evidence_id": f"cite-{item['finding_id']}", "kind": "finding", "reference_id": item["finding_id"]} for item in incident["findings"]]
    return {
        "incident_id": incident["incident_id"],
        "probable_root_cause": f"Possible degradation of {component['component_id']} (scripted test response)",
        "root_cause_layer": component["layer"],
        "root_cause_component": component,
        "timeline": [{"timestamp": node["timestamp"], "summary": "First cited event", "event_ids": [node["event_id"]]}],
        "causal_chain": [
            {
                "cause_id": component["component_id"],
                "effect_id": node["event_id"],
                "summary": "Component degradation may explain the cited event",
                "confidence": {"score": 0.4, "level": "low"},
                "evidence": evidence[:1],
            }
        ],
        "evidence": evidence,
        "confidence": {"score": 0.4, "level": "low"},
        "uncertainty": "Scripted test response; evidence is limited to the supplied fixture events.",
        "runtime": {"provider": "placeholder", "model": "placeholder", "harness": "placeholder"},
    }


class ScriptedJCode:
    """Fake JCode transport that answers from each prompt's INPUT; ``failures`` override responses per stage."""

    def __init__(self, failures: dict[str, list[str | Exception]] | None = None) -> None:
        self.failures = failures or {}
        self.prompts: dict[str, list[dict[str, Any]]] = {name: [] for name in MARKERS}

    def respond(self, prompt: str) -> str:
        stage = next(name for name, marker in MARKERS.items() if marker in prompt)
        payload = _input(prompt)
        self.prompts[stage].append(payload)
        queued = self.failures.get(stage)
        if queued:
            action = queued.pop(0)
            if isinstance(action, Exception):
                raise action
            return action
        if stage == "database":
            return json.dumps(_database_analysis(payload))
        if stage == "main_rca":
            return json.dumps(_report(payload))
        return json.dumps({"findings": []})

    @contextmanager
    def session(self, config: SpecialistRuntimeConfig, correlation_id: str) -> Iterator[JCodeSession]:
        del config, correlation_id
        yield _Session(self)


class _Session:
    def __init__(self, transport: ScriptedJCode) -> None:
        self._transport = transport

    def complete(self, prompt: str, timeout_seconds: float) -> JCodeResponse:
        del timeout_seconds
        return JCodeResponse(text=self._transport.respond(prompt), session_id="scripted", model="scripted", usage=TokenUsage())


def runtime_config(model: str) -> SpecialistRuntimeConfig:
    return SpecialistRuntimeConfig(
        jcode_binary="jcode", provider_profile="test", provider_base_url="http://localhost:11434/v1", model=model,
        timeout_seconds=5, max_events=100, max_prompt_characters=200_000, max_repair_attempts=1, prompt_version="test-v1",
    )


def scripted_agents(transport: ScriptedJCode) -> AnalysisAgents:
    runtime = SpecialistRuntime(runtime_config("qwen-test"), transport)
    return AnalysisAgents(
        specialists={Layer.DATABASE: DatabaseAgent(runtime), Layer.VM: VMAgent(runtime), Layer.HYPERVISOR: HypervisorAgent(runtime)},
        main_rca=MainRcaAgent(runtime_config("glm-test"), transport),
    )


def _jsonable(value: object) -> object:
    return value.isoformat() if isinstance(value, datetime) else value


@pytest.fixture
def aiops_jsonl() -> bytes:
    """Committed AIOps2025 log, node-metric, and pod-metric fixtures as one uploadable JSONL dataset."""
    rows = [
        {key: _jsonable(value) for key, value in row.items()}
        for name in ("logs", "node_metrics", "pod_metrics")
        for row in pl.read_parquet(FIXTURES / f"{name}.parquet").to_dicts()
    ]
    return "\n".join(json.dumps(row, ensure_ascii=False) for row in rows).encode("utf-8")


@pytest.fixture
def agents_for() -> Callable[[ScriptedJCode], AnalysisAgents]:
    return scripted_agents


@pytest.fixture
def scripted_jcode() -> type[ScriptedJCode]:
    return ScriptedJCode
