# AIOps2025 log normalization

Issue #5 converts the real record shapes confirmed by the Issue #1 investigation into the canonical `NormalizedEvent` from Issue #4.

## Supported records

- Parquet log rows with `@timestamp`, `message`, and Kubernetes fields. Only the verified Redis database logs are accepted at this stage; application logs are quarantined because Issue #6 owns layer routing.
- Parquet node metrics with an authoritative UTC `time` and `object_type = node`.
- Parquet database and database-pod metrics for the confirmed Redis, TiDB, TiKV, and PD component names.
- Parquet trace spans using `startTimeMillis`; confirmed database services such as Redis can become events.
- UTF-compatible JSON and JSONL are read record by record. AIOps2025 incident labels are preserved but quarantined because labels are evaluation data rather than infrastructure events.

The pipeline does not infer host data, TiDB placement, or application-layer routing. Literal missing markers found in Issue #1 (`""`, `"null"`, `"none"`, and `"nan"`) are not emitted as identifiers.

## Example

An abbreviated source row:

```text
@timestamp: 2025-06-01T18:30:00+08:00
k8_pod: redis-cart-0
k8_node_name: aiops-k8s-08
message: " Redis timeout "
```

becomes:

```text
timestamp: 2025-06-01T10:30:00Z
layer: database
severity: unknown
message: "Redis timeout"
pod_id/database_id: redis-cart-0
vm_id: aiops-k8s-08
```

The source has no dependable top-level severity, so `unknown` was added to the existing severity enum. This is the only contract change. The original timestamp, raw row, file, row number, and source format remain in event metadata and provenance.

## Rejection and duplicate handling

Naive timestamps are rejected unless the caller supplies a documented IANA timezone. Ambiguous and nonexistent daylight-saving times receive separate reason codes. Malformed input, missing fields, invalid severity, schema failures, and unsupported records are also retained in the rejected stream.

Each valid logical event gets a SHA-256 fingerprint made from normalized event fields and relevant source attributes. The fingerprint is stable across runs and does not depend on the file location or Python's runtime hash. A repeated fingerprint is retained in the duplicate stream.

Every batch reports counts that must reconcile:

```text
input:      3
accepted:   1
duplicates: 1
rejected:   1
```

Use the pipeline from Python:

```python
from cloudrca_backend.normalization import normalize_file

batch = normalize_file("data/extracted/aiops2025/logs.parquet")
print(batch.statistics)
```
