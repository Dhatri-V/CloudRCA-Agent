# ADR 0001: JCode model compatibility path

- Status: Accepted for MVP spike
- Date: 2026-09-28
- Issue: #2
- Canonical project: <https://github.com/1jehuang/jcode>
- Pinned release: `v0.88.0`, commit `ee4cd3db3`

## Decision

Use JCode's OpenAI-compatible provider surface for Qwen specialist sessions and GLM-5.3 correlation sessions. Keep provider URLs and model IDs configurable. Use the headless CLI for the first Python MVP integration; adopt `@1jehuang/jcode-sdk` in a small Node process only when CloudRCA needs durable sessions, SDK cancellation, custom tools, or SDK-enforced structured-output retries.

This decision does not build the real agents. It validates the boundary they can use later.

## Status vocabulary

- **DOCUMENTED**: supported by official documentation or pinned source, but not executed here.
- **VERIFIED**: executed successfully in this repository's spike environment.
- **NOT RUN**: requires credentials, a model download, or provider access that was unavailable.

## Evidence

| Capability | Status | Evidence |
| --- | --- | --- |
| JCode installation and version | VERIFIED | Official macOS arm64 release archive checksum matched `58378d...36f2e`; `jcode version --json` returned `v0.88.0 (ee4cd3db3)`. |
| Non-interactive execution | VERIFIED | `jcode run --json` completed for separate Qwen and GLM model selections. |
| Custom OpenAI-compatible endpoint | VERIFIED | Isolated `provider add` profiles sent streaming requests to a local `/v1/chat/completions` endpoint. |
| Qwen model routing | VERIFIED (transport only) | JCode sent model `qwen3:smoke`; response envelope and usage parsed. This was not Qwen inference. |
| GLM-5.3 model routing | VERIFIED (transport only) | JCode sent model `glm-5.3`; response envelope and usage parsed. This was not GLM inference. |
| Qwen live inference | NOT RUN | Ollama 0.34.4 is installed but its service is stopped and no Qwen model is installed; no Alibaba credential was present. |
| GLM-5.3 live inference | NOT RUN | No Z.AI API credential was present. |
| Structured JSON | VERIFIED at application boundary | JCode's JSON envelope carried model JSON, and the runner rejects malformed JSON and schema/type mismatches. |
| SDK structured retries | DOCUMENTED | The TypeScript SDK exposes `runStructured` with bounded correction attempts; not executed because the CLI path is sufficient for this spike. |
| Errors and timeouts | VERIFIED | Deterministic tests cover nonzero exits, malformed output, schema failure, and subprocess timeout. |
| Cancellation | DOCUMENTED | The SDK protocol exposes session cancellation. The CLI spike uses a hard subprocess timeout; live mid-stream cancellation was not executed. |
| Three-specialist handoff | VERIFIED at boundary | Deterministic database, VM, and hypervisor findings are serialized into the GLM prompt and validated in tests. No model reasoning claim is made. |
| Tools and system prompts | DOCUMENTED | JCode supports tool profiles and SDK session system-prompt overrides. Spike calls disable tools. |
| Usage accounting | VERIFIED | JCode returned input/output token fields from the deterministic endpoint. |
| Latency, retries, concurrency | PARTIAL | Per-call timeout is enforced. Provider latency, retry behavior, rate limits, and concurrent live sessions require credentials and load tests. |

## Provider configuration

### Qwen

Preferred local development path:

- JCode provider: `ollama` or a named OpenAI-compatible profile
- Base URL: `http://localhost:11434/v1`
- Model: the exact installed Ollama Qwen model ID, selected after measuring available hardware
- Authentication: none for localhost

Preferred hosted path:

- Alibaba Model Studio OpenAI-compatible base URL for the account's region and workspace, ending in `/compatible-mode/v1`
- API key supplied through an environment variable such as `DASHSCOPE_API_KEY`
- Model ID selected from the account's current supported Qwen models; `qwen3-coder-next` is a realistic coding-plan candidate but is not pinned until a live account test succeeds

### GLM-5.3

- Official model ID: `glm-5.3`
- Standard API base URL: `https://api.z.ai/api/paas/v4`
- Coding Plan OpenAI-compatible base URL: `https://api.z.ai/api/coding/paas/v4`
- Credential environment variable: `ZHIPU_API_KEY`
- Context window: 1,000,000 tokens; maximum output documented as 128,000 tokens
- Reasoning is always enabled; allowed effort values are `low`, `high`, and `max`

JCode `v0.88.0` has a built-in `zai` profile pointing at the Coding Plan endpoint, but its pinned default model remains `glm-4.5`. CloudRCA must explicitly select `glm-5.3`. The generic OpenAI-compatible profile is also valid and makes the endpoint/model selection explicit.

## Interface comparison

| Interface | Fit for CloudRCA |
| --- | --- |
| Headless `jcode run --json` | Smallest Python MVP path. Provides model selection, isolated profiles, JSON envelope, usage, and a process boundary. Application code must validate model JSON and enforce timeouts. |
| TypeScript SDK + `jcode api-bridge` | Strongest programmatic path for long-lived sessions, cancellation, custom tools, events, and `runStructured` retries. Adds Node and a small bridge process. |
| Rust SDK | Native and typed, but would add a Rust application boundary to a Python/FastAPI project. |
| ACP/TUI | Useful for interactive coding, not the service integration boundary. |

## Consequences and unsupported assumptions

- JCode is compatible with the planned architecture at its OpenAI-compatible transport boundary.
- Model quality, tool-call quality, provider authentication, real latency, rate limits, token costs, retries, and concurrent throughput remain unverified until live credentials or a local Qwen model are available.
- `jcode run --json` makes the outer response machine-readable; it does not by itself guarantee that model text satisfies CloudRCA's JSON Schema. The committed runner validates the inner JSON and fails closed.
- The CLI currently labels named OpenAI-compatible results as provider `openrouter` in its JSON envelope. Routing was verified from the captured endpoint and model request, so CloudRCA must key audit records by configured profile and model rather than that display label.
- Do not pin a Qwen size until deployment memory and latency are measured. Do not substitute a similarly named GLM model for `glm-5.3`.

## Reproduction

Install the exact JCode release separately, then run against credentialed providers without putting keys on the command line:

```bash
.venv/bin/python scripts/run_jcode_compatibility_smoke.py \
  --jcode /path/to/jcode-v0.88.0 \
  --qwen-base-url 'https://WORKSPACE.REGION.maas.aliyuncs.com/compatible-mode/v1' \
  --qwen-model qwen3-coder-next \
  --qwen-api-key-env DASHSCOPE_API_KEY \
  --glm-base-url https://api.z.ai/api/paas/v4 \
  --glm-model glm-5.3 \
  --glm-api-key-env ZHIPU_API_KEY
```

The runner creates an isolated temporary `JCODE_HOME`, disables JCode telemetry, references credentials only by environment-variable name, disables tools, validates both outputs, and deletes the temporary profile and sessions afterward.

