# Deterministic evaluation

Run the CI-safe scorecard with:

```bash
.venv/bin/python scripts/run_evaluation.py
```

It exercises deterministic normalization, routing, specialist orchestration, correlation, main-RCA validation, reporting, API, and Streamlit-client regression tests. It does not call an LLM provider.

The committed fixtures do not contain labeled end-to-end root-cause answers. Consequently root-cause accuracy, precision, recall, F1, live-provider latency, token cost, and success rates are deliberately not reported. Live-provider evaluation remains opt-in and must use a separately supplied, labeled corpus and credentials.
