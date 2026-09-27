# AIOps2025 compatibility report

## Decision

| CloudRCA requirement | Result |
|---|---|
| Host / Hypervisor | **UNSUPPORTED** |
| VM / Guest OS | **PARTIAL** |
| Database | **PARTIAL** |
| VM → Database topology | **PARTIAL** |
| Host → VM topology | **NOT AVAILABLE** |

**Required augmentation: Outcome B.** Use a synthetic Host layer above real worker VMs, retain the verified real Redis→VM mapping, and add an explicitly marked augmentation for TiDB/TiKV/PD→VM placement. AIOps2025 must not be described as providing a complete real VM→Database topology.

## Evidence boundary

“Documented” below means stated by the official documentation. “Verified” means observed in downloaded records. No relationship was accepted based only on similar names, simultaneous timestamps, or the deployment diagram.

## Inspected data

- Two official abnormal cases: service memory stress on `adservice` and service-level pod failure on `recommendationservice`.
- One day of official normal metrics, including TiDB, TiKV, and PD metrics.
- Full 400-record `groundtruth.jsonl` and 400-record `input.json`.
- 243 Parquet files, 3,579,225 telemetry rows, plus 802 JSON/JSONL records.
- 62,264,719 extracted telemetry bytes; 46,936,693 compressed telemetry bytes.
- Metrics, logs, traces, labels, node records, pod records, Redis records, TiDB SQL-layer records, TiKV records, and PD records.

The machine-readable outputs are `reports/aiops2025_sample_profile.json` and `reports/aiops2025_topology_evidence.json`.

## Raw-schema findings

### Formats and encodings

- Telemetry is Parquet with typed nested trace columns.
- Labels and inputs are UTF-8-compatible JSON/JSONL.
- All 243 Parquet files loaded successfully; no malformed file was found.
- Trace fields include `traceID`, `spanID`, `references`, `startTime`, `startTimeMillis`, `duration`, `tags`, `logs`, and a nested `process` object.

### Time

- Metric and log timestamps are typed `Datetime(ns, UTC)` in the sampled Parquet records.
- Trace `startTimeMillis` is Unix epoch milliseconds and converts deterministically to UTC.
- Ground-truth `start_time` and `end_time` values end in `Z`.
- Official archive and hourly filenames use CST/UTC+8 dates and hours even though record timestamps use UTC. Consumers must never parse filenames as UTC.
- Data from different modalities can be correlated after treating record timestamps as authoritative.

### Missing values and duplicates

- No unreadable Parquet file was found.
- Log message values were physically null in 5,319 of 124,751 case-1 rows and 6,609 of 406,974 case-2 rows.
- The two log files contained 74 and 960 exact duplicate rows respectively. No duplicates were observed in the representative metric and trace files inspected during analysis.
- Many unused metric fields contain the literal string `"null"` instead of Arrow null. The profiler reports both physical `null_counts` and combined `missing_counts` so these placeholders are visible.
- The inspected telemetry contains 1,034 exact duplicate rows overall.

### Severity and messages

- Logs contain a `message` field but no normalized top-level severity column.
- Some application messages contain embedded structured severity values; extracting them requires later parsing and cannot be assumed for every component.
- Redis log messages are present. No TiDB/TiKV/PD pod logs were found in the two abnormal sample log files.

## Layer validation

### Host / Hypervisor — UNSUPPORTED

No physical-host ID, hypervisor ID, hypervisor event, VM-host placement edge, or virtualization telemetry was found. Kubernetes nodes are not reclassified as physical hosts.

### VM / Guest OS — PARTIAL

**Documented:** the system uses eight Kubernetes worker VMs.

**Verified:** pod metrics and logs use worker names such as `aiops-k8s-01` and `aiops-k8s-08`. The sample contains seven named workers (`01`, `03`–`08`); `aiops-k8s-02` is absent. Node metrics cover CPU, memory, filesystem, disk, network, and TCP sockets.

**Limitation:** node metric rows identify nodes using eleven IP-address values, while pod/log placement uses `aiops-k8s-*` names. No raw mapping between those two namespaces was found. OS version, VM UUID, boot ID, and explicit guest/hypervisor metadata are absent. The worker-node layer is usable as a VM proxy, but it is not a complete guest-OS inventory.

### Database — PARTIAL

**Redis verified:** `redis-cart-0` appears in APM metrics, pod infrastructure metrics, logs, and traces (`process.serviceName = redis`).

**TiDB verified:** SQL-layer metrics include connection count, latency percentiles, QPS, slow-query/cache-related data, CPU, memory, uptime, and transaction retry information. TiKV and PD metrics are present in the `other` metric collection.

