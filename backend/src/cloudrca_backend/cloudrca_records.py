"""CloudRCA record types for application, database, VM/OS, and EC2 CloudWatch evidence.

Records are identified by ``cloudrca_record`` and normalized without inferring causes: the layer comes from the
record type and, for errors, from the emitter's own ``reported_by`` statement. Identifiers that later stages need
to relate records across layers (instance, service, database, request IDs, metric dimensions) are preserved.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

from .contracts import Layer, NormalizedEvent, Provenance, ProvenanceKind, Severity

RECORD_FIELD = "cloudrca_record"
Text = Annotated[str, Field(min_length=1, max_length=256)]
Count = Annotated[int, Field(ge=0)]
Measure = Annotated[float, Field(ge=0)]


class _Record(BaseModel):
    """Fields shared by every CloudRCA record; unknown fields are kept in the raw provenance record."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=False)
    cloudrca_record: str
    timestamp: str = Field(min_length=1)
    source: Text
    event_id: Text | None = None
    level: str | None = None
    log_group: Text | None = None
    log_stream: Text | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_booleans(cls, data: Any) -> Any:
        # No record field is boolean; without this, lax parsing would turn true/false into 1/0 measurements.
        if isinstance(data, Mapping) and any(isinstance(value, bool) for value in data.values()):
            raise ValueError("boolean values are not valid in CloudRCA records")
        return data


class RequestSummary(_Record):
    """Application request outcomes aggregated over a short window (counts, not individual requests)."""

    cloudrca_record: Literal["app.request_summary"]
    service: Text
    instance_id: Text
    window_seconds: Annotated[int, Field(gt=0)]
    request_count: Count
    error_5xx_count: Count | None = None
    write_count: Count | None = None
    p95_duration_ms: Measure | None = None
    max_duration_ms: Measure | None = None
    max_db_ms: Measure | None = None
    max_in_flight: Count | None = None
    route: Text | None = None

    @model_validator(mode="after")
    def subsets_cannot_exceed_total(self) -> RequestSummary:
        for name in ("error_5xx_count", "write_count"):
            value = getattr(self, name)
            if value is not None and value > self.request_count:
                raise ValueError(f"{name} cannot exceed request_count")
        return self


class ErrorEvent(_Record):
    """One error as reported by its emitter; ``reported_by`` names the component that raised it, not its cause."""

    cloudrca_record: Literal["app.error"]
    reported_by: Literal["application", "database"]
    service: Text
    instance_id: Text
    error_class: Text
    message: str = Field(min_length=1, max_length=2_000)
    database_id: Text | None = None
    request_id: Text | None = None
    route: Text | None = None
    method: Text | None = None
    status: Annotated[int, Field(ge=100, le=599)] | None = None
    operation: Text | None = None
    duration_ms: Measure | None = None
    db_wait_ms: Measure | None = None

    @model_validator(mode="after")
    def database_errors_name_the_database(self) -> ErrorEvent:
        if self.reported_by == "database" and not self.database_id:
            raise ValueError("database-reported errors require database_id")
        return self


HEARTBEAT_MEASURES = (
    "load_1m", "load_5m", "cpu_count", "mem_available_mb", "mem_total_mb", "disk_free_mb", "disk_total_mb",
    "db_pool_size", "db_pool_checked_out", "db_pool_overflow",
)


class Heartbeat(_Record):
    """A periodic VM/OS resource snapshot recorded on the instance."""

    cloudrca_record: Literal["host.heartbeat"]
    instance_id: Text
    load_1m: Measure | None = None
    load_5m: Measure | None = None
    cpu_count: Annotated[int, Field(gt=0)] | None = None
    mem_available_mb: Measure | None = None
    mem_total_mb: Measure | None = None
    disk_free_mb: Measure | None = None
    disk_total_mb: Measure | None = None
    db_pool_size: Count | None = None
    db_pool_checked_out: Count | None = None
    db_pool_overflow: Count | None = None

    @model_validator(mode="after")
    def needs_a_measurement(self) -> Heartbeat:
        if all(getattr(self, name) is None for name in HEARTBEAT_MEASURES):
            raise ValueError("heartbeat has no measurement")
        return self


class CloudWatchMetric(_Record):
    """One EC2 CloudWatch metric datapoint; the instance comes from the InstanceId dimension."""

    cloudrca_record: Literal["aws.cloudwatch.metric"]
    namespace: Text
    metric_name: Text
    value: float
    dimensions: dict[Text, Text]
    statistic: Text | None = None
    period_seconds: Annotated[int, Field(gt=0)] | None = None
    unit: Text | None = None

    @model_validator(mode="after")
    def needs_instance_dimension(self) -> CloudWatchMetric:
        if not self.dimensions.get("InstanceId"):
            raise ValueError("metric dimensions require InstanceId")
        return self


RECORD_MODELS: dict[str, type[_Record]] = {
    "app.request_summary": RequestSummary,
    "app.error": ErrorEvent,
    "host.heartbeat": Heartbeat,
    "aws.cloudwatch.metric": CloudWatchMetric,
}


def _measure_text(record: BaseModel, names: tuple[str, ...]) -> str:
    return ", ".join(f"{name}={getattr(record, name):g}" for name in names if getattr(record, name) is not None)


def _attributes(record: BaseModel, names: tuple[str, ...]) -> dict[str, JsonValue]:
    return {name: getattr(record, name) for name in names if getattr(record, name) is not None}


