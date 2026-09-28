# Shared data formats

Issue #4 defines the typed values exchanged between future CloudRCA pipeline stages. These models validate data only; they do not parse logs, group incidents, correlate events, or call an AI model.

## Data flow

```text
RawLogRecord -> NormalizedEvent -> Finding / Correlation -> Incident -> RCAReport
                         |
                         +-> TopologyRelationship
```

All contracts are strict and immutable after validation. Timestamps must include the UTC offset (`Z` or `+00:00`), confidence contains both a score from `0.0` through `1.0` and a `low`, `medium`, or `high` level, and claims carry evidence references. Provenance marks data as `observed`, `augmented`, or `synthetic`.

```python
from cloudrca_backend.contracts import NormalizedEvent, Provenance

event = NormalizedEvent(
    event_id="event-001",
    timestamp="2025-06-01T10:00:00Z",
    source="database.log",
    layer="database",
    severity="error",
    message="Connection timed out",
    vm_id="vm-01",
    database_id="db-01",
    provenance=Provenance(
        kind="observed",
        source="aiops2025",
        record_id="row-42",
    ),
)

payload = event.model_dump_json()
same_event = NormalizedEvent.model_validate_json(payload)
```

The Python definitions live in `backend/src/cloudrca_backend/contracts.py`. Versioned JSON Schema 2020-12 files live in `schemas/v1.0.0/` and are compatible with OpenAPI 3.1 schema objects. Regenerate them after an intentional contract change:

```bash
PYTHONPATH=backend/src .venv/bin/python scripts/export_contract_schemas.py
```

Run the contract tests with:

```bash
.venv/bin/pytest backend/tests/test_contracts.py
```
