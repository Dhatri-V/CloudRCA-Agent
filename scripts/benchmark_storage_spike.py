"""Repeatable Issue #14 SQLite/local-vector and PostgreSQL/pgvector comparison."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import psycopg
from sentence_transformers import SentenceTransformer

MODEL = "sentence-transformers/all-MiniLM-L6-v2"
CORPUS = (
    ("db-deadlock", "database", "v1", "A transaction deadlock blocks database writes."),
    ("vm-memory", "vm_guest_os", "v1", "Guest virtual machine memory pressure causes swapping."),
    ("host-contention", "host_hypervisor", "v1", "Hypervisor CPU contention delays hosted virtual machines."),
    ("db-replication", "database", "v1", "Replication lag delays database replicas."),
)
QUERIES = (("database deadlock", "database", "db-deadlock"), ("host CPU contention", "host_hypervisor", "host-contention"))


def vector(value: list[float]) -> str:
    return "[" + ",".join(str(item) for item in value) + "]"


def sqlite_run(path: Path, embeddings: list[list[float]], queries: list[list[float]]) -> tuple[list[str], float]:
    started = time.perf_counter()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE knowledge (id TEXT PRIMARY KEY, layer TEXT, version TEXT, embedding TEXT)")
        connection.executemany("INSERT INTO knowledge VALUES (?, ?, ?, ?)", [(chunk_id, layer, version, json.dumps(embedding)) for (chunk_id, layer, version, _), embedding in zip(CORPUS, embeddings)])
    results: list[str] = []
    for query, (_, layer, _) in zip(queries, QUERIES):
        with sqlite3.connect(path) as connection:
            rows = connection.execute("SELECT id, embedding FROM knowledge WHERE layer = ?", (layer,)).fetchall()
        results.append(max(rows, key=lambda item: sum(a * b for a, b in zip(query, json.loads(item[1]))))[0])
    return results, (time.perf_counter() - started) * 1000


def postgres_run(embeddings: list[list[float]], queries: list[list[float]]) -> tuple[list[str], float]:
    started = time.perf_counter()
    dsn = "postgresql://postgres:cloudrca_spike@localhost:55432/cloudrca_spike"
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
        connection.execute("DROP TABLE IF EXISTS knowledge")
        connection.execute("CREATE TABLE knowledge (id text PRIMARY KEY, layer text, version text, embedding vector(384))")
        with connection.cursor() as cursor:
            cursor.executemany("INSERT INTO knowledge VALUES (%s, %s, %s, %s::vector)", [(chunk_id, layer, version, vector(embedding)) for (chunk_id, layer, version, _), embedding in zip(CORPUS, embeddings)])
        connection.execute("ALTER TABLE knowledge ADD COLUMN migration_probe text DEFAULT 'ok'")
    results: list[str] = []
    for query, (_, layer, _) in zip(queries, QUERIES):
        with psycopg.connect(dsn) as connection:
            row = connection.execute("SELECT id FROM knowledge WHERE layer = %s ORDER BY embedding <=> %s::vector LIMIT 1", (layer, vector(query))).fetchone()
            assert row is not None
            results.append(row[0])
    return results, (time.perf_counter() - started) * 1000


def main() -> None:
    model = SentenceTransformer(MODEL)
    embeddings = model.encode([item[3] for item in CORPUS], normalize_embeddings=True).tolist()
    queries = model.encode([item[0] for item in QUERIES], normalize_embeddings=True).tolist()
    workspace = Path(".spike-storage")
    workspace.mkdir(exist_ok=True)
    sqlite_results, sqlite_ms = sqlite_run(workspace / "knowledge.sqlite3", embeddings, queries)
    postgres_results, postgres_ms = postgres_run(embeddings, queries)
    expected = [item[2] for item in QUERIES]
    print(json.dumps({"model": MODEL, "dimensions": len(embeddings[0]), "expected": expected, "sqlite": {"results": sqlite_results, "recall_at_1": sum(a == b for a, b in zip(sqlite_results, expected)) / len(expected), "ms": sqlite_ms}, "postgres": {"results": postgres_results, "recall_at_1": sum(a == b for a, b in zip(postgres_results, expected)) / len(expected), "ms": postgres_ms}}, indent=2))


if __name__ == "__main__":
    main()
