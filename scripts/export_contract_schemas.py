#!/usr/bin/env python3
"""Export versioned CloudRCA Pydantic contracts as JSON Schema 2020-12."""

from __future__ import annotations

import json
from pathlib import Path

from cloudrca_backend.contracts import CONTRACT_VERSION, SCHEMA_MODELS, contract_json_schema


def main() -> int:
    destination = Path("schemas") / f"v{CONTRACT_VERSION}"
    destination.mkdir(parents=True, exist_ok=True)
    for name, model in SCHEMA_MODELS.items():
        path = destination / f"{name}.schema.json"
        path.write_text(json.dumps(contract_json_schema(name, model), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
