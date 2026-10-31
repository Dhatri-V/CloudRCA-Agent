"""R5: the CloudWatch/Splitwise converter keeps every error, summarizes only ordinary requests, and copies no private fields."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cloudrca_backend.normalization import normalize_file
from cloudrca_backend.pipeline import RcaPipeline
from cloudrca_backend.routing import load_routing_config

from scripts.convert_cloudwatch_export import main

ROOT = Path(__file__).resolve().parents[2]
EXPORT = ROOT / "tests" / "fixtures" / "splitwise" / "export"
ROUTING = load_routing_config(ROOT / "config" / "routing" / "aiops2025.yaml")


def run(tmp_path: Path, *args: str) -> list[dict[str, Any]]:
    output = tmp_path / "converted.jsonl"
    assert main([*args, "--output", str(output)]) == 0
    return [json.loads(line) for line in output.read_text().splitlines()]


def full(tmp_path: Path) -> list[dict[str, Any]]:
    return run(
        tmp_path, "--app-log", str(EXPORT / "app_log.jsonl"),
        "--metric-data", str(EXPORT / "metric_data.json"), "--metric-queries", str(EXPORT / "metric_queries.json"),
    )


def of(records: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [record for record in records if record["cloudrca_record"] == kind]


def test_output_is_accepted_by_the_normalizer(tmp_path: Path) -> None:
    full(tmp_path)
    batch = normalize_file(tmp_path / "converted.jsonl")
    assert batch.statistics.rejected == 0 and batch.statistics.accepted == 9


def test_every_valid_error_is_kept_one_to_one_with_its_identifiers(tmp_path: Path) -> None:
    errors = of(full(tmp_path), "app.error")
    assert [(item["reported_by"], item["request_id"], item["error_class"]) for item in errors] == [
        ("database", "req-0003", "sqlalchemy.exc.OperationalError"),
        ("application", "req-0004", "AttributeError"),
    ]
    database_error = errors[0]
    assert database_error["database_id"] == "splitwise-sqlite" and database_error["db_wait_ms"] == 5000
    assert database_error["timestamp"] == "2026-10-30T10:01:10Z" and database_error["instance_id"] == "i-0synthetic01"


def test_request_summaries_aggregate_without_dropping_error_counts(tmp_path: Path) -> None:
    summaries = of(full(tmp_path), "app.request_summary")
    assert [(item["timestamp"], item["request_count"], item["error_5xx_count"], item["write_count"]) for item in summaries] == [
        ("2026-10-30T10:00:00Z", 2, 0, 1),
        ("2026-10-30T10:01:00Z", 2, 2, 2),
    ]
    busy = summaries[1]
    assert (busy["p95_duration_ms"], busy["max_duration_ms"], busy["max_db_ms"], busy["max_in_flight"]) == (5011.0, 5011.0, 5004.0, 7)
    assert busy["level"] == "warning" and summaries[0]["level"] == "info"
    assert all(set(item) & {"request_id", "message", "error_class"} == set() for item in summaries)  # no per-request details


def test_metrics_keep_namespace_dimensions_statistic_and_period(tmp_path: Path) -> None:
    metrics = of(full(tmp_path), "aws.cloudwatch.metric")
    assert [(item["metric_name"], item["timestamp"], item["value"]) for item in metrics] == [
        ("CPUUtilization", "2026-10-30T09:55:00Z", 12.0),
        ("CPUUtilization", "2026-10-30T10:00:00Z", 41.5),
        ("CPUCreditBalance", "2026-10-30T10:00:00Z", 143.0),
    ]
    cpu = metrics[1]
    assert (cpu["namespace"], cpu["dimensions"], cpu["statistic"], cpu["period_seconds"], cpu["unit"]) == (
        "AWS/EC2", {"InstanceId": "i-0synthetic01"}, "Average", 300, "Percent",
    )


def test_window_defaults_to_the_errors_and_drops_records_outside_it(tmp_path: Path) -> None:
    records = full(tmp_path)
    times = sorted(record["timestamp"] for record in records)
    assert times[0] >= "2026-10-30T09:46:00Z" and times[-1] <= "2026-10-30T10:06:30Z"
    assert not any(record.get("request_id") == "req-9999" for record in records)
    assert "2026-10-30T08:00:00Z" not in times


def test_explicit_window_and_no_error_refusal(tmp_path: Path) -> None:
    records = run(tmp_path, "--app-log", str(EXPORT / "app_log.jsonl"), "--start", "2026-10-30T11:00:00Z", "--end", "2026-10-30T12:00:00Z")
    assert [record["cloudrca_record"] for record in records] == ["app.request_summary"]
    quiet = tmp_path / "quiet.jsonl"
    quiet.write_text(json.dumps({"event": "request", "ts": "2026-10-30T10:00:00Z", "service": "s", "instance_id": "i", "status": 200}))
    with pytest.raises(SystemExit, match="No error events found"):
        main(["--app-log", str(quiet), "--output", str(tmp_path / "out.jsonl")])


def test_private_and_undefined_fields_never_reach_the_output(tmp_path: Path) -> None:
    output = (full(tmp_path) and (tmp_path / "converted.jsonl").read_text())
    assert "SYNTHETIC-PRIVATE" not in output
    for key in ("title", "amount", "paid_by", "body"):
        assert f'"{key}"' not in output


def test_malformed_and_unattributed_lines_are_skipped_with_reasons(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    full(tmp_path)
    report = json.loads(capsys.readouterr().err)
    assert report["skipped"] == {
        "error_missing_required_fields": 1,  # an error without reported_by is never guessed
        "log_line_bad_timestamp": 1,
        "log_line_not_json": 1,
        "metric_datapoint_invalid": 1,
        "metric_result_without_query": 1,
        "metric_without_instance_dimension": 1,
        "unknown_event_type": 1,
    }
    assert report["converted"] == {"app.error": 2, "app.request_summary": 2, "aws.cloudwatch.metric": 3, "host.heartbeat": 2}


def test_cloudwatch_logs_export_keeps_event_ids_and_streams(tmp_path: Path) -> None:
    records = run(tmp_path, "--logs-json", str(EXPORT / "logs_cli.json"), "--log-group", "/splitwise/app")
    [error] = of(records, "app.error")
    assert error["event_id"] == "37950117284512345678901234567890"
    assert (error["source"], error["log_group"], error["log_stream"]) == ("aws.cloudwatch.logs", "/splitwise/app", "i-0synthetic01")
    [summary] = of(records, "app.request_summary")
    assert summary["error_5xx_count"] == 1 and summary["log_group"] == "/splitwise/app"


def test_invalid_arguments_are_rejected(tmp_path: Path) -> None:
    out = str(tmp_path / "o.jsonl")
    with pytest.raises(SystemExit, match="must be given together"):
        main(["--metric-data", str(EXPORT / "metric_data.json"), "--output", out])
    with pytest.raises(SystemExit, match="timezone"):
        main(["--app-log", str(EXPORT / "app_log.jsonl"), "--start", "2026-10-30T10:00:00", "--end", "2026-10-30T11:00:00Z", "--output", out])
    with pytest.raises(SystemExit, match="Provide"):
        main(["--output", out])


def test_converted_export_flows_through_the_pipeline_as_one_cross_layer_incident(tmp_path: Path, scripted_jcode: Any, agents_for: Any) -> None:
    full(tmp_path)
    content = (tmp_path / "converted.jsonl").read_bytes()
    transport = scripted_jcode()
    result = RcaPipeline(lambda _: ("converted.jsonl", content), lambda: agents_for(transport), ROUTING)({"dataset_id": "d"})
    [incident] = [payload for kind, payload in result.artifacts if kind.value == "incident"]
    assert sorted(incident["affected_layers"]) == ["application", "database", "vm_guest_os"]
    edges = {(item["source"]["component_id"], item["relationship_type"], item["target"]["component_id"]) for item in incident["topology"]["relationships"]}
    assert {("splitwise-api", "runs_on", "i-0synthetic01"), ("splitwise-sqlite", "runs_on", "i-0synthetic01"), ("splitwise-api", "depends_on", "splitwise-sqlite")} <= edges
    assert len(transport.prompts["main_rca"]) == 1
