"""Offline coverage for the Issue #15 local RAG pipeline."""

from pathlib import Path

import pytest
from cloudrca_backend.contracts import Layer
from cloudrca_backend.knowledge import (
    KnowledgeChunk,
    KnowledgeDocument,
    SqliteKnowledgeRepository,
    chunk_document,
    parse_markdown_document,
    refresh_corpus,
)


class KeywordEmbedder:
    """Deterministic test double; production uses the pinned MiniLM model."""

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[float(text.lower().count("storage")), float(text.lower().count("memory")), 1.0] for text in texts]


def _chunks() -> tuple[KnowledgeChunk, ...]:
    return chunk_document(KnowledgeDocument("test guide", "1.0", Layer.HYPERVISOR, "KVM", (("Storage", "storage storage queue delays guest input output"),))) + chunk_document(KnowledgeDocument("test guide", "1.0", Layer.VM, "Linux", (("Memory", "memory pressure causes swapping"),)))


def test_parsing_and_chunking_preserve_metadata_and_stable_citations(tmp_path: Path) -> None:
    document = tmp_path / "guide.md"
    document.write_text("---\nsource: guide\nversion: 1\nlayer: database\nproduct: Redis\n---\n# Guide\n\n## Timeout\none two three four", encoding="utf-8")
    parsed = parse_markdown_document(document)
    first = chunk_document(parsed, max_characters=7)
    assert [item.chunk_id for item in first] == [item.chunk_id for item in chunk_document(parsed, max_characters=7)]
    assert parsed.layer is Layer.DATABASE
    assert all(item.source == "guide" and item.section == "Timeout" for item in first)


def test_refresh_is_idempotent_filters_and_respects_budget(tmp_path: Path) -> None:
    repository, embedder, chunks = SqliteKnowledgeRepository(tmp_path / "knowledge.sqlite3"), KeywordEmbedder(), _chunks()
    assert repository.refresh(chunks, embedder) == repository.refresh(chunks, embedder) == 2
    results = repository.retrieve("storage", embedder, layer=Layer.HYPERVISOR, product="KVM", version="1.0", max_characters=100)
    assert results[0].chunk == chunks[0]
    assert repository.retrieve("storage", embedder, max_characters=5) == ()
    assert repository.retrieve("missing", embedder, product="missing") == ()


def test_refresh_requires_documents_and_corpus_covers_all_layers(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        refresh_corpus(SqliteKnowledgeRepository(tmp_path / "empty.sqlite3"), tmp_path / "empty", KeywordEmbedder())
    assert {parse_markdown_document(path).layer for path in Path("knowledge/corpus").glob("*.md")} == set(Layer)


def test_labeled_query_set_meets_recall_threshold(tmp_path: Path) -> None:
    repository, embedder, chunks = SqliteKnowledgeRepository(tmp_path / "knowledge.sqlite3"), KeywordEmbedder(), _chunks()
    repository.refresh(chunks, embedder)
    queries = (("storage", chunks[0].chunk_id), ("memory", chunks[1].chunk_id))
    assert sum(repository.retrieve(query, embedder, limit=1)[0].chunk.chunk_id == expected for query, expected in queries) / len(queries) >= 1.0
