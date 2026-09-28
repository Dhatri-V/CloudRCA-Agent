"""AIOps2025 parsing and normalization into canonical CloudRCA events."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import polars as pl
from charset_normalizer import from_bytes
from dateutil import parser as date_parser
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .contracts import Layer, NormalizedEvent, Provenance, ProvenanceKind, Severity

MISSING_STRINGS = frozenset({"", "null", "none", "nan"})
DATABASE_COMPONENTS = frozenset({"redis", "tidb", "tikv", "pd"})
DIMENSION_FIELDS = frozenset(
    {
        "time", "@timestamp", "cf", "device", "instance", "kpi_key", "kpi_name",
        "kubernetes_node", "mountpoint", "namespace", "object_type", "pod", "sql_type", "type",
    }
)


class RejectionReason(StrEnum):
    INVALID_ENCODING = "INVALID_ENCODING"
    MALFORMED_RECORD = "MALFORMED_RECORD"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    AMBIGUOUS_TIMEZONE = "AMBIGUOUS_TIMEZONE"
    NONEXISTENT_LOCAL_TIME = "NONEXISTENT_LOCAL_TIME"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    INVALID_SEVERITY = "INVALID_SEVERITY"
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
    UNSUPPORTED_RECORD = "UNSUPPORTED_RECORD"


class NormalizationError(ValueError):
    """Expected record-level failure that belongs in quarantine."""

    def __init__(self, reason: RejectionReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class SourceRecord(BaseModel):
    """One source row plus its stable location in a dataset file."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    source_file: str
    row_number: Annotated[int, Field(ge=1)]
    source_format: str
    data: dict[str, Any]
    parse_error: str | None = None
    parse_reason: RejectionReason | None = None


class RejectedRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source_file: str
    row_number: int
    reason: RejectionReason
    detail: str
    raw_record: dict[str, JsonValue]


class DuplicateRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source_file: str
    row_number: int
    fingerprint: str
    raw_record: dict[str, JsonValue]


class BatchStatistics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    input: Annotated[int, Field(ge=0)]
    accepted: Annotated[int, Field(ge=0)]
    duplicates: Annotated[int, Field(ge=0)]
    rejected: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def counts_must_reconcile(self) -> BatchStatistics:
        if self.input != self.accepted + self.duplicates + self.rejected:
            raise ValueError("input must equal accepted + duplicates + rejected")
        return self


class NormalizationBatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    events: tuple[NormalizedEvent, ...]
    duplicates: tuple[DuplicateRecord, ...]
    rejected: tuple[RejectedRecord, ...]
    statistics: BatchStatistics


def _json_safe(value: Any) -> JsonValue:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _safe_record(data: Mapping[str, Any]) -> dict[str, JsonValue]:
    return {str(key): _json_safe(value) for key, value in data.items()}


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).split())
    return None if cleaned.lower() in MISSING_STRINGS else cleaned


