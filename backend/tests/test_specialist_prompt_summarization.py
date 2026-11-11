"""Specialist prompts stay within the size limit without losing evidence or time order.

Regression: the real 21-minute Splitwise baseline (63 VM events) built a 67,731-character A2 prompt; the 50,000
limit failed A2 with context_limit_failure. Per-event routing explanations and topology were repeated on every event.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cloudrca_backend.anomaly_signals import anomaly_signals
from cloudrca_backend.contracts import Layer
from cloudrca_backend.database_agent import DatabaseAgent, build_database_prompt
from cloudrca_backend.event_summary import NOTABLE_HEAD, NOTABLE_TAIL, VERBATIM_EVENT_LIMIT
from cloudrca_backend.hypervisor_agent import HypervisorAgent
from cloudrca_backend.main_rca_agent import MainRcaAgent
from cloudrca_backend.normalization import normalize_file
from cloudrca_backend.pipeline import AnalysisAgents, RcaPipeline
from cloudrca_backend.routing import RoutedEvent, RoutingLayer, load_routing_config, route_and_resolve
from cloudrca_backend.specialist_runtime import SpecialistRequest, SpecialistRuntime, SpecialistRuntimeConfig
from cloudrca_backend.vm_agent import EXAMPLE_VM_FINDING, VMAgent, build_vm_prompt

from backend.tests.test_main_rca_event_summarization import large_incident_records

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "tests" / "fixtures" / "splitwise" / "real_baseline_healthy.jsonl"
ROUTING = load_routing_config(ROOT / "config" / "routing" / "aiops2025.yaml")
LIMIT = 50_000  # the configured default for CLOUDRCA_SPECIALIST_MAX_PROMPT_CHARACTERS


def routed_from(path: Path) -> list[RoutedEvent]:
    return [route_and_resolve(event, ROUTING) for event in normalize_file(path).events]


def layer_events(routed: list[RoutedEvent], layer: RoutingLayer) -> tuple[RoutedEvent, ...]:
    return tuple(item for item in routed if item.routing.layer is layer)


def write(tmp_path: Path, records: list[dict[str, Any]]) -> Path:
    path = tmp_path / "dataset.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records))
    return path


def input_of(prompt: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(next(line for line in prompt.splitlines() if line.startswith("INPUT=")).removeprefix("INPUT="))
    return data


def vm_input(events: tuple[RoutedEvent, ...]) -> tuple[str, dict[str, Any]]:
    prompt = build_vm_prompt(SpecialistRequest(layer=Layer.VM, events=events), "v1")
    return prompt, input_of(prompt)


def offsets_to_times(item: dict[str, Any]) -> list[datetime]:
    first = datetime.fromisoformat(item["first"]["timestamp"])
    return [first + timedelta(seconds=offset) for offset in item["offsets_s"]]


def test_the_real_baseline_a2_prompt_fits_and_accounts_for_every_event() -> None:
    events = layer_events(routed_from(BASELINE), RoutingLayer.VM)
    assert len(events) == 63
    prompt, data = vm_input(events)
    assert len(prompt) < LIMIT  # was 67,731
    summary = data["event_summary"]
    assert summary["total_events"] == 63 and summary["verbatim_events"] == len(data["events"])
    assert summary["verbatim_events"] + summary["condensed_events"] == 63
    assert sum(item["count"] for item in data["event_series"]) == summary["condensed_events"]
    listed = {item["event_id"] for item in data["events"]}
    by_id = {item.event.event_id: item.event for item in events}
    for item in data["event_series"]:
        assert item["first"]["event_id"] in by_id and item["last"]["event_id"] in by_id
        assert len(item["offsets_s"]) == item["count"]
        assert all(len(column) == item["count"] for column in item.get("values", {}).values())
        assert not {item["first"]["event_id"], item["last"]["event_id"]} & listed


def test_time_order_is_preserved_in_events_and_series() -> None:
    events = layer_events(routed_from(BASELINE), RoutingLayer.VM)
    _, data = vm_input(events)
    times = [item["timestamp"] for item in data["events"]]
    assert times == sorted(times)
    by_id = {item.event.event_id: item.event for item in events}
    for item in data["event_series"]:
        series_times = offsets_to_times(item)
        assert series_times == sorted(series_times)
        assert series_times[0] == by_id[item["first"]["event_id"]].timestamp and series_times[-1] == by_id[item["last"]["event_id"]].timestamp


def test_repeated_context_is_listed_once_not_per_event() -> None:
    events = layer_events(routed_from(BASELINE), RoutingLayer.VM)
    _, data = vm_input(events)
    assert all({"topology", "provenance"}.isdisjoint(item) and set(item["routing"]) == {"layer", "confidence"} for item in data["events"])
    assert all(item["routing"]["layer"] == "vm_guest_os" for item in data["events"])
    [rule] = data["context"]["routing_rules"]
    assert rule["event_count"] == 63 and rule["explanation"]
    [gap] = data["context"]["topology"]["unavailable"]
    assert gap["target_kind"] == "host" and "AWS does not expose" in gap["reason"]


def test_small_batches_are_listed_in_full_without_series() -> None:
    events = layer_events(routed_from(ROOT / "tests/fixtures/synthetic/splitwise_vm_pressure_db_failure.jsonl"), RoutingLayer.VM)
    assert len(events) <= VERBATIM_EVENT_LIMIT
    _, data = vm_input(events)
    assert {item["event_id"] for item in data["events"]} == {item.event.event_id for item in events}
    assert "event_series" not in data and "event_summary" not in data
    assert data["anomaly_signals"]["count"] >= 3  # credit exhaustion, CPU saturation, load per CPU are visible to A2


def test_signal_events_stay_verbatim_at_both_ends_and_every_one_is_accounted_for(tmp_path: Path) -> None:
    events = layer_events(routed_from(write(tmp_path, large_incident_records(60, 100))), RoutingLayer.VM)
    prompt, data = vm_input(events)
    assert len(prompt) < LIMIT
    flagged = [signal.event_id for signal in anomaly_signals(item.event for item in events) if signal.check == "load_per_cpu"]
    assert len(flagged) > NOTABLE_HEAD + NOTABLE_TAIL
    order = sorted(flagged, key=lambda event_id: next(item.event.timestamp for item in events if item.event.event_id == event_id))
    listed = {item["event_id"] for item in data["events"]}
    assert set(order[:NOTABLE_HEAD] + order[-NOTABLE_TAIL:]) <= listed  # onset and latest occurrences stay verbatim
    [repeated] = [item for item in data["event_series"] if item.get("signal") == "load_per_cpu"]
    assert repeated["repeated_signal"] is True and repeated["count"] == len(flagged) - NOTABLE_HEAD - NOTABLE_TAIL
    assert data["anomaly_signals"]["count"] == len(anomaly_signals(item.event for item in events))


def test_a_database_error_storm_keeps_head_tail_and_every_occurrence(tmp_path: Path) -> None:
    events = layer_events(routed_from(write(tmp_path, large_incident_records(60, 100))), RoutingLayer.DATABASE)
    assert len(events) == 100
    prompt = build_database_prompt(SpecialistRequest(layer=Layer.DATABASE, events=events), "v1")
    data = input_of(prompt)
    assert len(prompt) < LIMIT
    records = sorted(int(str(item["record_id"]).split("-")[1]) for item in data["events"])
    assert records == [0, 1, 2, 98, 99]
    [storm] = data["event_series"]
    assert storm["repeated_notable"] is True and storm["count"] == 95 and storm["values"]["duration_ms"] == [5000 + index for index in range(3, 98)]


def limited_agents(transport: Any) -> AnalysisAgents:
    config = SpecialistRuntimeConfig(
        jcode_binary="jcode", provider_profile="test", provider_base_url="http://localhost:11434/v1", model="qwen-test",
        timeout_seconds=5, max_events=100, max_prompt_characters=LIMIT, max_repair_attempts=1, prompt_version="test-v1",
    )
    runtime = SpecialistRuntime(config, transport)
    return AnalysisAgents(
        specialists={Layer.DATABASE: DatabaseAgent(runtime), Layer.VM: VMAgent(runtime), Layer.HYPERVISOR: HypervisorAgent(runtime)},
        main_rca=MainRcaAgent(config, transport),
    )


def test_the_real_baseline_runs_a2_under_the_default_limit_and_completes_healthy(scripted_jcode: Any) -> None:
    transport = scripted_jcode()
    result = RcaPipeline(lambda _: (BASELINE.name, BASELINE.read_bytes()), lambda: limited_agents(transport), ROUTING)({"dataset_id": "d"})
    [incident] = [payload for kind, payload in result.artifacts if kind.value == "incident"]
    assert [(item["specialist"], item["status"], item["error_code"]) for item in incident["specialist_workflow"]["executions"]] == [("A2", "success", None)]
    assert incident["analysis_outcome"] == "no_anomaly_detected"


def test_a2_may_cite_a_condensed_event_from_a_series(scripted_jcode: Any) -> None:
    events = layer_events(routed_from(BASELINE), RoutingLayer.VM)
    _, data = vm_input(events)
    condensed = data["event_series"][0]["last"]["event_id"]
    assert condensed not in {item["event_id"] for item in data["events"]}
    at = data["event_series"][0]["last"]["timestamp"]

    class CitesSeries(scripted_jcode):  # type: ignore[misc, valid-type]
        def respond(self, prompt: str) -> str:
            if "CLOUDRCA_VM_PROMPT_VERSION=" not in prompt:
                return str(super().respond(prompt))
            finding = json.loads(json.dumps(EXAMPLE_VM_FINDING.model_dump(mode="json")).replace("event-id-from-input", condensed).replace("vm-id-from-input", "i-0baseline0000001"))
            finding["time_range"] = {"start": at, "end": at}  # a finding's range must equal its evidence range
            return json.dumps({"findings": [finding]})

    transport = CitesSeries()
    result = RcaPipeline(lambda _: (BASELINE.name, BASELINE.read_bytes()), lambda: limited_agents(transport), ROUTING)({"dataset_id": "d"})
    [finding] = [payload for kind, payload in result.artifacts if kind.value == "finding"]
    assert condensed in json.dumps(finding)  # accepted: every supplied event stays a valid citation
    [incident] = [payload for kind, payload in result.artifacts if kind.value == "incident"]
    assert incident["analysis_outcome"] != "no_anomaly_detected"  # a finding always proceeds to the main RCA


@pytest.mark.parametrize("builder", [build_vm_prompt, build_database_prompt])
def test_every_specialist_prompt_explains_the_bounded_input(builder: Any) -> None:
    events = layer_events(routed_from(ROOT / "tests/fixtures/synthetic/splitwise_vm_pressure_db_failure.jsonl"), RoutingLayer.VM)
    layer = Layer.VM if builder is build_vm_prompt else Layer.DATABASE
    if layer is Layer.DATABASE:
        events = layer_events(routed_from(ROOT / "tests/fixtures/synthetic/splitwise_vm_pressure_db_failure.jsonl"), RoutingLayer.DATABASE)
    prompt = builder(SpecialistRequest(layer=layer, events=events), "v1")
    assert "INPUT.events are in time order" in prompt and prompt.index("INPUT.events are in time order") < prompt.index("INPUT=")
