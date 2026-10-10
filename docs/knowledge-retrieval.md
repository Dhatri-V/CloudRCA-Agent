# Knowledge retrieval

Issue #15 adds a local SQLite knowledge store for the MVP. Curated Markdown documents in `knowledge/corpus/` carry source, version, layer, and product metadata in YAML front matter. Each section is chunked at word boundaries and gets a SHA-256 citation ID derived from its stable metadata and content.

Refresh the corpus with the evaluated Issue #14 embedding model:

```bash
cloudrca-knowledge-refresh
```

The command is idempotent: re-running it updates existing chunk IDs without duplicating rows. `SqliteKnowledgeRepository.retrieve()` supports exact `layer`, `product`, and `version` filters and only returns complete chunks that fit `max_characters`. Lexical retrieval is intentionally omitted because no evaluation has shown it improves this corpus.
