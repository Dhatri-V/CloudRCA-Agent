# CloudRCA-Agent

CloudRCA-Agent is a local Python application for evidence-backed, cross-layer cloud incident analysis. Upload a supported log dataset, follow its analysis job, inspect incident evidence, and review the resulting RCA report in a Streamlit dashboard.

## What it does

```text
uploaded JSON / JSONL logs
  -> normalization and layer routing
  -> A1 database, A2 VM, and A3 hypervisor specialists
  -> orchestration and incident grouping
  -> temporal, topology, and cross-layer correlation
  -> main RCA reasoning and report
  -> FastAPI persistence/query API and Streamlit dashboard
```

Specialist observations, hypotheses, components, time ranges, and evidence are validated before they can become part of an incident or report. The dashboard reuses the ingestion/analysis and incident-query APIs; it does not replace or duplicate the backend workflow.

## Technology stack

- Python 3.12
- FastAPI and Pydantic v2 for the local backend API and contracts
- SQLite for the local persistence implementation, with the selected retrieval/storage interfaces
- Qwen-compatible specialist runtime for A1/A2/A3 and the configured main RCA-model integration
- Python RAG/retrieval, orchestration, incident grouping, and cross-layer correlation components
- Streamlit and Plotly for the local dashboard and visualizations
- pytest, Ruff, and mypy for validation

The active application and CI are Python-only. Running the project does **not** require Docker, containers, Docker Compose, React, Node.js, Vite, or TypeScript. The historical `frontend/` source and older decision records are retained as project history, but are not part of the supported local runtime.

## Input data

Use the dashboard to upload AIOps2025-shaped JSON or JSONL log/metric records (up to 10 MiB). Records the normalizer does not recognize are counted as rejected and reported in the job summary. Logs/datasets are uploaded to this system; there is no live AWS or CloudWatch ingestion. Keep credentials, production logs, and personally identifiable data out of the repository.

The committed fixtures are sanitized, reduced examples for deterministic tests. Dataset provenance and compatibility notes are in [docs/dataset-validation](docs/dataset-validation).

## Local installation

From a clean checkout, create and populate a project-local Python environment:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install uv
.venv/bin/uv sync --extra dev
cp .env.example .env
```

Configure `.env` for the services you intend to use, then export it in each terminal before starting the API (`set -a; source .env; set +a`); the application reads the process environment and does not load `.env` itself. Do not commit it. At minimum, keep the local API/database settings suitable for your machine. Configure Qwen and main-RCA provider credentials and endpoints only when you intend to run a workflow that calls those providers; the repository does not supply credentials or make live provider claims.

See [.env.example](.env.example) for the available setting names and [docs/development.md](docs/development.md) for the concise developer setup.

## Run the local demo

Use two terminals from the repository root.

Terminal 1 starts the API, which runs the RCA pipeline for each analysis job:

```bash
set -a; source .env; set +a
.venv/bin/cloudrca-api
```

Analysis calls the configured Qwen specialists and the main-RCA model through JCode. The default main-RCA model is a free OpenRouter model (`OPENROUTER_API_KEY` in `.env`); Z.AI GLM-5.3 remains an optional alternative in `.env.example`. If the JCode binary, provider settings, or provider key are missing, the job finishes as `failed` with an error naming the missing setting; normalized events and candidate incidents are still stored for inspection.

Terminal 2 starts Streamlit:

```bash
.venv/bin/streamlit run streamlit_app.py
```

Open the local URL printed by Streamlit (normally `http://localhost:8501`). The dashboard connects to `http://localhost:8000` by default; set `CLOUDRCA_BACKEND_URL` in `.env` if the API is elsewhere.

### Short walkthrough

1. Open the **Upload and analysis** page and upload a JSON or JSONL fixture/dataset.
2. Submit it for analysis, then use the job status controls to follow, retry, or cancel the job. When it finishes, the page lists the job's event counts and incidents with their analysis outcomes.
3. Open an incident from the job results or **Incidents**, then **Evidence** to inspect that incident's findings, cited events, timeline, and RCA report. Incidents where specialists found no anomaly are labeled *insufficient evidence* and have no report.
4. Open the report view to review the RCA narrative and export it where offered.
5. Treat observations as directly evidenced and hypotheses as explicitly labeled inferences.

## Tests and evaluation

Run the full local validation suite:

```bash
.venv/bin/uv run pytest
.venv/bin/uv run ruff check backend src scripts tests streamlit_app.py
.venv/bin/uv run mypy backend/src src
git diff --check
```

Run the deterministic evaluation coverage scorecard with:

```bash
.venv/bin/uv run python scripts/run_evaluation.py
```

The scorecard reports which pipeline stages have deterministic fixture coverage. It deliberately does **not** claim root-cause accuracy, precision, recall, F1, live-provider latency, or model-token cost: there is no end-to-end labeled RCA ground truth in the repository and CI does not call live providers. Do not present those metrics without collecting the required labeled data and measurements.

## Limitations

- Input is uploaded data, not a live CloudWatch/AWS integration.
- Provider-backed analysis requires your own configured, reachable provider endpoint and credentials. Automated tests script the LLM responses from each prompt's input; they verify pipeline wiring and evidence validation, not live model behavior.
- Each job analyzes at most `CLOUDRCA_PIPELINE_MAX_INCIDENTS` incidents (default 10, highest severity first); the rest are stored as `not_analyzed` and the job is marked `partial`. Knowledge-base retrieval is not yet passed to the main RCA step.
- Jobs run in-process inside the API; a long LLM-backed job occupies that process until it finishes.
- The repository currently has no end-to-end labeled RCA ground truth, so it makes no accuracy, precision, recall, or F1 claim.
- The local persistence/runtime configuration is intended for demonstration and development; assess operational persistence, provider reliability, security, and scaling before production use.
- Evidence-backed output reduces unsupported claims, but reported hypotheses remain hypotheses rather than confirmed facts.

## Further documentation

- [Development setup](docs/development.md)
- [Dataset validation and provenance](docs/dataset-validation)
- [Architecture decisions](docs/architecture)
- [Evaluation coverage](evaluation/README.md)
