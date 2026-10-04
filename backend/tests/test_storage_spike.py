"""Repeatable SQLite local-vector baseline for the Issue #14 architecture spike."""

import ast
import sqlite3
from pathlib import Path


def _search(path: Path, query: tuple[float, ...], layer: str) -> list[str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT chunk_id, vector FROM chunks WHERE layer = ?", (layer,)).fetchall()
    return [
        chunk_id
        for chunk_id, _ in sorted(
            rows, key=lambda row: -sum(a * b for a, b in zip(query, ast.literal_eval(row[1])))
        )
    ]


def test_sqlite_local_vector_baseline_persists_and_filters_metadata(tmp_path: Path) -> None:
    database = tmp_path / "knowledge.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, layer TEXT NOT NULL, vector TEXT NOT NULL)")
        connection.executemany("INSERT INTO chunks VALUES (?, ?, ?)", [("db", "database", "(1.0, 0.0)"), ("vm", "vm_guest_os", "(0.0, 1.0)")])
    assert _search(database, (1.0, 0.0), "database") == ["db"]
    assert _search(database, (1.0, 0.0), "vm_guest_os") == ["vm"]
