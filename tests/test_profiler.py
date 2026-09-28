import json
from pathlib import Path

import pytest

from cloudrca_dataset_validation.profiler import profile_directory, profile_file

FIXTURES = Path(__file__).parent / "fixtures" / "aiops2025"


def test_profile_is_deterministic() -> None:
    """Repeated profiles of unchanged fixtures must be identical."""
    assert profile_directory(FIXTURES) == profile_directory(FIXTURES)


def test_fixture_profile_has_reproducible_counts_and_schema() -> None:
    """Fixture counts, schemas, and sentinel missing values must be stable."""
    result = profile_directory(FIXTURES)
    assert result["file_count"] == 7
    assert result["total_records"] == 25
    assert result["malformed_files"] == 0
    pod = next(item for item in result["files"] if item["path"] == "pod_metrics.parquet")
    assert pod["record_count"] == 5
    assert pod["schema"]["time"] == "Datetime(time_unit='ns', time_zone='UTC')"
    assert pod["identifier_coverage"]["pod"] == {"non_null": 5, "distinct": 2}
    assert pod["missing_counts"]["kubernetes_node"] == 5


def test_utc_timestamp_metadata_is_preserved() -> None:
    """Parquet UTC timezone metadata must survive profiling."""
    profile = profile_file(FIXTURES / "node_metrics.parquet")
    assert profile["timestamps"]["time"]["timezone"] == "UTC"
    assert profile["timestamps"]["time"]["minimum"].endswith("Z")


def test_text_fixtures_load_without_encoding_errors() -> None:
    """Committed JSON fixtures must decode successfully."""
    for name in ("incidents.json", "fixture_manifest.json"):
        profile = profile_file(FIXTURES / name)
        assert "read_error" not in profile
        assert profile["encoding"] is not None


def test_heterogeneous_json_values_are_profiled_not_marked_malformed(tmp_path: Path) -> None:
    """Mixed scalar/list label values must normalize into a valid profile."""
    path = tmp_path / "groundtruth.jsonl"
    rows = [{"instance": "service-a"}, {"instance": ["pod-a", "service-a"]}]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    profile = profile_file(path)
    assert "read_error" not in profile
    assert profile["record_count"] == 2
    assert profile["schema"]["instance"] == "String"


def test_identifier_coverage_excludes_missing_string_sentinels(tmp_path: Path) -> None:
    """Literal null markers must not count as real identifiers."""
    path = tmp_path / "identifiers.json"
    path.write_text(json.dumps([{"instance": "null"}, {"instance": ""}, {"instance": "vm-1"}]), encoding="utf-8")
    profile = profile_file(path)
    assert profile["identifier_coverage"]["instance"] == {"non_null": 1, "distinct": 1}


def test_uppercase_jsonl_suffix_is_supported(tmp_path: Path) -> None:
    """Suffix matching must be case-insensitive during parsing and discovery."""
    path = tmp_path / "labels.JSONL"
    path.write_text('{"instance":"vm-1"}\n{"instance":"vm-2"}\n', encoding="utf-8")
    profile = profile_file(path)
    assert profile["format"] == "jsonl"
    assert profile["record_count"] == 2


def test_missing_profile_root_fails_explicitly(tmp_path: Path) -> None:
    """A missing dataset root must not produce a misleading empty success."""
    with pytest.raises(NotADirectoryError):
        profile_directory(tmp_path / "missing")
