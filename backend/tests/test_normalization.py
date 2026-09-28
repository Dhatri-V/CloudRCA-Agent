"""Focused tests for AIOps2025 parsing and canonical event normalization."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest
from cloudrca_backend.contracts import NormalizedEvent
from cloudrca_backend.normalization import (
    BatchStatistics,
    NormalizationError,
    RejectionReason,
    SourceRecord,
    normalize_batch,
    normalize_file,
    normalize_record,
    parse_utc_timestamp,
    read_aiops2025_records,
)
from pydantic import ValidationError

FIXTURES = Path(__file__).parents[2] / "tests" / "fixtures" / "aiops2025"
UTC_TIME = datetime(2025, 4, 29, 6, 3, 8, 31_000, tzinfo=timezone.utc)


def source(data: dict[str, object], row: int = 1) -> SourceRecord:
    return SourceRecord(source_file="fixture.parquet", row_number=row, source_format="parquet", data=data)


def redis_log(**changes: object) -> SourceRecord:
    data: dict[str, object] = {
        "@timestamp": UTC_TIME,
        "agent_name": " filebeat-filebeat ",
        "k8_pod": "redis-cart-0",
        "k8_node_name": "vm-worker-08",
        "message": " Redis   persistence warning ",
        "k8_namespace": "hipstershop",
    }
    data.update(changes)
    return source(data)


def test_real_log_fixture_accepts_database_rows_and_quarantines_application_rows() -> None:
    batch = normalize_file(FIXTURES / "logs.parquet", relative_to=FIXTURES)

    assert batch.statistics == BatchStatistics(input=5, accepted=3, duplicates=0, rejected=2)
    assert all(isinstance(event, NormalizedEvent) for event in batch.events)
    assert all(event.layer.value == "database" for event in batch.events)
    assert {rejected.reason for rejected in batch.rejected} == {RejectionReason.UNSUPPORTED_RECORD}


@pytest.mark.parametrize(
    ("fixture", "accepted_layer"),
    [
        ("node_metrics.parquet", "vm_guest_os"),
        ("pod_metrics.parquet", "database"),
        ("tidb_metrics.parquet", "database"),
        ("tikv_metrics.parquet", "database"),
    ],
)
def test_each_confirmed_metric_fixture_is_parsed(fixture: str, accepted_layer: str) -> None:
    batch = normalize_file(FIXTURES / fixture, relative_to=FIXTURES)

    assert batch.statistics.input > 0
    assert batch.statistics.input == batch.statistics.accepted + batch.statistics.duplicates + batch.statistics.rejected
    assert all(event.layer.value == accepted_layer for event in batch.events)


def test_json_incident_labels_are_read_but_not_misrepresented_as_events() -> None:
    records = read_aiops2025_records(FIXTURES / "incidents.json", relative_to=FIXTURES)
    batch = normalize_batch(records)

    assert len(records) == 2
    assert batch.statistics == BatchStatistics(input=2, accepted=0, duplicates=0, rejected=2)
    assert all(item.reason is RejectionReason.UNSUPPORTED_RECORD for item in batch.rejected)


def test_jsonl_reader_preserves_malformed_lines_for_quarantine(tmp_path: Path) -> None:
    path = tmp_path / "groundtruth.jsonl"
    path.write_text('{"start_time":"2025-01-01T00:00:00Z"}\nnot-json\n', encoding="utf-8")

    batch = normalize_file(path)

    assert batch.statistics == BatchStatistics(input=2, accepted=0, duplicates=0, rejected=2)
    assert batch.rejected[1].reason is RejectionReason.MALFORMED_RECORD
    assert batch.rejected[1].raw_record == {"raw_text": "not-json"}


def test_utf8_jsonl_content_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    path.write_text('{"instance":"数据库"}\n', encoding="utf-8")
    records = read_aiops2025_records(path)
    assert records[0].data["instance"] == "数据库"


def test_trace_epoch_milliseconds_and_nested_service_are_supported(tmp_path: Path) -> None:
    path = tmp_path / "trace.parquet"
    pl.DataFrame(
        {
            "traceID": ["trace-1"],
            "spanID": ["span-1"],
            "startTimeMillis": [1745906588031],
            "operationName": [" redis GET "],
            "process": [{"serviceName": "redis"}],
        }
    ).write_parquet(path)

    batch = normalize_file(path)

    assert batch.statistics.accepted == 1
    assert batch.events[0].service == "redis"
    assert batch.events[0].timestamp.tzinfo is not None
    assert batch.events[0].timestamp.utcoffset() == timedelta(0)


def test_normalization_cleans_text_and_preserves_optional_identifiers() -> None:
    event, _ = normalize_record(redis_log())

    assert event.source == "filebeat-filebeat"
    assert event.message == "Redis persistence warning"
    assert event.pod_id == "redis-cart-0"
    assert event.vm_id == "vm-worker-08"
    assert event.database_id == "redis-cart-0"
    assert event.severity.value == "unknown"


def test_null_sentinels_are_not_emitted_as_identifiers() -> None:
    event, _ = normalize_record(redis_log(k8_node_name="null"))
    assert event.vm_id is None


def test_offset_timestamp_is_converted_to_utc_and_original_is_retained() -> None:
    event, _ = normalize_record(redis_log(**{"@timestamp": "2025-06-01T18:30:00+08:00"}))

    assert event.timestamp == datetime(2025, 6, 1, 10, 30, tzinfo=timezone.utc)
    assert event.metadata["original_timestamp"] == "2025-06-01T18:30:00+08:00"


def test_naive_timestamp_is_rejected_without_a_documented_timezone() -> None:
    batch = normalize_batch((redis_log(**{"@timestamp": "2025-06-01 18:30:00"}),))
    assert batch.rejected[0].reason is RejectionReason.INVALID_TIMESTAMP


def test_explicit_timezone_converts_unambiguous_naive_timestamp() -> None:
    assert parse_utc_timestamp("2025-06-01 18:30:00", assumed_timezone="Asia/Shanghai") == datetime(
        2025, 6, 1, 10, 30, tzinfo=timezone.utc
    )


def test_daylight_saving_gap_and_fold_are_explicit() -> None:
    with pytest.raises(NormalizationError) as nonexistent:
        parse_utc_timestamp("2025-03-09T02:30:00", assumed_timezone="America/New_York")
    assert nonexistent.value.reason is RejectionReason.NONEXISTENT_LOCAL_TIME

    with pytest.raises(NormalizationError) as ambiguous:
        parse_utc_timestamp("2025-11-02T01:30:00", assumed_timezone="America/New_York")
    assert ambiguous.value.reason is RejectionReason.AMBIGUOUS_TIMEZONE
    assert parse_utc_timestamp(
        "2025-11-02T01:30:00", assumed_timezone="America/New_York", fold=1
    ) == datetime(2025, 11, 2, 6, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("bad_timestamp", [None, "", "not-a-time"])
def test_malformed_timestamps_are_quarantined(bad_timestamp: object) -> None:
    batch = normalize_batch((redis_log(**{"@timestamp": bad_timestamp}),))
    assert batch.rejected[0].reason is RejectionReason.INVALID_TIMESTAMP


def test_missing_message_is_quarantined() -> None:
    batch = normalize_batch((redis_log(message=None),))
    assert batch.rejected[0].reason is RejectionReason.MISSING_REQUIRED_FIELD


def test_invalid_explicit_severity_is_quarantined() -> None:
    batch = normalize_batch((redis_log(severity="emergency"),))
    assert batch.rejected[0].reason is RejectionReason.INVALID_SEVERITY


def test_canonical_schema_failure_is_quarantined() -> None:
    batch = normalize_batch((redis_log(agent_name="x" * 257),))
    assert batch.rejected[0].reason is RejectionReason.SCHEMA_VALIDATION_FAILED


def test_confirmed_pd_metric_shape_is_supported() -> None:
    batch = normalize_batch(
        (
            source(
                {
                    "time": UTC_TIME,
                    "object_type": "pd",
                    "instance": "null",
                    "leader_changes": 2.0,
                }
            ),
        )
    )
    assert batch.events[0].database_id == "pd"


def test_fingerprint_is_stable_and_duplicate_record_is_preserved() -> None:
    first = redis_log()
    second = first.model_copy(update={"source_file": "another/path.parquet", "row_number": 99})
    first_event, first_fingerprint = normalize_record(first)
    second_event, second_fingerprint = normalize_record(second)
    batch = normalize_batch((first, second))

    assert first_fingerprint == second_fingerprint
    assert first_event.event_id == second_event.event_id
    assert batch.statistics == BatchStatistics(input=2, accepted=1, duplicates=1, rejected=0)
    assert batch.duplicates[0].raw_record["message"] == " Redis   persistence warning "


def test_existing_fingerprints_detect_duplicates_across_batches() -> None:
    _, fingerprint = normalize_record(redis_log())
    batch = normalize_batch((redis_log(),), seen_fingerprints={fingerprint})
    assert batch.statistics == BatchStatistics(input=1, accepted=0, duplicates=1, rejected=0)


def test_provenance_keeps_source_location_raw_record_and_original_time() -> None:
    event, _ = normalize_record(redis_log())

    assert event.provenance.source == "AIOps2025"
    assert event.provenance.record_id == "fixture.parquet:1"
    assert event.provenance.details["raw_record"]["message"] == " Redis   persistence warning "
    assert event.metadata["original_timestamp"] == UTC_TIME.isoformat()


def test_batch_counts_reconcile_with_accepted_duplicate_and_rejected() -> None:
    batch = normalize_batch((redis_log(), redis_log(), redis_log(message=None)))
    assert batch.statistics == BatchStatistics(input=3, accepted=1, duplicates=1, rejected=1)
    with pytest.raises(ValidationError, match="must equal"):
        BatchStatistics(input=3, accepted=3, duplicates=1, rejected=0)
