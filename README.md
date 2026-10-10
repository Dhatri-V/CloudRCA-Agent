# Agentic Cloud RCA

A multi-agent system for **cross-layer cloud root cause analysis (RCA)**.

The system analyzes cloud infrastructure logs across multiple layers, identifies errors within each layer, correlates related failures across time, and determines the probable root-cause chain behind an incident.

## 🎯 Objective

Cloud failures are often visible in one layer while their actual cause originates in another.

For example:

```text
Hypervisor I/O degradation
        ↓
VM resource pressure
        ↓
Database slowdown
        ↓
Database connection timeout
```

Instead of analyzing each error independently, this project aims to identify these **cross-layer relationships** using a multi-agent architecture.

---

## 🏗️ Planned Architecture

```text
                   CLOUD LOG DATASET
                          │
                          ▼
                 Log Parser / Cleaner
                          │
                          ▼
              Timestamp Normalization
                       → UTC
                          │
                          ▼
                     Layer Router
                          │
              ┌───────────┼───────────┐
              ▼           ▼           ▼
             A1          A2          A3
          Database       VM       Hypervisor
            Agent       Agent        Agent
           (Qwen)      (Qwen)       (Qwen)
              │           │           │
              └───────────┼───────────┘
                          ▼
                     MAIN AGENT
                      GLM-5.3
                          │
                          ▼
                Temporal Correlation
                          │
                          ▼
               Cross-Layer Reasoning
                          │
                          ▼
                  Root Cause Chain
                          │
                          ▼
                   Incident Report
```

The multi-agent system will operate using **JCode as the agent harness**, providing the environment for agent coordination, tools, communication, context, and execution.

---

## 🤖 Multi-Agent Design

### A1 — Database Agent
Analyzes database-layer logs and extracts:
- Database errors
- Timestamp
- Severity
- Error type
- Possible database-level causes

### A2 — VM Agent
Analyzes guest OS / virtual machine logs and extracts:
- Resource problems
- Memory/CPU issues
- OS-level failures
- Timestamp
- Possible VM-level causes

### A3 — Hypervisor Agent
Analyzes host/hypervisor logs and extracts:
- Host resource issues
- Virtualization failures
- I/O problems
- Timestamp
- Possible infrastructure-level causes

### Main RCA Agent

The main agent receives findings from A1, A2, and A3 and performs:

- Temporal correlation
- Cross-layer event correlation
- Causal reasoning
- Root-cause hypothesis generation
- Evidence-chain construction

---

## 🧠 Model Architecture

```text
Qwen
├── A1 Database Agent
├── A2 VM Agent
└── A3 Hypervisor Agent

              ↓

GLM-5.3
└── Main Cross-Layer RCA Agent

              ↓

Incident Report
```

**JCode** acts as the harness coordinating the multi-agent workflow.

---

## 🕐 Timestamp Normalization

Logs may originate from systems using different time zones.

Before agent analysis, timestamps are normalized to **UTC** so events from different infrastructure layers can be accurately correlated.

```text
Raw Logs
   ↓
Parse Timestamp
   ↓
Convert → UTC
   ↓
Layer-specific analysis
```

---

## 📄 Expected Incident Report

The final system should produce structured incident reports containing:

```text
Incident
│
├── Detected Errors
│   ├── Database
│   ├── VM
│   └── Hypervisor
│
├── Event Timeline
│
├── Cross-Layer Relationships
│
├── Probable Root Cause
│
├── Causal Chain
│
└── Supporting Evidence
```

---

## 🛠️ Planned Tech Stack

**Agent Harness**
- JCode

**LLMs**
- Qwen — specialist agents
- GLM-5.3 — main RCA agent

**AI / Retrieval**
- RAG
- Vector database

**Backend**
- Python
- FastAPI

**Infrastructure**
- Docker

**Frontend**
- Web-based incident analysis dashboard

---

## 🚧 Project Status

Currently under development.

Development is being tracked through GitHub Issues, with each component implemented and tested independently before end-to-end integration.

## Dataset validation

Issue #1 validates AIOps2025 without committing its large raw archives. The compatibility decision and provenance are documented in:

- [`docs/dataset-validation/aiops2025-compatibility.md`](docs/dataset-validation/aiops2025-compatibility.md)
- [`docs/dataset-validation/provenance.md`](docs/dataset-validation/provenance.md)

Create a Python 3.12 environment and install the validation dependencies:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Profile an extracted subset and reproduce topology evidence:

```bash
.venv/bin/python scripts/validate_aiops2025_subset.py data/extracted/aiops2025 \
  --profile-output reports/aiops2025_sample_profile.json \
  --topology-output reports/aiops2025_topology_evidence.json
```

Raw data belongs under `data/raw/` and extracted data under `data/extracted/`; both paths are ignored by Git. The committed fixtures are reduced and sanitized derivatives for deterministic testing only.

## JCode and model compatibility

Issue #2 pins JCode `v0.88.0` and records the Qwen/GLM-5.3 compatibility decision in [`docs/architecture/0001-jcode-model-compatibility.md`](docs/architecture/0001-jcode-model-compatibility.md). The included smoke runner uses isolated temporary configuration, accepts credentials only through named environment variables, and validates structured model output. Live provider calls are optional and were not claimed without credentials.

## Development

See [`docs/development.md`](docs/development.md) for the short clean-checkout setup and quality commands.

The shared event, topology, finding, incident, and RCA data formats are documented in [`docs/domain-contracts.md`](docs/domain-contracts.md).

The supported AIOps2025 parsing, UTC conversion, deduplication, and quarantine behavior is documented in [`docs/log-normalization.md`](docs/log-normalization.md).

Deterministic layer routing and evidence-constrained topology behavior are documented in [`docs/layer-routing-topology.md`](docs/layer-routing-topology.md).

The isolated JCode/Qwen execution contract shared by future specialist agents is documented in [`docs/specialist-runtime.md`](docs/specialist-runtime.md).

The versioned API, local job persistence, readiness checks, and upload contract are documented in [`docs/api.md`](docs/api.md).
