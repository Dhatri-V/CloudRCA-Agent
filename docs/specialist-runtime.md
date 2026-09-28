# Shared specialist runtime

Issue #7 provides one execution path for the future Database, VM, and Hypervisor Qwen specialists. It supplies runtime machinery only; specialist diagnosis rules belong to Issues #8–#10.

```text
Routed canonical events + topology
              ↓
deterministic versioned prompt
              ↓
isolated temporary JCode session
              ↓
configured Qwen provider/model
              ↓
validated canonical Finding
```

## Execution

Each call creates a new temporary `JCODE_HOME`, configures the named OpenAI-compatible provider, disables JCode tools and telemetry, and invokes the Issue #2 `jcode run --json` interface. Provider profile, URL, model, credential environment-variable name, timeout, prompt size, event count, repair attempts, and prompt version come from typed configuration. Credential values are never stored in the runtime configuration or passed on the command line.

The prompt contains the specialist layer, canonical event fields, routing result, supported topology, concise provenance, prompt version, and the existing `Finding` JSON Schema. Raw source records are excluded. Identical inputs and a matching prompt version create identical prompts.

The response must validate as the Issue #4 `Finding`, use the requested specialist layer, and reference only input events or topology evidence. Invalid JSON or schema content receives at most the configured number of repair attempts in the same isolated execution. Exhausted repairs raise a typed `invalid_structured_output` failure.

Timeouts, JCode/provider errors, configuration failures, oversized batches, and oversized prompts also raise typed failures. No input is silently truncated.

## Traceability

Every run records structured metadata with structlog:

- correlation ID and specialist layer
- provider profile and model
- prompt version
- JCode session ID
- available input/output token counts
- duration and repair count
- success or typed error information

Prompts, raw records, and credentials are not logged.

## Configuration

Copy `.env.example` and set the Qwen values for the installed local model or credentialed OpenAI-compatible provider. Load the runtime config through the existing application settings:

```python
from cloudrca_backend import JCodeCliTransport, SpecialistRuntime, load_specialist_runtime_config

runtime_config = load_specialist_runtime_config(settings)
runtime = SpecialistRuntime(runtime_config, JCodeCliTransport())
result = runtime.run(request)
```

Automated tests use a fake JCode transport. No live Qwen inference, model download, Ollama service, credentials, or network request is required or claimed.
