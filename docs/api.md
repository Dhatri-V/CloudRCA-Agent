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

`cloudrca-api` runs the production RCA pipeline (`cloudrca_backend.pipeline`) for every analysis job: dataset loading → normalization → routing → incident grouping → A1/A2/A3 specialists per incident → cross-layer correlation → main RCA → report validation and rendering. Its run summary, events, incidents, findings, correlation graphs, and reports are stored as job artifacts in one transaction and served by the existing incident, finding, evidence, graph, report, and job-artifact endpoints.

Job status reflects what was actually analyzed: `completed` when every incident received a validated report or was explicitly marked `insufficient_evidence` (specialists found no anomaly, so no root cause is inferred); `partial` when some incidents failed or exceeded `CLOUDRCA_PIPELINE_MAX_INCIDENTS`; `failed` when the dataset has no supported events, the LLM runtime is not configured, or no incident could be analyzed. The job `error` field gives a safe reason with error codes only. Each incident payload carries its `analysis_outcome`.

`/healthz` only means the process is serving. `/readyz` separately reports database, JCode-binary, and main-RCA provider-key availability without returning any secret. Startup runs the versioned SQLite schema migration. Jobs run in-process; use an external worker before relying on this API for multi-process or high-volume production workloads.
