"""Deterministic, read-only profiling for AIOps2025 validation data."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json

from charset_normalizer import from_bytes
import polars as pl

SUPPORTED_SUFFIXES = {".parquet", ".json", ".jsonl"}
IDENTIFIER_COLUMNS = (
    "instance",
    "pod",
    "object_id",
    "object_type",
    "k8_node_name",
    "k8_pod",
    "k8_namespace",
    "traceID",
    "spanID",
)
TIMESTAMP_COLUMNS = ("time", "@timestamp", "startTimeMillis", "start_time", "end_time")
MISSING_STRINGS = ["", "null", "none", "nan"]


def _json_safe(value: Any) -> Any:
    """Convert timestamp values into deterministic UTC JSON strings."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value


def _text_encoding(path: Path) -> dict[str, Any]:
    """Detect a text file's encoding and return serializable metadata."""
    match = from_bytes(path.read_bytes()).best()
    if match is None:
        return {"encoding": None, "encoding_confidence": 0.0}
    return {
        "encoding": match.encoding,
        "encoding_confidence": round(1.0 - match.percent_chaos / 100.0, 6),
    }


def _read_frame(path: Path) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Read a supported source while normalizing heterogeneous JSON values."""
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        return pl.read_parquet(path), {"encoding": "binary/parquet", "encoding_confidence": 1.0}
    encoding = _text_encoding(path)
    text = path.read_text(encoding=encoding["encoding"] or "utf-8")
    if suffix == ".jsonl":
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        parsed = json.loads(text)
        records = parsed if isinstance(parsed, list) else [parsed]
    keys = sorted({key for record in records for key in record})
    normalized: list[dict[str, Any]] = []
    complex_keys = {
        key
        for key in keys
        if any(isinstance(record.get(key), (dict, list)) for record in records)
    }
    for record in records:
        row: dict[str, Any] = {}
        for key in keys:
            value = record.get(key)
            if key in complex_keys:
                row[key] = None if value is None else (
                    value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
                )
            else:
                row[key] = value
        normalized.append(row)
    return pl.from_dicts(normalized, infer_schema_length=None), encoding


def _timestamp_profile(frame: pl.DataFrame) -> dict[str, Any]:
    """Summarize recognized timestamp columns and their timezone metadata."""
    result: dict[str, Any] = {}
    for column in TIMESTAMP_COLUMNS:
        if column not in frame.columns:
            continue
        series = frame[column].drop_nulls()
        if series.is_empty():
            result[column] = {"non_null": 0, "minimum": None, "maximum": None, "timezone": None}
            continue
        dtype = frame.schema[column]
        timezone_name = getattr(dtype, "time_zone", None)
        if isinstance(dtype, pl.Datetime):
            minimum, maximum = series.min(), series.max()
        elif column == "startTimeMillis" and dtype.is_integer():
            converted = series.cast(pl.Datetime("ms", "UTC"))
            minimum, maximum, timezone_name = converted.min(), converted.max(), "UTC"
        else:
            minimum, maximum = series.min(), series.max()
            if isinstance(minimum, str):
                timezone_name = "UTC" if minimum.endswith("Z") else "not encoded"
        result[column] = {
            "non_null": series.len(),
            "minimum": _json_safe(minimum),
            "maximum": _json_safe(maximum),
            "timezone": timezone_name,
        }
    return result


def profile_file(path: str | Path, *, relative_to: str | Path | None = None) -> dict[str, Any]:
    """Profile one supported file without modifying it."""
    source = Path(path)
    display = source.relative_to(relative_to) if relative_to else source
    base: dict[str, Any] = {
        "path": display.as_posix(),
        "format": source.suffix.lower().removeprefix("."),
        "size_bytes": source.stat().st_size,
        "malformed_records": 0,
    }
    try:
        frame, encoding = _read_frame(source)
    except Exception as exc:  # malformed files must be reported, not hidden
        return {**base, "read_error": f"{type(exc).__name__}: {exc}", "malformed_records": 1}

    nulls = frame.null_count().row(0) if frame.width else ()
    missing_counts: dict[str, int] = {}
    for column, dtype in frame.schema.items():
        physical_nulls = frame[column].null_count()
        sentinel_nulls = 0
        if dtype == pl.String:
            sentinel_nulls = frame.select(
                pl.col(column).fill_null("").str.strip_chars().str.to_lowercase().is_in(MISSING_STRINGS).sum()
            ).item() - physical_nulls
        missing_counts[column] = int(physical_nulls + max(sentinel_nulls, 0))
    identifiers: dict[str, Any] = {}
    for column in IDENTIFIER_COLUMNS:
        if column in frame.columns:
            values = frame[column].drop_nulls()
            if frame.schema[column] == pl.String:
                values = values.filter(~values.str.strip_chars().str.to_lowercase().is_in(MISSING_STRINGS))
            identifiers[column] = {
                "non_null": values.len(),
                "distinct": values.n_unique(),
            }
    return {
        **base,
        **encoding,
        "record_count": frame.height,
        "column_count": frame.width,
        "schema": {name: str(dtype) for name, dtype in frame.schema.items()},
        "null_counts": dict(zip(frame.columns, (int(value) for value in nulls))),
        "missing_counts": missing_counts,
        "duplicate_records": int(frame.is_duplicated().sum()) if frame.width else 0,
        "timestamps": _timestamp_profile(frame),
        "identifier_coverage": identifiers,
    }


def profile_directory(path: str | Path) -> dict[str, Any]:
    """Profile every supported file below a directory in lexical order."""
    root = Path(path)
    if not root.is_dir():
        raise NotADirectoryError(f"Dataset source is not a directory: {root}")
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES)
    profiles = [profile_file(file, relative_to=root) for file in files]
    return {
        "profile_version": 1,
        "source_root": root.name,
        "file_count": len(profiles),
        "total_size_bytes": sum(item["size_bytes"] for item in profiles),
        "total_records": sum(item.get("record_count", 0) for item in profiles),
        "total_duplicate_records": sum(item.get("duplicate_records", 0) for item in profiles),
        "malformed_files": sum(1 for item in profiles if item.get("read_error")),
        "formats": sorted({item["format"] for item in profiles}),
        "files": profiles,
    }
