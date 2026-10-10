# Development setup

CloudRCA-Agent is developed and run locally with Python 3.12 and `uv`. The supported application consists of the FastAPI backend and the Streamlit dashboard; it has no Node.js, React, Vite, TypeScript, Docker, or container runtime requirement.

## Install

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install uv
.venv/bin/uv sync --extra dev
cp .env.example .env
```

Load or configure the example settings in your shell before running application code. The supported profiles are `development`, `test`, and `demo`. Do not commit `.env` or provider credentials.

## Run locally

Start the backend in one terminal:

```bash
.venv/bin/cloudrca-api
```

Start the dashboard in another:

```bash
.venv/bin/streamlit run streamlit_app.py
```

The dashboard uses `CLOUDRCA_BACKEND_URL` (default `http://localhost:8000`) to reach the existing backend APIs.

## Quality checks

Run these commands from the repository root:

```bash
.venv/bin/uv run ruff check backend src scripts tests streamlit_app.py
.venv/bin/uv run mypy backend/src src
.venv/bin/uv run pytest
git diff --check
```

For deterministic evaluation coverage, run:

```bash
.venv/bin/uv run python scripts/run_evaluation.py
```
