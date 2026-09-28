# Development setup

CloudRCA uses Python 3.12 with `uv` and Node.js 22 LTS with npm.

## Install

```bash
uv sync --extra dev
npm --prefix frontend ci
```

Copy and load the example settings in your shell before running application code:

```bash
cp .env.example .env
set -a
. ./.env
set +a
```

The supported profiles are `development`, `test`, and `demo`. Do not commit `.env`.

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
