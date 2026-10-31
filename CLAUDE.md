# CloudRCA-Agent

Local Python app for evidence-backed, cross-layer cloud root cause analysis (RCA). Upload AIOps2025-shaped JSON/JSONL logs → FastAPI job runs the pipeline → Streamlit shows incidents, findings, evidence, and the RCA report.

## Architecture (backend/src/cloudrca_backend/)

- `pipeline.py` — `RcaPipeline`: the production job runner. load dataset → `normalization.py` → `routing.py` → `incident_grouping.py` → per incident `orchestration.py` (A1/A2/A3) → `correlation.py` → `main_rca_agent.py` → `reporting.py`.
- Specialists: `database_agent.py` (A1), `vm_agent.py` (A2), `hypervisor_agent.py` (A3). All LLM calls go through `specialist_runtime.py` (`SpecialistRuntime`, `JCodeCliTransport` → JCode CLI → OpenAI-compatible endpoint).
- `contracts.py` — Pydantic contracts (three layers: database, vm_guest_os, host_hypervisor). Every finding/report must cite evidence IDs from the input; validators reject unknown IDs.
- `api.py` — FastAPI + SQLite job/artifact store (`cloudrca-api`). `dashboard.py` + `streamlit_app.py` — UI.
- LLM config is env-only: `CLOUDRCA_QWEN_*` (specialists), `CLOUDRCA_GLM_*` (main RCA; name is historical: default is OpenRouter `nvidia/nemotron-3-ultra-550b-a55b:free`, Z.AI GLM optional). Provider profile names must start with `cloudrca-` (JCode reserves built-in names).

## Commands (from repo root)

```bash
.venv/bin/uv run pytest                                            # full suite
.venv/bin/uv run ruff check backend src scripts tests streamlit_app.py
.venv/bin/uv run mypy backend/src src
.venv/bin/uv run python scripts/run_evaluation.py                  # deterministic scorecard
set -a; source .env; set +a; .venv/bin/cloudrca-api                # API on :8000 (.env is not auto-loaded)
.venv/bin/streamlit run streamlit_app.py                           # dashboard on :8501
```

Tests use committed fixtures (`tests/fixtures/aiops2025/`) and the scripted JCode double in `backend/tests/conftest.py`; they never call live LLMs.

## GitHub workflow

- Never work on `main`. One branch per logical change (`feature/…`, `fix/…`, `docs/…`).
- Before committing: pytest, ruff, mypy all pass. Do not weaken or delete tests to pass.
- Commit only relevant files → push → PR to `main` (use `.github/pull_request_template.md`) → merge (merge commit) only after the `backend` CI check passes and there are no blocking review comments → sync local `main`.
- No force-push to `main`, no bypassing checks.

## Security and constraints

- Never commit `.env`, API keys, or credentials; never print key values. Keys live only in `.env` (gitignored) and are referenced by variable name.
- Do not fabricate LLM outputs, findings, or test results. Label scripted/mocked results as such.
- Ask before paid API calls, AWS changes, or new dependencies. No Docker, no LangGraph.
- Leave untracked user files (`data/cloudrca.sqlite3`, `demo_aiops2025.jsonl`) alone.
