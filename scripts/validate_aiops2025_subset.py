"""Generate reproducible profile and topology evidence from an extracted subset."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

import polars as pl

from cloudrca_dataset_validation import extract_database_placements, profile_directory


def _safe(value):
    """Serialize UTC-aware datetimes used in topology intervals."""
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    raise TypeError(type(value).__name__)


def topology_evidence(root: Path) -> dict:
    """Build topology conclusions from relationships explicit in ``root``."""
    placements = []
    candidate_files = sorted(root.rglob("infra_pod_pod_cpu_usage*.parquet")) + sorted(root.rglob("log_filebeat*.parquet"))
    for path in candidate_files:
        placements.extend(
            extract_database_placements(
                pl.read_parquet(path),
                mapping_source=path.relative_to(root).as_posix(),
            )
        )

    tidb_files = sorted(root.rglob("infra_tidb*.parquet")) + sorted(root.rglob("infra_tikv*.parquet")) + sorted(root.rglob("infra_pd*.parquet"))
    tidb_rows = 0
    explicit_tidb_placement_rows = 0
    for path in tidb_files:
        frame = pl.read_parquet(path)
        tidb_rows += frame.height
        if "pod" in frame.columns and "kubernetes_node" in frame.columns:
            explicit_tidb_placement_rows += frame.filter(
                ~pl.col("pod").cast(pl.String).str.to_lowercase().is_in(["null", "", "none"])
                & ~pl.col("kubernetes_node").cast(pl.String).str.to_lowercase().is_in(["null", "", "none"])
            ).height

    observed_components = {item["database_component_id"] for item in placements}
    for path in tidb_files:
        if path.name.startswith("infra_tidb"):
            observed_components.add("tidb")
        elif path.name.startswith("infra_tikv"):
            observed_components.add("tikv")
        elif path.name.startswith("infra_pd"):
            observed_components.add("pd")
    mapped_components = {item["database_component_id"] for item in placements}
    unmapped_components = observed_components - mapped_components
    status = "VERIFIED" if mapped_components and not unmapped_components else "PARTIAL" if mapped_components else "NOT AVAILABLE"
    redis_nodes = sorted({item["vm_node_id"] for item in placements if item["database_component_id"] == "redis-cart"})
    return {
        "topology_status": status,
        "verified_relationships": [
            f"{component} pod -> Kubernetes worker VM" for component in sorted(mapped_components)
        ],
        "unverified_relationships": [
            f"{component} component -> Kubernetes worker VM" for component in sorted(unmapped_components)
        ],
        "redis_vm_nodes": redis_nodes,
        "redis_rescheduling_observed": len(redis_nodes) > 1,
        "tidb_metric_files": len(tidb_files),
        "tidb_metric_rows": tidb_rows,
        "tidb_rows_with_explicit_pod_and_node": explicit_tidb_placement_rows,
        "placements": placements,
        "inference_policy": "A relationship is emitted only when one record contains timestamp, database pod ID, and worker-node ID.",
    }


def main() -> None:
    """Write profiling and topology evidence for an extracted subset."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--profile-output", type=Path, required=True)
    parser.add_argument("--topology-output", type=Path, required=True)
    args = parser.parse_args()
    args.profile_output.parent.mkdir(parents=True, exist_ok=True)
    args.topology_output.parent.mkdir(parents=True, exist_ok=True)
    args.profile_output.write_text(
        json.dumps(profile_directory(args.source), indent=2, sort_keys=True, default=_safe) + "\n",
        encoding="utf-8",
    )
    args.topology_output.write_text(
        json.dumps(topology_evidence(args.source), indent=2, sort_keys=True, default=_safe) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
