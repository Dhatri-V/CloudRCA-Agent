#!/usr/bin/env python3
"""Convert Splitwise application logs and EC2 CloudWatch metrics into CloudRCA records (standard library only).

Inputs (combine only sources that do not cover the same events; overlapping inputs would duplicate errors and counts):
  --app-log FILE          Splitwise JSON-lines log pulled from the instance (event: request | error | heartbeat)
  --logs-json FILE        output of `aws logs filter-log-events --output json` whose messages are those JSON lines
  --metric-data FILE      output of `aws cloudwatch get-metric-data --output json`
  --metric-queries FILE   the --metric-data-queries JSON used for that call (maps result IDs to metric/dimensions)

Error events are always kept one-to-one; only ordinary request lines are summarized per window. Only fields defined
by the CloudRCA record types are copied, so anything else in the logs (bodies, names, amounts) never reaches the output.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ERROR_FIELDS = ("request_id", "route", "method", "status", "operation", "duration_ms", "db_wait_ms", "database_id")
HEARTBEAT_FIELDS = ("load_1m", "load_5m", "cpu_count", "mem_available_mb", "mem_total_mb", "disk_free_mb", "disk_total_mb")
MAX_MESSAGE = 2_000


@dataclass
class Report:
    """What was converted, skipped, and why; printed to stderr, never mixed into the dataset."""

    converted: Counter[str] = field(default_factory=Counter)
    skipped: Counter[str] = field(default_factory=Counter)
    window: tuple[str, str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"converted": dict(sorted(self.converted.items())), "skipped": dict(sorted(self.skipped.items())), "window": self.window}


@dataclass(frozen=True)
class LogLine:
    data: dict[str, Any]
    timestamp: datetime
    source: str
    event_id: str
    log_group: str | None = None
    log_stream: str | None = None


def parse_time(value: Any) -> datetime | None:
    """Timezone-aware ISO 8601 only; naive or unparseable timestamps are rejected, never guessed."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def read_app_log(path: Path, report: Report) -> Iterator[LogLine]:
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            yield from _log_line(line, f"{path.name}:{number}", "splitwise.applog", report)


def read_logs_json(path: Path, log_group: str | None, report: Report) -> Iterator[LogLine]:
    try:
        events = json.loads(path.read_text(encoding="utf-8")).get("events", [])
    except (json.JSONDecodeError, AttributeError):
        report.skipped["logs_json_unreadable"] += 1
        return
    for index, event in enumerate(events):
        if not isinstance(event, dict) or not isinstance(event.get("message"), str):
            report.skipped["cloudwatch_event_without_message"] += 1
            continue
        event_id = str(event.get("eventId") or f"{path.name}:{index}")
        yield from _log_line(event["message"], event_id, "aws.cloudwatch.logs", report, log_group, event.get("logStreamName"))


def _log_line(text: str, event_id: str, source: str, report: Report, log_group: str | None = None, log_stream: str | None = None) -> Iterator[LogLine]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        report.skipped["log_line_not_json"] += 1
        return
    if not isinstance(data, dict):
        report.skipped["log_line_not_object"] += 1
        return
    timestamp = parse_time(data.get("ts"))
    if timestamp is None:
        report.skipped["log_line_bad_timestamp"] += 1
        return
    yield LogLine(data, timestamp, source, event_id, log_group, log_stream if isinstance(log_stream, str) else None)


def _valid_error(data: dict[str, Any]) -> bool:
    required = (data.get(name) for name in ("service", "instance_id", "error_class", "message"))
    return all(isinstance(value, str) and value for value in required) and data.get("reported_by") in {"application", "database"}


def _is_error(line: LogLine) -> bool:
    """Window anchors: errors that will be converted, and 5xx request outcomes; malformed errors never stretch the window."""
    status = line.data.get("status")
    if line.data.get("event") == "error":
        return _valid_error(line.data)
    return line.data.get("event") == "request" and isinstance(status, int) and not isinstance(status, bool) and status >= 500


def select_window(lines: list[LogLine], start: datetime | None, end: datetime | None, before: timedelta, after: timedelta) -> tuple[datetime, datetime]:
    if start and end:
        return start, end
    errors = [line.timestamp for line in lines if _is_error(line)]
    if not errors:
        raise SystemExit("No error events found; pass --start and --end to choose a window explicitly.")
    return (start or min(errors) - before), (end or max(errors) + after)


