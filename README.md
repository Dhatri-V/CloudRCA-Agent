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
