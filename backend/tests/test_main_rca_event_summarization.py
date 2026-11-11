"""Large incidents fit the main RCA prompt without losing evidence: notable events verbatim, routine values in series."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cloudrca_backend.correlation import CorrelationConfig, CrossLayerCorrelator
from cloudrca_backend.incident_grouping import GroupingConfig, IncidentGrouper
from cloudrca_backend.main_rca_agent import (
    NOTABLE_HEAD,
    NOTABLE_TAIL,
    TOPOLOGY_EVIDENCE_LIMIT,
    VERBATIM_EVENT_LIMIT,
    MainRcaAgent,
    MainRcaRequest,
    build_main_rca_prompt,
    validate_main_rca_report,
)
from cloudrca_backend.normalization import normalize_file
from cloudrca_backend.pipeline import AnalysisAgents, RcaPipeline, incident_topology
from cloudrca_backend.routing import load_routing_config, route_and_resolve
from cloudrca_backend.specialist_runtime import SpecialistRuntimeConfig

ROOT = Path(__file__).resolve().parents[2]
ROUTING = load_routing_config(ROOT / "config/routing/aiops2025.yaml")
CONFIG = SpecialistRuntimeConfig(
    jcode_binary="jcode", provider_profile="cloudrca-test", provider_base_url="http://localhost:11434/v1", model="rca",
    timeout_seconds=5, max_events=100, max_prompt_characters=200_000, max_repair_attempts=1, prompt_version="v2",
)
INSTANCE = "i-large-incident"
START = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)


def at(seconds: float) -> str:
    return (START + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def large_incident_records(minutes: int = 60, lock_errors: int = 100) -> list[dict[str, Any]]:
    """Realistic Splitwise shape: per-minute heartbeats, pool, and request summaries; 5-min CPU; an error storm."""
    base = {"source": "splitwise.applog", "log_group": "/splitwise/app", "log_stream": INSTANCE}
    records: list[dict[str, Any]] = []
    for minute in range(minutes):
        t = minute * 60
        records.append({"cloudrca_record": "host.heartbeat", "timestamp": at(t), **base, "event_id": f"hb-{minute}", "instance_id": INSTANCE,
                        "load_1m": round(0.4 + minute / 20, 2), "cpu_count": 2, "mem_available_mb": 600 - minute * 5, "mem_total_mb": 1024})
        records.append({"cloudrca_record": "app.pool_snapshot", "timestamp": at(t + 1), **base, "event_id": f"pool-{minute}",
                        "service": "splitwise-api", "instance_id": INSTANCE, "db_pool_size": 5, "db_pool_checked_out": min(5, minute // 10)})
        records.append({"cloudrca_record": "app.request_summary", "timestamp": at(t + 2), **base, "event_id": f"req-{minute}", "level": "info",
                        "service": "splitwise-api", "instance_id": INSTANCE, "window_seconds": 60, "request_count": 40, "error_5xx_count": 0,
                        "write_count": 20, "max_db_ms": 20 + minute * 80})
        if minute % 5 == 0:
            records.append({"cloudrca_record": "aws.cloudwatch.metric", "timestamp": at(t), "source": "aws.cloudwatch.metrics", "event_id": f"cpu-{minute}",
                            "namespace": "AWS/EC2", "metric_name": "CPUUtilization", "statistic": "Average", "period_seconds": 300, "unit": "Percent",
                            "value": 20.0 + minute, "dimensions": {"InstanceId": INSTANCE}})
    for index in range(lock_errors):
        records.append({"cloudrca_record": "app.error", "timestamp": at(minutes * 60 - 600 + index * 5), **base, "event_id": f"lock-{index}",
                        "level": "error", "reported_by": "database", "service": "splitwise-api", "instance_id": INSTANCE, "database_id": "splitwise-sqlite",
                        "request_id": f"r-{index}", "route": "/expenses", "method": "POST", "status": 500, "error_class": "sqlalchemy.exc.OperationalError",
                        "message": "SYNTHETIC: database is locked", "operation": "INSERT expenses", "duration_ms": 5000 + index, "db_wait_ms": 5000})
    for index, (error_class, message) in enumerate((("AttributeError", "SYNTHETIC: 'NoneType' object has no attribute 'title'"), ("ValueError", "SYNTHETIC: withheld"))):
        records.append({"cloudrca_record": "app.error", "timestamp": at(minutes * 60 - 300 + index), **base, "event_id": f"app-error-{index}", "level": "error",
                        "reported_by": "application", "service": "splitwise-api", "instance_id": INSTANCE, "error_class": error_class, "message": message, "status": 500})
    return records


def request_for(tmp_path: Path, records: list[dict[str, Any]]) -> MainRcaRequest:
    path = tmp_path / "large.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records))
    routed = tuple(route_and_resolve(event, ROUTING) for event in normalize_file(path).events)
    [incident] = IncidentGrouper(GroupingConfig()).group(routed).incidents
    return MainRcaRequest(
        incident=incident, graph=CrossLayerCorrelator(CorrelationConfig()).correlate(incident, routed),
        topology=incident_topology(routed), events=tuple(item.event for item in routed),
    )


def payload(prompt: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(next(line for line in prompt.splitlines() if line.startswith("INPUT=")).removeprefix("INPUT="))
    return data


def record_ids(rca_request: MainRcaRequest, event_ids: list[str] | set[str]) -> set[str]:
    by_id = {event.event_id: event.provenance.record_id for event in rca_request.events}
    return {str(by_id[event_id]) for event_id in event_ids}


def test_small_incidents_are_listed_unchanged() -> None:
    fixture = ROOT / "tests/fixtures/synthetic/splitwise_vm_pressure_db_failure.jsonl"
    routed = tuple(route_and_resolve(event, ROUTING) for event in normalize_file(fixture).events)
    [incident] = IncidentGrouper(GroupingConfig()).group(routed).incidents
    rca_request = MainRcaRequest(incident=incident, graph=CrossLayerCorrelator(CorrelationConfig()).correlate(incident, routed), events=tuple(item.event for item in routed))
    assert len(rca_request.events) <= VERBATIM_EVENT_LIMIT
    data = payload(build_main_rca_prompt(rca_request, "v2"))
    assert "event_series" not in data and "event_summary" not in data
    assert {item["event_id"] for item in data["events"]} == {event.event_id for event in rca_request.events}


def test_a_large_incident_fits_and_accounts_for_every_event(tmp_path: Path) -> None:
    rca_request = request_for(tmp_path, large_incident_records())
    total = len(rca_request.events)
    assert total > 250
    prompt = build_main_rca_prompt(rca_request, "v2")
    assert len(prompt) < 50_000
    data = payload(prompt)
    summary = data["event_summary"]
    assert summary["total_events"] == total
    assert summary["verbatim_events"] == len(data["events"])
    assert summary["verbatim_events"] + summary["condensed_events"] == total
    assert sum(item["count"] for item in data["event_series"]) == summary["condensed_events"]
    # Every event on the instance supports its runs_on relationship: topology lists a few IDs and the full count.
    runs_on = max(data["topology"]["relationships"], key=lambda item: item.get("evidence_count", 0))
    assert runs_on["evidence_count"] > 100 and len(runs_on["evidence_reference_ids"]) == TOPOLOGY_EVIDENCE_LIMIT
    assert set(runs_on["evidence_reference_ids"]) <= {item["event_id"] for item in data["events"]}


def test_every_distinct_error_is_verbatim_and_a_storm_keeps_head_tail_and_every_occurrence(tmp_path: Path) -> None:
    rca_request = request_for(tmp_path, large_incident_records())
    data = payload(build_main_rca_prompt(rca_request, "v2"))
    listed = record_ids(rca_request, {item["event_id"] for item in data["events"]})
    assert {"app-error-0", "app-error-1"} <= listed  # distinct errors are never condensed
    lock_listed = sorted(int(record.split("-")[1]) for record in listed if record.startswith("lock-"))
    assert lock_listed == [0, 1, 2, 98, 99] and NOTABLE_HEAD == 3 and NOTABLE_TAIL == 2
    [storm] = [item for item in data["event_series"] if item.get("repeated_notable")]
    assert storm["count"] == 95 and storm["message"] == "sqlalchemy.exc.OperationalError: SYNTHETIC: database is locked"
    assert storm["offsets_s"] == [5 * index for index in range(95)] and storm["values"]["duration_ms"] == [5000 + index for index in range(3, 98)]
    assert storm["constant"] == {"db_wait_ms": 5000, "status": 500}  # unchanging measures are stated once, not dropped


def test_condensed_series_keep_every_value_and_cite_real_extremes(tmp_path: Path) -> None:
    rca_request = request_for(tmp_path, large_incident_records())
    data = payload(build_main_rca_prompt(rca_request, "v2"))
    [heartbeats] = [item for item in data["event_series"] if item["record_kind"] == "host.heartbeat"]
    events = {event.event_id: event for event in rca_request.events}
    assert len(heartbeats["values"]["load_1m"]) == heartbeats["count"] == 58  # 60 minus the two verbatim endpoints
    assert heartbeats["values"]["load_1m"] == [round(0.4 + minute / 20, 2) for minute in range(1, 59)]
    peak = events[heartbeats["extremes"]["load_1m"]["max_event_id"]]
    assert peak.provenance.record_id == "hb-58"
    low_memory = events[heartbeats["extremes"]["mem_available_mb"]["min_event_id"]]
    assert low_memory.provenance.record_id == "hb-58"
    for item in data["event_series"]:
        assert item["first"]["event_id"] in events and item["last"]["event_id"] in events


def test_condensed_events_remain_valid_citations(tmp_path: Path) -> None:
    rca_request = request_for(tmp_path, large_incident_records())
    data = payload(build_main_rca_prompt(rca_request, "v2"))
    listed = {item["event_id"] for item in data["events"]}
    condensed = next(event for event in rca_request.events if event.event_id not in listed)
    service = next(item for item in rca_request.incident.affected_components if item.component_id == "splitwise-api")
    citation = [{"evidence_id": "c1", "kind": "event", "reference_id": condensed.event_id}]
    report = {
        "incident_id": rca_request.incident.incident_id, "probable_root_cause": "test", "root_cause_layer": "application",
        "root_cause_component": service.model_dump(mode="json", exclude={"schema_version"}),
        "timeline": [{"timestamp": "2026-11-01T09:00:00Z", "summary": "t", "event_ids": [condensed.event_id]}],
        "causal_chain": [{"cause_id": "splitwise-api", "effect_id": condensed.event_id, "summary": "s", "confidence": {"score": 0.5, "level": "medium"}, "evidence": citation}],
        "evidence": citation, "uncertainty": "test", "confidence": {"score": 0.5, "level": "medium"},
        "alternative_hypotheses": [{"summary": "alt", "layer": "vm_guest_os", "confidence": {"score": 0.2, "level": "low"}, "evidence": citation}],
        "runtime": {"provider": "x", "model": "x", "harness": "x"},
    }
    assert validate_main_rca_report(json.dumps(report), rca_request, CONFIG).evidence[0].reference_id == condensed.event_id


def test_specialist_cited_events_stay_identified_and_the_pipeline_completes(tmp_path: Path, scripted_jcode: Any, agents_for: Any) -> None:
    """The scripted database specialist cites all 40 lock errors: each stays in INPUT by ID with its own time."""
    path = tmp_path / "large.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in large_incident_records(minutes=30, lock_errors=40)))
    transport = scripted_jcode()
    scripted: AnalysisAgents = agents_for(transport)
    limited = SpecialistRuntimeConfig(**{**CONFIG.model_dump(), "max_prompt_characters": 50_000, "max_events": 200})
    captured: list[MainRcaRequest] = []
    main_rca = MainRcaAgent(limited, transport)

    class Capturing:
        def analyze(self, rca_request: MainRcaRequest) -> Any:
            captured.append(rca_request)
            return main_rca.analyze(rca_request)

    result = RcaPipeline(lambda _: (path.name, path.read_bytes()), lambda: AnalysisAgents(scripted.specialists, Capturing()), ROUTING)({"dataset_id": "d"})
    [incident] = [item for kind, item in result.artifacts if kind.value == "incident"]
    assert incident["analysis_outcome"] == "rca_reported"  # fits the default 50,000-character limit
    rca_request = captured[0]
    data = payload(build_main_rca_prompt(rca_request, "v2"))
    cited = set().union(*(_cited(finding.finding) for finding in rca_request.incident.findings))
    assert len(cited) >= 40
    events = {event.event_id: event for event in rca_request.events}
    listed = {item["event_id"] for item in data["events"]}
    in_series: dict[str, datetime] = {}
    for item in data["event_series"]:
        first = datetime.fromisoformat(item["first"]["timestamp"])
        for event_id, index in item.get("cited_event_ids", {}).items():
            in_series[event_id] = first + timedelta(seconds=item["offsets_s"][index])
    assert cited <= listed | set(in_series)
    assert in_series and all(events[event_id].timestamp == time for event_id, time in in_series.items())


def _cited(value: Any) -> set[str]:
    if isinstance(value, dict):
        found = set(value.get("evidence_event_ids", [])) if isinstance(value.get("evidence_event_ids"), list) else set()
        return found.union(*(_cited(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_cited(item) for item in value))
    return set()