def _common(line: LogLine, kind: str) -> dict[str, Any]:
    record: dict[str, Any] = {"cloudrca_record": kind, "timestamp": iso(line.timestamp), "source": line.source, "event_id": line.event_id}
    if line.log_group:
        record["log_group"] = line.log_group
    if line.log_stream:
        record["log_stream"] = line.log_stream
    return record


def error_record(line: LogLine, report: Report) -> dict[str, Any] | None:
    data = line.data
    # reported_by is stated by the logger and never defaulted, so database errors are not misattributed.
    if not _valid_error(data):
        report.skipped["error_missing_required_fields"] += 1
        return None
    required = {name: data[name] for name in ("service", "instance_id", "error_class", "message")}
    record = {**_common(line, "app.error"), "level": "error", "reported_by": data["reported_by"], **required}
    record["message"] = str(required["message"])[:MAX_MESSAGE]
    for name in ERROR_FIELDS:
        value = data.get(name)
        if value is None:
            continue
        if isinstance(value, str) or _number(value) is not None:
            record[name] = value
        else:
            report.skipped["error_field_not_scalar"] += 1  # nested or boolean values would invalidate the record
    return record


def heartbeat_record(line: LogLine, report: Report) -> dict[str, Any] | None:
    measures = {name: value for name in HEARTBEAT_FIELDS if (value := _number(line.data.get(name))) is not None}
    instance = line.data.get("instance_id")
    if not isinstance(instance, str) or not instance or not measures:
        report.skipped["heartbeat_missing_required_fields"] += 1
        return None
    if "cpu_count" in measures:
        measures["cpu_count"] = int(measures["cpu_count"])
    return {**_common(line, "host.heartbeat"), "instance_id": instance, **measures}


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def request_summaries(lines: Iterable[LogLine], window_seconds: int, report: Report) -> list[dict[str, Any]]:
    """Per (service, instance, window) counts and maxima; error lines are summarized here AND kept as app.error."""
    buckets: dict[tuple[str, str, int], list[LogLine]] = {}
    for line in lines:
        service, instance = line.data.get("service"), line.data.get("instance_id")
        if not (isinstance(service, str) and service and isinstance(instance, str) and instance):
            report.skipped["request_missing_service_or_instance"] += 1
            continue
        start = int(line.timestamp.timestamp()) // window_seconds * window_seconds
        buckets.setdefault((service, instance, start), []).append(line)
    summaries = []
    for (service, instance, start), members in sorted(buckets.items()):
        durations = [value for line in members if (value := _number(line.data.get("duration_ms"))) is not None]
        db_times = [value for line in members if (value := _number(line.data.get("db_ms"))) is not None]
        in_flight = [int(value) for line in members if (value := _number(line.data.get("in_flight"))) is not None]
        errors_5xx = sum(isinstance(line.data.get("status"), int) and line.data["status"] >= 500 for line in members)
        summary: dict[str, Any] = {
            "cloudrca_record": "app.request_summary",
            "timestamp": iso(datetime.fromtimestamp(start, UTC)),
            "source": members[0].source,
            "event_id": f"summary:{service}:{instance}:{start}:{window_seconds}",
            "level": "warning" if errors_5xx else "info",
            "service": service,
            "instance_id": instance,
            "window_seconds": window_seconds,
            "request_count": len(members),
            "error_5xx_count": errors_5xx,
            "write_count": sum(line.data.get("is_write") is True for line in members),
        }
        if durations:
            summary.update(p95_duration_ms=_p95(durations), max_duration_ms=max(durations))
        if db_times:
            summary["max_db_ms"] = max(db_times)
        if in_flight:
            summary["max_in_flight"] = max(in_flight)
        if members[0].log_group:
            summary["log_group"] = members[0].log_group
        summaries.append(summary)
    return summaries