def _stable_json(value: Mapping[str, Any]) -> str:
    return json.dumps(_safe_record(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _source_record(
    display: str,
    row_number: int,
    source_format: str,
    data: dict[str, Any],
    parse_error: str | None = None,
    parse_reason: RejectionReason | None = None,
) -> SourceRecord:
    return SourceRecord(
        source_file=display,
        row_number=row_number,
        source_format=source_format,
        data=data,
        parse_error=parse_error,
        parse_reason=parse_reason,
    )


def read_aiops2025_records(path: str | Path, *, relative_to: str | Path | None = None) -> tuple[SourceRecord, ...]:
    """Read confirmed AIOps2025 Parquet, JSON, or JSONL without hiding bad input."""
    source = Path(path)
    display = source.relative_to(relative_to).as_posix() if relative_to else source.as_posix()
    suffix = source.suffix.lower()
    if suffix == ".parquet":
        try:
            rows = pl.read_parquet(source).to_dicts()
        except Exception as exc:
            return (_source_record(display, 1, "parquet", {}, str(exc), RejectionReason.MALFORMED_RECORD),)
        return tuple(_source_record(display, index, "parquet", row) for index, row in enumerate(rows, start=1))
    if suffix not in {".json", ".jsonl"}:
        return (
            _source_record(
                display,
                1,
                suffix.removeprefix(".") or "unknown",
                {},
                "unsupported file format",
                RejectionReason.UNSUPPORTED_RECORD,
            ),
        )
    raw_bytes = source.read_bytes()
    match = from_bytes(raw_bytes).best()
    if match is None:
        return (
            _source_record(
                display, 1, suffix[1:], {}, "unknown encoding", RejectionReason.INVALID_ENCODING
            ),
        )
    text = str(match)
    if suffix == ".jsonl":
        records: list[SourceRecord] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
                if not isinstance(parsed, dict):
                    raise TypeError("record must be a JSON object")
                records.append(_source_record(display, line_number, "jsonl", parsed))
            except (json.JSONDecodeError, TypeError) as exc:
                records.append(
                    _source_record(
                        display,
                        line_number,
                        "jsonl",
                        {"raw_text": line},
                        str(exc),
                        RejectionReason.MALFORMED_RECORD,
                    )
                )
        return tuple(records)
    try:
        parsed_json = json.loads(text)
    except json.JSONDecodeError as exc:
        return (
            _source_record(
                display,
                1,
                "json",
                {"raw_text": text},
                str(exc),
                RejectionReason.MALFORMED_RECORD,
            ),
        )
    items = parsed_json if isinstance(parsed_json, list) else [parsed_json]
    return tuple(
        _source_record(
            display,
            index,
            "json",
            item if isinstance(item, dict) else {"raw_value": item},
            None if isinstance(item, dict) else "record must be a JSON object",
            None if isinstance(item, dict) else RejectionReason.MALFORMED_RECORD,
        )
        for index, item in enumerate(items, start=1)
    )


def _localize_naive(value: datetime, timezone_name: str, fold: int | None) -> datetime:
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise NormalizationError(RejectionReason.INVALID_TIMESTAMP, f"unknown timezone: {timezone_name}") from exc
    candidates: list[datetime] = []
    for candidate_fold in (0, 1):
        candidate = value.replace(tzinfo=zone, fold=candidate_fold)
        round_trip = candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        if round_trip == value:
            candidates.append(candidate)
    if not candidates:
        raise NormalizationError(RejectionReason.NONEXISTENT_LOCAL_TIME, "local timestamp does not exist")
    if len(candidates) == 2 and candidates[0].utcoffset() != candidates[1].utcoffset():
        if fold is None:
            raise NormalizationError(RejectionReason.AMBIGUOUS_TIMEZONE, "local timestamp occurs twice; fold is required")
        return candidates[fold]
    return candidates[0]


def parse_utc_timestamp(
    value: Any, *, assumed_timezone: str | None = None, fold: int | None = None, epoch_milliseconds: bool = False
) -> datetime:
    """Parse an authoritative record timestamp and return an aware UTC datetime."""
    try:
        if epoch_milliseconds:
            parsed = datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
        elif isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and value.strip():
            parsed = date_parser.isoparse(value.strip())
        else:
            raise ValueError("timestamp is missing")
    except (ValueError, TypeError, OverflowError) as exc:
        raise NormalizationError(RejectionReason.INVALID_TIMESTAMP, f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if assumed_timezone is None:
            raise NormalizationError(RejectionReason.INVALID_TIMESTAMP, "timestamp has no timezone")
        if fold not in {None, 0, 1}:
            raise NormalizationError(RejectionReason.INVALID_TIMESTAMP, "fold must be 0 or 1")
        parsed = _localize_naive(parsed, assumed_timezone, fold)
    return parsed.astimezone(timezone.utc)


def _component_name(value: str | None) -> str | None:
    if value is None:
        return None
    lowered = value.lower()
    for component in DATABASE_COMPONENTS:
        if lowered == component or lowered.startswith(f"{component}-") or lowered.startswith(f"{component}_"):
            return component
    return None


def _severity(data: Mapping[str, Any]) -> tuple[Severity, str | None]:
    original = next(
        (_clean_text(data.get(key)) for key in ("severity", "level", "log.level") if data.get(key) is not None),
        None,
    )
    if original is None:
        return Severity.UNKNOWN, None
    mapped = {"warn": "warning", "fatal": "critical", "trace": "debug"}.get(original.lower(), original.lower())
    try:
        return Severity(mapped), original
    except ValueError as exc:
        raise NormalizationError(RejectionReason.INVALID_SEVERITY, f"unsupported severity: {original}") from exc


def _trace_service(data: Mapping[str, Any]) -> str | None:
    process = data.get("process")
    return _clean_text(process.get("serviceName")) if isinstance(process, Mapping) else None


def _classify_record(data: Mapping[str, Any]) -> tuple[str, Layer]:
    if "@timestamp" in data or "k8_pod" in data:
        if _component_name(_clean_text(data.get("k8_pod"))) is not None:
            return "log", Layer.DATABASE
        raise NormalizationError(RejectionReason.UNSUPPORTED_RECORD, "log is not from a confirmed infrastructure layer")
    if "startTimeMillis" in data or "traceID" in data:
        if _component_name(_trace_service(data)) is not None:
            return "trace", Layer.DATABASE
        raise NormalizationError(RejectionReason.UNSUPPORTED_RECORD, "trace is not from a confirmed infrastructure layer")
    if "time" in data:
        object_type = _clean_text(data.get("object_type"))
        if object_type == "node":
            return "metric", Layer.VM
        if _component_name(object_type) is not None or _component_name(_clean_text(data.get("pod"))) is not None:
            return "metric", Layer.DATABASE
        raise NormalizationError(RejectionReason.UNSUPPORTED_RECORD, "metric is not from a confirmed infrastructure layer")
    raise NormalizationError(RejectionReason.UNSUPPORTED_RECORD, "record is not a confirmed telemetry shape")


def _metric_message(data: Mapping[str, Any]) -> str:
    values = [
        f"{key}={value}"
        for key, value in sorted(data.items())
        if key not in DIMENSION_FIELDS and isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    if not values:
        raise NormalizationError(RejectionReason.MISSING_REQUIRED_FIELD, "metric has no numeric measurement")
    return ", ".join(values)


def normalize_record(record: SourceRecord, *, assumed_timezone: str | None = None) -> tuple[NormalizedEvent, str]:
    """Normalize one supported source record and return its event and fingerprint."""
    if record.parse_error:
        raise NormalizationError(record.parse_reason or RejectionReason.MALFORMED_RECORD, record.parse_error)
    data = record.data
    record_kind, layer = _classify_record(data)
    timestamp_key = "@timestamp" if record_kind == "log" else "startTimeMillis" if record_kind == "trace" else "time"
    original_timestamp = data.get(timestamp_key)
    timestamp = parse_utc_timestamp(
        original_timestamp,
        assumed_timezone=assumed_timezone,
        epoch_milliseconds=timestamp_key == "startTimeMillis",
    )
    pod_id = _clean_text(data.get("k8_pod") or data.get("pod"))
    object_type = _clean_text(data.get("object_type"))
    instance = _clean_text(data.get("instance"))
    service = _trace_service(data) if record_kind == "trace" else _component_name(pod_id) or object_type
    severity, original_severity = _severity(data)
    if record_kind == "log":
        message = _clean_text(data.get("message"))
        source = _clean_text(data.get("agent_name")) or "aiops2025.log"
    elif record_kind == "trace":
        message = _clean_text(data.get("operationName") or data.get("operation_name") or "trace span")
        source = "aiops2025.trace"
    else:
        message = _metric_message(data)
        source = f"aiops2025.metric.{object_type or 'unknown'}"
    if message is None:
        raise NormalizationError(RejectionReason.MISSING_REQUIRED_FIELD, "message is missing")
    source_attributes = {
        key: _json_safe(value)
        for key in (
            "k8_namespace",
            "namespace",
            "kpi_key",
            "kpi_name",
            "device",
            "mountpoint",
            "sql_type",
            "type",
            "traceID",
            "spanID",
            "duration",
        )
        if (value := data.get(key)) is not None and _clean_text(value) is not None
    }
    vm_id = _clean_text(data.get("k8_node_name") or data.get("kubernetes_node"))
    database_id: str | None = None
    if layer is Layer.VM:
        vm_id = instance
    elif layer is Layer.DATABASE:
        database_id = pod_id or instance or service
    logical = {
        "timestamp": timestamp.isoformat(), "source": source, "service": service, "layer": layer.value,
        "severity": severity.value, "message": message, "vm_id": vm_id, "pod_id": pod_id,
        "database_id": database_id,
        "source_attributes": source_attributes,
    }
    fingerprint = hashlib.sha256(_stable_json(logical).encode("utf-8")).hexdigest()
    raw_record = _safe_record(data)
    try:
        event = NormalizedEvent(
            event_id=f"event-{fingerprint}",
            timestamp=timestamp,
            source=source,
            service=service,
            layer=layer,
            severity=severity,
            message=message,
            vm_id=vm_id,
            pod_id=pod_id,
            database_id=database_id,
            metadata={
                "record_kind": record_kind,
                "original_timestamp": _json_safe(original_timestamp),
                "original_timezone": str(original_timestamp.tzinfo)
                if isinstance(original_timestamp, datetime) and original_timestamp.tzinfo
                else None,
                "original_severity": original_severity,
                "fingerprint": fingerprint,
                "source_attributes": source_attributes,
            },
            provenance=Provenance(
                kind=ProvenanceKind.OBSERVED,
                source="AIOps2025",
                record_id=f"{record.source_file}:{record.row_number}",
                details={
                    "source_file": record.source_file,
                    "row_number": record.row_number,
                    "source_format": record.source_format,
                    "raw_record": raw_record,
                },
            ),
        )
    except Exception as exc:
        raise NormalizationError(RejectionReason.SCHEMA_VALIDATION_FAILED, str(exc)) from exc
    return event, fingerprint


def normalize_batch(
    records: Iterable[SourceRecord],
    *,
    assumed_timezone: str | None = None,
    seen_fingerprints: Iterable[str] = (),
) -> NormalizationBatch:
    """Normalize records deterministically while preserving duplicates and failures."""
    record_list = tuple(records)
    seen = set(seen_fingerprints)
    events: list[NormalizedEvent] = []
    duplicates: list[DuplicateRecord] = []
    rejected: list[RejectedRecord] = []
    for record in record_list:
        raw = _safe_record(record.data)
        try:
            event, fingerprint = normalize_record(record, assumed_timezone=assumed_timezone)
            if fingerprint in seen:
                duplicates.append(
                    DuplicateRecord(
                        source_file=record.source_file,
                        row_number=record.row_number,
                        fingerprint=fingerprint,
                        raw_record=raw,
                    )
                )
                continue
            seen.add(fingerprint)
            events.append(event)
        except NormalizationError as exc:
            rejected.append(
                RejectedRecord(
                    source_file=record.source_file,
                    row_number=record.row_number,
                    reason=exc.reason,
                    detail=exc.detail,
                    raw_record=raw,
                )
            )
    statistics = BatchStatistics(
        input=len(record_list), accepted=len(events), duplicates=len(duplicates), rejected=len(rejected)
    )
    return NormalizationBatch(
        events=tuple(events), duplicates=tuple(duplicates), rejected=tuple(rejected), statistics=statistics
    )


def normalize_file(
    path: str | Path, *, relative_to: str | Path | None = None, assumed_timezone: str | None = None
) -> NormalizationBatch:
    """Read and normalize one confirmed AIOps2025 file."""
    return normalize_batch(
        read_aiops2025_records(path, relative_to=relative_to), assumed_timezone=assumed_timezone
    )