def _request_summary(record: RequestSummary) -> tuple[Layer, str, dict[str, Any], dict[str, JsonValue]]:
    measures = ("request_count", "error_5xx_count", "write_count", "p95_duration_ms", "max_duration_ms", "max_db_ms", "max_in_flight")
    message = f"{record.service} requests over {record.window_seconds}s: {_measure_text(record, measures)}"
    ids = {"service": record.service, "vm_id": record.instance_id}
    return Layer.APPLICATION, message, ids, _attributes(record, ("window_seconds", "route", *measures))


def _error(record: ErrorEvent) -> tuple[Layer, str, dict[str, Any], dict[str, JsonValue]]:
    attributes = _attributes(record, ("reported_by", "error_class", "request_id", "route", "method", "status", "operation", "duration_ms", "db_wait_ms"))
    message = f"{record.error_class}: {record.message}"
    if record.reported_by == "database":
        # The application that raised the database error is kept as an attribute, not as this event's service,
        # so it is never mistaken for a database-layer component.
        attributes["reporting_service"] = record.service
        return Layer.DATABASE, message, {"database_id": record.database_id, "vm_id": record.instance_id}, attributes
    if record.database_id:
        attributes["database_id_context"] = record.database_id
    return Layer.APPLICATION, message, {"service": record.service, "vm_id": record.instance_id}, attributes


def _heartbeat(record: Heartbeat) -> tuple[Layer, str, dict[str, Any], dict[str, JsonValue]]:
    return Layer.VM, f"host heartbeat: {_measure_text(record, HEARTBEAT_MEASURES)}", {"vm_id": record.instance_id}, _attributes(record, HEARTBEAT_MEASURES)


def _metric(record: CloudWatchMetric) -> tuple[Layer, str, dict[str, Any], dict[str, JsonValue]]:
    statistic = f" {record.statistic}" if record.statistic else ""
    unit = f" {record.unit}" if record.unit else ""
    period = f" (period {record.period_seconds}s)" if record.period_seconds else ""
    message = f"{record.namespace} {record.metric_name}{statistic}={record.value:g}{unit}{period}"
    attributes: dict[str, JsonValue] = {
        "namespace": record.namespace, "metric_name": record.metric_name, "value": record.value,
        "dimensions": dict(sorted(record.dimensions.items())), **_attributes(record, ("statistic", "period_seconds", "unit")),
    }
    return Layer.VM, message, {"vm_id": record.dimensions["InstanceId"]}, attributes


BUILDERS: dict[str, Callable[[Any], tuple[Layer, str, dict[str, Any], dict[str, JsonValue]]]] = {
    "app.request_summary": _request_summary,
    "app.error": _error,
    "host.heartbeat": _heartbeat,
    "aws.cloudwatch.metric": _metric,
}


def normalize_cloudrca_record(
    data: Mapping[str, Any],
    *,
    record_location: tuple[str, int, str],
    raw_record: dict[str, JsonValue],
    parse_timestamp: Callable[[Any], Any],
    stable_json: Callable[[Mapping[str, Any]], str],
    error_factory: Callable[[str, str], Exception],
) -> tuple[NormalizedEvent, str]:
    """Validate and normalize one CloudRCA record; failures raise via ``error_factory(reason, detail)``."""
    kind = data.get(RECORD_FIELD)
    model = RECORD_MODELS.get(kind) if isinstance(kind, str) else None
    if model is None:
        raise error_factory("UNSUPPORTED_RECORD", f"unsupported cloudrca_record: {kind!r}")
    try:
        record = model.model_validate(dict(data))
    except ValidationError as exc:
        missing = any(item["type"] == "missing" for item in exc.errors())
        fields = ", ".join(".".join(str(part) for part in item["loc"]) or "record" for item in exc.errors())
        raise error_factory("MISSING_REQUIRED_FIELD" if missing else "SCHEMA_VALIDATION_FAILED", f"invalid {kind}: {fields}") from exc
    timestamp = parse_timestamp(record.timestamp)
    layer, message, ids, attributes = BUILDERS[record.cloudrca_record](record)
    severity = Severity.UNKNOWN
    if record.level:
        mapped = {"warn": "warning", "fatal": "critical", "trace": "debug"}.get(record.level.lower(), record.level.lower())
        try:
            severity = Severity(mapped)
        except ValueError as exc:
            raise error_factory("INVALID_SEVERITY", f"unsupported severity: {record.level}") from exc
    cloudwatch = _attributes(record, ("log_group", "log_stream"))
    logical = {
        "record": record.cloudrca_record, "event_id": record.event_id, "timestamp": timestamp.isoformat(),
        "source": record.source, "layer": layer.value, "severity": severity.value, "message": message,
        **{key: value for key, value in ids.items() if value is not None}, "source_attributes": attributes,
    }
    fingerprint = hashlib.sha256(stable_json(logical).encode("utf-8")).hexdigest()
    source_file, row_number, source_format = record_location
    event = NormalizedEvent(
        event_id=f"event-{fingerprint}",
        timestamp=timestamp,
        source=record.source,
        layer=layer,
        severity=severity,
        message=message,
        metadata={
            "record_kind": record.cloudrca_record,
            "original_timestamp": record.timestamp,
            "original_severity": record.level,
            "fingerprint": fingerprint,
            "source_attributes": attributes,
            "cloudwatch": cloudwatch or None,
        },
        provenance=Provenance(
            kind=ProvenanceKind.OBSERVED,
            source=record.source,
            record_id=record.event_id or f"{source_file}:{row_number}",
            details={
                "source_file": source_file, "row_number": row_number, "source_format": source_format,
                "event_id": record.event_id, "raw_record": raw_record,
            },
        ),
        **ids,
    )
    return event, fingerprint
