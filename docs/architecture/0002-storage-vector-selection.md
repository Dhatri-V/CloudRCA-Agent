# ADR 0002: storage and vector-search selection

## Status

Accepted for the local MVP; revisit before multi-user deployment.

## Context and candidates

CloudRCA needs durable normalized events, topology/evidence, specialist and audit outputs, candidate incidents, correlation graphs, and future reports. RAG retrieval needs knowledge chunks, embeddings, source/layer/version metadata, and metadata filtering. The candidates were SQLite 3 plus a local vector search and PostgreSQL 17 plus pgvector.

## Method and measured results

`scripts/benchmark_storage_spike.py` used the same four knowledge chunks, layer/version metadata filters, two expected-answer queries, and cached `sentence-transformers/all-MiniLM-L6-v2` embeddings (384 dimensions) for both candidates. It measures the small corpus's end-to-end schema/create/insert/filter/retrieve path; it is not a production latency claim.

On macOS arm64 with Docker Desktop 29.5.2, SQLite's local application-owned cosine scan returned both expected chunks (Recall@1 1.0) in 3.33 ms. PostgreSQL 17 + pgvector in the temporary `pgvector/pgvector:pg17` container returned both expected chunks (Recall@1 1.0) in 50.41 ms. Both candidates reopened their database connection before retrieval, proving restart persistence for the fixture. PostgreSQL enabled `vector` and applied `ALTER TABLE ... ADD COLUMN migration_probe`; SQLite's smoke schema is recreated from ordinary SQL. SQLAlchemy 2 and Alembic are project-managed experiment dependencies; production migrations are deferred to Issue #18.

The PostgreSQL experiment used one temporary container, port 55432, and no project data volume. A four-reader concurrent probe returned the expected count (4) from all four independent connections. This is a small correctness check, not an enterprise load test. The pulled image and Docker startup are an additional local-development dependency; SQLite requires only the Python standard library. Multi-user concurrency remains a trigger to revisit this decision.

## Decision

Select SQLite plus a local, application-owned vector baseline for the local MVP: it matched pgvector's small-corpus Recall@1, has lower measured setup-path latency, and avoids a service for local development. Keep the `KnowledgeRepository` boundary storage-independent. PostgreSQL/pgvector remains the re-evaluation option before production persistence in Issue #18 if expected concurrent writers, corpus volume, or deployment durability outgrow SQLite.

## Consequences

Local development needs no service or Docker volume. This ADR must be revisited when multi-user concurrency, operational durability, corpus size, or server-side vector indexing exceeds the local MVP.
