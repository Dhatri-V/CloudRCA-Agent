"""Refresh the curated local knowledge corpus."""

from __future__ import annotations

import argparse
from pathlib import Path

from cloudrca_backend.knowledge import SentenceTransformerEmbedder, SqliteKnowledgeRepository, refresh_corpus


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=Path("knowledge/corpus"))
    parser.add_argument("--database", type=Path, default=Path("data/knowledge.sqlite3"))
    args = parser.parse_args()
    count = refresh_corpus(SqliteKnowledgeRepository(args.database), args.corpus, SentenceTransformerEmbedder())
    print(f"Refreshed {count} knowledge chunks into {args.database}")


if __name__ == "__main__":
    main()
