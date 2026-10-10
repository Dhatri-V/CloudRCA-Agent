from scripts.run_evaluation import TESTS


def test_deterministic_scorecard_covers_the_local_pipeline_boundaries() -> None:
    joined = " ".join(TESTS)
    for name in ("normalization", "routing", "orchestration", "correlation", "main_rca_agent", "reporting", "api", "dashboard"):
        assert name in joined
