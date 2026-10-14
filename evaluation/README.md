# Deterministic evaluation

Run the CI-safe scorecard with:

```bash
.venv/bin/python scripts/run_evaluation.py
```

It exercises deterministic normalization, routing, specialist orchestration, correlation, main-RCA validation, reporting, API, and Streamlit-client regression tests, plus the production pipeline runner end to end (upload → API job → artifacts → dashboard). LLM responses in these tests are scripted from each prompt's own input; it does not call an LLM provider, so it validates wiring and evidence checks, not model quality.

The committed fixtures do not contain labeled end-to-end root-cause answers. Consequently root-cause accuracy, precision, recall, F1, live-provider latency, token cost, and success rates are deliberately not reported. Live-provider evaluation remains opt-in and must use a separately supplied, labeled corpus and credentials.