def metric_records(data_path: Path, queries_path: Path, window: tuple[datetime, datetime] | None, report: Report) -> list[dict[str, Any]]:
    try:
        results = json.loads(data_path.read_text(encoding="utf-8")).get("MetricDataResults", [])
        queries = json.loads(queries_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, AttributeError):
        report.skipped["metric_files_unreadable"] += 1
        return []
    by_id = {query.get("Id"): query.get("MetricStat") for query in queries if isinstance(query, dict)}
    records = []
    for result in results:
        stat = by_id.get(result.get("Id"))
        if not isinstance(stat, dict) or not isinstance(stat.get("Metric"), dict):
            report.skipped["metric_result_without_query"] += 1
            continue
        metric = stat["Metric"]
        dimensions = {
            item["Name"]: item["Value"]
            for item in metric.get("Dimensions", [])
            if isinstance(item, dict) and isinstance(item.get("Name"), str) and isinstance(item.get("Value"), str)
        }
        if not dimensions.get("InstanceId"):
            report.skipped["metric_without_instance_dimension"] += len(result.get("Values", []))
            continue
        for timestamp_text, value in zip(result.get("Timestamps", []), result.get("Values", []), strict=False):
            timestamp, number = parse_time(timestamp_text), _number(value)
            if timestamp is None or number is None:
                report.skipped["metric_datapoint_invalid"] += 1
                continue
            if window and not window[0] <= timestamp <= window[1]:
                continue
            record = {
                "cloudrca_record": "aws.cloudwatch.metric",
                "timestamp": iso(timestamp),
                "source": "aws.cloudwatch.metrics",
                "event_id": f"metric:{result.get('Id')}:{iso(timestamp)}",
                "namespace": metric.get("Namespace"),
                "metric_name": metric.get("MetricName"),
                "value": number,
                "dimensions": dict(sorted(dimensions.items())),
                "statistic": stat.get("Stat"),
                "period_seconds": stat.get("Period"),
                "unit": stat.get("Unit"),
            }
            records.append({key: value for key, value in record.items() if value is not None})
    return records


def convert(args: argparse.Namespace) -> tuple[list[dict[str, Any]], Report]:
    report = Report()
    lines: list[LogLine] = []
    if args.app_log:
        lines.extend(read_app_log(args.app_log, report))
    if args.logs_json:
        lines.extend(read_logs_json(args.logs_json, args.log_group, report))
    if bool(args.metric_data) != bool(args.metric_queries):
        raise SystemExit("--metric-data and --metric-queries must be given together.")
    start, end = parse_time(args.start) if args.start else None, parse_time(args.end) if args.end else None
    if (args.start and start is None) or (args.end and end is None):
        raise SystemExit("--start/--end must be ISO 8601 timestamps with a timezone, e.g. 2026-10-30T10:00:00Z.")
    window = None
    if lines:
        window = select_window(lines, start, end, timedelta(minutes=args.before_minutes), timedelta(minutes=args.after_minutes))
    elif start and end:
        window = (start, end)
    elif args.metric_data:
        raise SystemExit("Metrics-only conversion needs --start and --end; without logs there are no errors to anchor a window.")
    if window:
        report.window = (iso(window[0]), iso(window[1]))
        lines = [line for line in lines if window[0] <= line.timestamp <= window[1]]

    records: list[dict[str, Any]] = []
    requests: list[LogLine] = []
    for line in lines:
        kind = line.data.get("event")
        if kind == "error":
            if record := error_record(line, report):
                records.append(record)
        elif kind == "heartbeat":
            if record := heartbeat_record(line, report):
                records.append(record)
        elif kind == "request":
            requests.append(line)
        else:
            report.skipped["unknown_event_type"] += 1
    records.extend(request_summaries(requests, args.window_seconds, report))
    if args.metric_data:
        records.extend(metric_records(args.metric_data, args.metric_queries, window, report))
    records.sort(key=lambda record: (record["timestamp"], record["event_id"]))
    report.converted.update(record["cloudrca_record"] for record in records)
    return records, report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--app-log", type=Path)
    parser.add_argument("--logs-json", type=Path)
    parser.add_argument("--log-group")
    parser.add_argument("--metric-data", type=Path)
    parser.add_argument("--metric-queries", type=Path)
    parser.add_argument("--start", help="window start (ISO 8601 with timezone); default: 15 min before the first error")
    parser.add_argument("--end", help="window end (ISO 8601 with timezone); default: 5 min after the last error")
    parser.add_argument("--before-minutes", type=float, default=15.0)
    parser.add_argument("--after-minutes", type=float, default=5.0)
    parser.add_argument("--window-seconds", type=int, default=60, help="request summary window")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not (args.app_log or args.logs_json or args.metric_data):
        raise SystemExit("Provide --app-log, --logs-json, and/or --metric-data with --metric-queries.")
    if args.window_seconds <= 0:
        raise SystemExit("--window-seconds must be positive.")
    records, report = convert(args)
    if not records:
        print(json.dumps(report.as_dict(), indent=2), file=sys.stderr)
        raise SystemExit("No records converted.")
    args.output.write_text("\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n", encoding="utf-8")
    print(json.dumps(report.as_dict(), indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
