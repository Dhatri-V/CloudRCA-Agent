"""Local knowledge ingestion and retrieval for evidence-grounded RCA."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from .contracts import Layer

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class EmbeddingModel(Protocol):
    def encode(self, texts: Sequence[str]) -> list[list[float]]: ...


class SentenceTransformerEmbedder:
    """Lazy production embedder so import and test collection stay offline."""

    def __init__(self, model_name: str = EMBEDDING_MODEL) -> None:
        self._model_name = model_name
        self._model: Any | None = None

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._model_name)
        return self._model.encode(list(texts), normalize_embeddings=True).tolist()


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    source: str
    version: str
    layer: Layer
    product: str
    sections: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    chunk_id: str
    source: str
    section: str
    version: str
    layer: Layer
    product: str
    text: str


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    chunk: KnowledgeChunk
    score: float


def parse_markdown_document(path: Path) -> KnowledgeDocument:
    """Parse a curated Markdown document with required YAML front matter."""
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---\n"):
        raise ValueError(f"{path} must start with YAML front matter")
    _, front_matter, body = content.split("---\n", 2)
    metadata = yaml.safe_load(front_matter)
    if not isinstance(metadata, dict):
        raise ValueError(f"{path} front matter must be a mapping")
    required = ("source", "layer", "product")
    if any(not isinstance(metadata.get(name), str) or not metadata[name].strip() for name in required) or not metadata.get("version"):
        raise ValueError(f"{path} front matter requires source, version, layer, and product")
    try:
        layer = Layer(metadata["layer"])
    except ValueError as error:
        raise ValueError(f"{path} has unsupported layer {metadata['layer']!r}") from error
    sections: list[tuple[str, str]] = []
    heading = "Overview"
    lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("# "):
            continue
        if line.startswith("## "):
            text = "\n".join(lines).strip()
            if text:
                sections.append((heading, text))
            heading, lines = line[3:].strip(), []
        else:
            lines.append(line)
    text = "\n".join(lines).strip()
    if text:
        sections.append((heading, text))
    if not sections:
        raise ValueError(f"{path} has no readable sections")
    return KnowledgeDocument(metadata["source"].strip(), str(metadata["version"]), layer, metadata["product"].strip(), tuple(sections))


def chunk_document(document: KnowledgeDocument, *, max_characters: int = 1_000) -> tuple[KnowledgeChunk, ...]:
    """Split sections at word boundaries and derive stable, citation-ready IDs."""
    if max_characters < 1:
        raise ValueError("max_characters must be positive")
    chunks: list[KnowledgeChunk] = []
    for section, text in document.sections:
        words = text.split()
        part: list[str] = []
        for word in words:
            candidate = " ".join((*part, word))
            if part and len(candidate) > max_characters:
                chunks.append(_chunk(document, section, len(chunks), " ".join(part)))
                part = [word]
            else:
                part.append(word)
        if part:
            chunks.append(_chunk(document, section, len(chunks), " ".join(part)))
    return tuple(chunks)


def _chunk(document: KnowledgeDocument, section: str, ordinal: int, text: str) -> KnowledgeChunk:
    identity = "\x1f".join((document.source, document.version, document.layer.value, document.product, section, str(ordinal), text))
    return KnowledgeChunk(hashlib.sha256(identity.encode("utf-8")).hexdigest(), document.source, section, document.version, document.layer, document.product, text)


class SqliteKnowledgeRepository:
    """Application-owned vectors with exact metadata filters for the local MVP."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS knowledge_chunks (
                chunk_id TEXT PRIMARY KEY, source TEXT NOT NULL, section TEXT NOT NULL,
                version TEXT NOT NULL, layer TEXT NOT NULL, product TEXT NOT NULL,
                text TEXT NOT NULL, embedding TEXT NOT NULL)""")

    def refresh(self, chunks: Iterable[KnowledgeChunk], embedder: EmbeddingModel) -> int:
        items = tuple(chunks)
        vectors = embedder.encode([item.text for item in items])
        if len(vectors) != len(items):
            raise ValueError("embedder returned a vector count different from the chunk count")
        with self._connect() as connection:
            connection.executemany("""INSERT INTO knowledge_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET source=excluded.source, section=excluded.section,
                version=excluded.version, layer=excluded.layer, product=excluded.product,
                text=excluded.text, embedding=excluded.embedding""", [
                (item.chunk_id, item.source, item.section, item.version, item.layer.value, item.product, item.text, json.dumps(vector))
                for item, vector in zip(items, vectors, strict=True)
            ])
        return len(items)

    def retrieve(self, query: str, embedder: EmbeddingModel, *, layer: Layer | None = None, product: str | None = None,
                 version: str | None = None, limit: int = 5, max_characters: int = 8_000) -> tuple[RetrievedChunk, ...]:
        if limit < 1 or max_characters < 1:
            raise ValueError("limit and max_characters must be positive")
        query_vector = embedder.encode([query])
        if len(query_vector) != 1:
            raise ValueError("embedder must return one query vector")
        clauses, values = [], []
        for column, value in (("layer", layer.value if layer else None), ("product", product), ("version", version)):
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(f"SELECT * FROM knowledge_chunks{where}", values).fetchall()
        scored = sorted((RetrievedChunk(KnowledgeChunk(row[0], row[1], row[2], row[3], Layer(row[4]), row[5], row[6]), _cosine(query_vector[0], json.loads(row[7]))) for row in rows), key=lambda item: (-item.score, item.chunk.chunk_id))
        selected: list[RetrievedChunk] = []
        used = 0
        for item in scored:
            if len(selected) == limit:
                break
            if used + len(item.chunk.text) <= max_characters:
                selected.append(item)
                used += len(item.chunk.text)
        return tuple(selected)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("embedding vectors must have matching non-zero dimensions")
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(sum(value * value for value in right))
    return sum(a * b for a, b in zip(left, right, strict=True)) / denominator if denominator else 0.0


def refresh_corpus(repository: SqliteKnowledgeRepository, corpus: Path, embedder: EmbeddingModel) -> int:
    """Idempotently ingest every curated Markdown document under a corpus directory."""
    paths = sorted(corpus.rglob("*.md"))
    if not paths:
        raise FileNotFoundError(f"no Markdown documents found under {corpus}")
    return repository.refresh((chunk for path in paths for chunk in chunk_document(parse_markdown_document(path))), embedder)
