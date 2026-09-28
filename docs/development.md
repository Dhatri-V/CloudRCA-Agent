# Development setup

CloudRCA uses Python 3.12 with `uv` and Node.js 22 LTS with npm.

## Install

```bash
uv sync --extra dev
cd frontend
npm ci
```

Copy `.env.example` to `.env` when running application code. The supported profiles are `development`, `test`, and `demo`. Do not commit `.env`.

## Backend checks

Run these commands from the repository root:

```bash
uv run ruff check backend src scripts tests
uv run mypy backend/src src
uv run pytest
```

## Frontend checks

Run these commands from `frontend/`:

```bash
npm run lint
npm run format:check
npm test
```
