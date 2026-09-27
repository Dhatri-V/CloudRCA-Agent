from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from cloudrca_dataset_validation.topology import extract_database_placements


FIXTURES = Path(__file__).parent / "fixtures" / "aiops2025"


def test_extracts_explicit_redis_vm_mapping() -> None:
    frame = pl.read_parquet(FIXTURES / "pod_metrics.parquet")
    mappings = extract_database_placements(frame, mapping_source="infra_pod")
    assert len(mappings) == 1
    assert mappings[0]["database_component_id"] == "redis-cart"
    assert mappings[0]["database_pod_id"] == "redis-cart-0"
    assert mappings[0]["vm_node_id"] == "vm-worker-08"
    assert mappings[0]["valid_from_utc"].tzinfo is not None


def test_tidb_endpoint_is_not_guessed_to_be_a_vm() -> None:
    frame = pl.read_parquet(FIXTURES / "tidb_metrics.parquet")
    assert extract_database_placements(frame, mapping_source="infra_tidb") == []


def test_coincident_records_do_not_create_a_relationship() -> None:
    timestamp = datetime(2025, 4, 30, tzinfo=timezone.utc)
    frame = pl.DataFrame(
        {
            "time": [timestamp, timestamp],
            "instance": ["vm-worker-03", "10.233.0.11:20180"],
            "object_type": ["node", "tikv"],
            "pod": [None, None],
        }
    )
    assert extract_database_placements(frame, mapping_source="mixed_metrics") == []


def test_node_change_creates_time_aware_intervals() -> None:
    frame = pl.DataFrame(
        {
            "time": [
                datetime(2025, 4, 30, 10, 0, tzinfo=timezone.utc),
                datetime(2025, 4, 30, 10, 1, tzinfo=timezone.utc),
                datetime(2025, 4, 30, 10, 2, tzinfo=timezone.utc),
            ],
            "instance": ["vm-1", "vm-1", "vm-2"],
            "object_type": ["pod", "pod", "pod"],
            "pod": ["redis-cart-0", "redis-cart-0", "redis-cart-0"],
        }
    )
    mappings = extract_database_placements(frame, mapping_source="infra_pod")
    assert [(item["vm_node_id"], item["valid_from_utc"].minute, item["valid_to_utc"].minute) for item in mappings] == [
        ("vm-1", 0, 1),
        ("vm-2", 2, 2),
    ]
