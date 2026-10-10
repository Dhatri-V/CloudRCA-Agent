# API and job persistence

Issue #18 adds a local FastAPI service with a durable SQLite job store. Start it with:

```bash
cloudrca-api
```

Set `CLOUDRCA_API_DATABASE` to choose the database path. The service listens on `http://127.0.0.1:8000`; its interactive schema is at `/docs`.

- `POST /api/v1/analysis-jobs` queues an analysis.
- `GET /api/v1/analysis-jobs/{job_id}` reads its durable state.
- `POST /api/v1/analysis-jobs/{job_id}/artifacts?kind=report` stores a JSON event, finding, incident, graph, report, or run record.
- `POST /api/v1/analysis-jobs/{job_id}/uploads?filename=logs.txt` stores raw request-body bytes and returns an upload id; `GET .../uploads/{upload_id}` downloads them.

`/healthz` only means the process is serving. `/readyz` separately reports database, JCode-binary, and GLM-key availability without returning any secret. Startup runs the versioned SQLite schema migration. Jobs run in-process; use an external worker before relying on this API for multi-process or high-volume production workloads.
