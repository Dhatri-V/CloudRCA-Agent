import shutil
from pathlib import Path

from scripts.validate_aiops2025_subset import topology_evidence

FIXTURES = Path(__file__).parent / "fixtures" / "aiops2025"


def test_topology_status_is_derived_from_supplied_files(tmp_path: Path) -> None:
    """A Redis-only subset must not claim absent TiDB components are unverified."""
    destination = tmp_path / "infra_pod_pod_cpu_usage_fixture.parquet"
    shutil.copyfile(FIXTURES / "pod_metrics.parquet", destination)
    evidence = topology_evidence(tmp_path)
    assert evidence["topology_status"] == "VERIFIED"
    assert evidence["verified_relationships"] == ["redis-cart pod -> Kubernetes worker VM"]
    assert evidence["unverified_relationships"] == []
