"""Run the deterministic CloudRCA evaluation suite without live providers."""

from __future__ import annotations

import json

import pytest

TESTS = (
    "backend/tests/test_normalization.py",
    "backend/tests/test_routing.py",
    "backend/tests/test_orchestration.py",
    "backend/tests/test_correlation.py",
    "backend/tests/test_main_rca_agent.py",
    "backend/tests/test_reporting.py",
    "backend/tests/test_api.py",
    "backend/tests/test_dashboard.py",
)


def main() -> int:
    """Execute only deterministic tests and emit an honest, reproducible scorecard."""
    result = pytest.main(["-q", *TESTS])
    scorecard = {
        "mode": "deterministic local regression",
        "pytest_exit_code": result,
        "covered_stages": ["normalization", "routing", "orchestration", "correlation", "main_rca_validation", "reporting", "api", "streamlit_client"],
        "not_measured": ["root_cause_accuracy", "precision", "recall", "f1", "live_provider_latency", "model_token_cost"],
        "reason": "The committed fixtures do not provide labeled end-to-end RCA ground truth and CI does not call live providers.",
    }
    print(json.dumps(scorecard, indent=2, sort_keys=True))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
