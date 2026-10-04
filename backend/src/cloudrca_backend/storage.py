"""Minimal storage boundary selected by the Issue #14 architecture spike."""

from collections.abc import Sequence
from typing import Protocol

from .contracts import Identifier


class KnowledgeChunk(Protocol):
    chunk_id: Identifier
    text: str


class KnowledgeRepository(Protocol):
    """Future RAG storage boundary; operational records remain ordinary persisted data."""

    def upsert(self, chunk_id: str, text: str, embedding: Sequence[float], metadata: dict[str, str]) -> None: ...

    def search(self, embedding: Sequence[float], *, metadata: dict[str, str], limit: int) -> list[str]: ...
