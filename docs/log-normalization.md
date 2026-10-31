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

## CloudRCA records (application, database, VM/OS, EC2 CloudWatch)

Records with a `cloudrca_record` field are validated by `cloudrca_backend.cloudrca_records` instead of the AIOps2025 rules above (which are unchanged). Every record needs `timestamp` (ISO 8601 with a timezone) and `source`; `event_id` (for example a CloudWatch Logs event ID), `level`, `log_group`, and `log_stream` are optional and preserved. Unknown fields are kept in the provenance raw record.

| `cloudrca_record` | Layer | Required fields | Identifiers kept |
| --- | --- | --- | --- |
| `app.request_summary` | application | `service`, `instance_id`, `window_seconds`, `request_count` | service, `vm_id`; route and counts/durations as attributes |
| `app.error` + `reported_by: application` | application | `service`, `instance_id`, `error_class`, `message` | service, `vm_id`, request ID, route, method, status |
| `app.error` + `reported_by: database` | database (A1) | the above plus `database_id` | `database_id`, `vm_id`, request ID; the application is kept as `reporting_service` |
| `host.heartbeat` | vm_guest_os (A2) | `instance_id` and at least one of load, CPU count, memory, disk, or Splitwise R6 connection-pool measures | `vm_id` |
| `aws.cloudwatch.metric` | vm_guest_os (A2) | `namespace`, `metric_name`, numeric `value`, `dimensions.InstanceId` | `vm_id`; namespace, statistic, period, unit, all dimensions |

Normalization preserves evidence and never decides causes: the layer comes from the record type and the emitter's own `reported_by` statement, not from message content. Database-reported errors keep the application only as an attribute so it is never treated as a database-layer component. No hypervisor host is set because AWS does not expose one. Splitwise R6 pool measures retain their source meanings: `db_pool_size` is configured capacity, `db_pool_checked_out` is currently leased connections, and `db_pool_overflow` is additional connections above capacity. Missing fields are rejected as `MISSING_REQUIRED_FIELD`; wrong types, negative or inconsistent counts, booleans, unknown `reported_by` values, and missing `InstanceId` dimensions as `SCHEMA_VALIDATION_FAILED`; timestamps without a timezone as `INVALID_TIMESTAMP`. The same CloudWatch event exported twice is a duplicate; distinct event IDs are kept.