**Limitations:** TiDB/TiKV/PD logs were not present in the abnormal sample; traces did not expose TiDB components; TiDB metrics identify endpoints such as `10.233.79.158:10080`, TiKV metrics use endpoints such as `10.233.85.101:20180`, and PD frequently uses `"null"` as its instance. These records do not identify a database pod or worker node.

## VM → Database topology

### Redis: VERIFIED

The same real pod-metric rows contain:

```text
time: UTC timestamp
object_type: pod
pod: redis-cart-0
instance: aiops-k8s-08
```

Real log rows independently contain `k8_pod = redis-cart-0` and `k8_node_name = aiops-k8s-08`. This proves `redis-cart-0 → aiops-k8s-08` for the sampled periods without a name or timestamp guess.

### TiDB/TiKV/PD: NOT VERIFIED

Across 44 TiDB/TiKV/PD metric files and 244,630 rows, zero rows contained both a usable database pod ID and Kubernetes-node ID. TiDB endpoint IPs cannot be joined to node metric IPs merely because both look like addresses, and contemporaneous measurements do not establish placement.

Therefore the overall result is **VM → Database topology: PARTIAL**.

## Time-aware placement

Redis placement can be represented as:

```text
database_component_id = redis-cart
database_pod_id       = redis-cart-0
vm_node_id            = aiops-k8s-08
valid_from_utc         = first explicit observation
valid_to_utc           = last contiguous explicit observation
mapping_source         = source Parquet file
```

No Redis rescheduling was observed: the pod remained on `aiops-k8s-08` across all three sampled metric days and both abnormal log windows. This demonstrates a time-aware representation, but does not demonstrate a real rescheduling event. TiDB time-aware placement cannot be reconstructed from the selected release records.

## Incident and class validation

The real abnormal subset contains:

1. `adservice` service memory stress, 2025-04-26 19:10:15Z–19:40:15Z.
2. `recommendationservice` service-level pod failure, 2025-04-29 06:10:42Z–06:38:42Z.

Both have metrics, logs, traces, incident windows, and published evidence labels. Neither is a node- or database-rooted incident, so the sample does not prove a real Node→Database→Application causal chain.

The full raw ground truth verifies 400 cases: 195 service, 123 pod, and 82 node cases. Fault-type counts range from 13 to 45; the largest type is pod failure (45) and the smallest are JVM CPU and JVM exception (13 each). The labels include TiDB/TiKV/PD and Redis incidents, but validating their telemetry would require downloading at least one 419–769 MB daily archive.

## Topology and identifier limitations

- Physical host/hypervisor identifiers are absent.
- Only seven of the documented eight worker names occur in the selected records.
- Node-metric IPs are not mapped to `aiops-k8s-*` worker names.
- Redis placement is explicit; TiDB/TiKV/PD placement is absent.
- TiDB component identifiers are endpoint addresses rather than Kubernetes pod IDs.
- No supplied topology file exists in AIOps2025.
- Dynamic placement is observable only when pod metrics or logs repeatedly co-record pod and node.
- File names use CST while record timestamps use UTC.

## Augmentation strategy

Use **Outcome B**:

```text
Synthetic Host
      ↓ explicit synthetic Host→VM edge
Real Kubernetes worker VM
      ↓ real Redis placement / augmented TiDB placement
Real database telemetry
```

Rules for later augmentation:

1. Preserve all original AIOps2025 rows unchanged.
2. Mark every generated host event and TiDB placement edge with `synthetic = true`, provenance, generator version, scenario ID, and validity interval.
3. Keep verified Redis edges marked as real with their source file and observation interval.
4. Never derive TiDB placement from endpoint resemblance or temporal coincidence.
5. Evaluate real-only and augmented scenarios separately.

No synthetic dataset was generated in Issue #1.

## Acceptance assessment

- Canonical source, distribution, license, and checksums: **satisfied**.
- Small real subset selected without committing raw data: **satisfied**.
- Formats, schemas, counts, missing values, duplicates, timestamps, identifiers, metrics, logs, traces, and labels profiled: **satisfied**.
- Host, VM, and database compatibility classified: **satisfied**.
- VM→Database topology tested without unsupported inference: **satisfied; result is PARTIAL**.
- Time-aware mapping tested: **satisfied for Redis; unavailable for TiDB**.
- Representative node/TiDB incident telemetry: **not satisfied from the small sample**; obtaining it requires a 419–769 MB daily archive and was intentionally stopped under the large-download constraint.
- Sanitized fixtures, notebook, deterministic profiler, and automated tests: **satisfied**.

AIOps2025 remains usable only with the documented augmentation strategy. Final dataset approval should acknowledge that the real topology covers Redis→VM but not TiDB/TiKV/PD→VM.
