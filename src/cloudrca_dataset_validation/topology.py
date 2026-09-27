"""Evidence-constrained topology extraction for AIOps2025 records."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
import re

import polars as pl

DATABASE_POD = re.compile(r"(?i)(?:^|[-_])(redis|tidb|tikv|tiflash|pd)(?:[-_]|$)")


def _field(frame: pl.DataFrame, candidates: tuple[str, ...]) -> str | None:
    return next((candidate for candidate in candidates if candidate in frame.columns), None)


def _component(pod: str) -> str:
    lower = pod.lower()
    for component in ("redis-cart", "tidb-tikv", "tidb-tidb", "tidb-pd", "tikv", "tiflash", "redis"):
        if component in lower:
            return component
    return pod


def extract_database_placements(
    frame: pl.DataFrame,
    *,
    mapping_source: str,
    maximum_gap: timedelta = timedelta(minutes=2),
) -> list[dict[str, Any]]:
    """Extract only mappings explicitly co-located in individual records.

    The ``instance`` field is accepted as a node only for rows whose
    ``object_type`` is ``pod``. This prevents TiDB endpoint addresses from
    being reinterpreted as worker-node identifiers.
    """
    time_column = _field(frame, ("time", "@timestamp"))
    pod_column = _field(frame, ("pod", "k8_pod"))
    node_column = _field(frame, ("k8_node_name", "vm_node_id"))
    if node_column is None and "instance" in frame.columns and "object_type" in frame.columns:
        types = frame["object_type"].drop_nulls().unique().to_list()
        if types and set(types) == {"pod"}:
            node_column = "instance"
    if not all((time_column, pod_column, node_column)):
        return []

    explicit = (
        frame.select(
            pl.col(time_column).alias("timestamp"),
            pl.col(pod_column).cast(pl.String).alias("database_pod_id"),
            pl.col(node_column).cast(pl.String).alias("vm_node_id"),
        )
        .drop_nulls()
        .filter(pl.col("database_pod_id").map_elements(lambda value: bool(DATABASE_POD.search(value)), return_dtype=pl.Boolean))
        .sort(["database_pod_id", "timestamp"])
    )
    if explicit.is_empty():
        return []

    placements: list[dict[str, Any]] = []
    for pod_frame in explicit.partition_by("database_pod_id", maintain_order=True):
        rows = pod_frame.iter_rows(named=True)
        current: dict[str, Any] | None = None
        for row in rows:
            timestamp = row["timestamp"]
            if current is None or row["vm_node_id"] != current["vm_node_id"] or timestamp - current["valid_to_utc"] > maximum_gap:
                if current:
                    placements.append(current)
                current = {
                    "database_component_id": _component(row["database_pod_id"]),
                    "database_pod_id": row["database_pod_id"],
                    "vm_node_id": row["vm_node_id"],
                    "valid_from_utc": timestamp,
                    "valid_to_utc": timestamp,
                    "mapping_source": mapping_source,
                }
            else:
                current["valid_to_utc"] = timestamp
        if current:
            placements.append(current)
    return placements
