"""Generate reproducible profile and topology evidence from an extracted subset."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

import polars as pl

from cloudrca_dataset_validation import extract_database_placements, profile_directory


def _safe(value):
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    raise TypeError(type(value).__name__)


def topology_evidence(root: Path) -> dict:
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

    redis_nodes = sorted({item["vm_node_id"] for item in placements if item["database_component_id"] == "redis-cart"})
    return {
        "topology_status": "PARTIAL",
        "verified_relationships": ["redis-cart pod -> Kubernetes worker VM"],
        "unverified_relationships": ["TiDB/TiKV/PD component -> Kubernetes worker VM"],
        "redis_vm_nodes": redis_nodes,
        "redis_rescheduling_observed": len(redis_nodes) > 1,
        "tidb_metric_files": len(tidb_files),
        "tidb_metric_rows": tidb_rows,
        "tidb_rows_with_explicit_pod_and_node": explicit_tidb_placement_rows,
        "placements": placements,
        "inference_policy": "A relationship is emitted only when one record contains timestamp, database pod ID, and worker-node ID.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--profile-output", type=Path, required=True)
    parser.add_argument("--topology-output", type=Path, required=True)
    args = parser.parse_args()
    args.profile_output.parent.mkdir(parents=True, exist_ok=True)
    args.topology_output.parent.mkdir(parents=True, exist_ok=True)
    args.profile_output.write_text(json.dumps(profile_directory(args.source), indent=2, sort_keys=True, default=_safe) + "\n")
    args.topology_output.write_text(json.dumps(topology_evidence(args.source), indent=2, sort_keys=True, default=_safe) + "\n")


if __name__ == "__main__":
    main()
