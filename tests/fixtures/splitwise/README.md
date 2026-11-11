# Splitwise CloudRCA records

**Synthetic, hand-written test data** (messages start with `SYNTHETIC:`), not output from a real deployment. `cloudrca_records.jsonl` contains one example of each CloudRCA record type (`aws.cloudwatch.metric`, `host.heartbeat`, `app.request_summary`, and `app.error` reported by the database and by the application) for instance `i-0synthetic01`. Its heartbeat includes the real Splitwise R6 integer pool fields: configured capacity (`db_pool_size`), leased connections (`db_pool_checked_out`), and connections above capacity (`db_pool_overflow`). It exercises normalization and routing only; it encodes no expected root cause. No personal data, SQL parameters, or request bodies.

`export/` holds **synthetic** converter inputs for `scripts/convert_cloudwatch_export.py`: a Splitwise JSON-lines application log (`app_log.jsonl`, including deliberately malformed, out-of-window, and private-looking lines that the converter must skip or drop), an `aws logs filter-log-events` result (`logs_cli.json`), and an `aws cloudwatch get-metric-data` result with its queries (`metric_data.json`, `metric_queries.json`). The `SYNTHETIC-PRIVATE-*` values exist only to prove such fields never reach converter output.

`real_baseline_healthy.jsonl` is **derived from a real collection**: a healthy 21-minute baseline (2026-11-11 17:26–17:47 UTC) gathered from the Splitwise EC2 deployment with `scripts/collect_splitwise_incident.py` and the read-only collector role. The only edit is the EC2 instance ID, which is replaced with `i-0baseline0000001` everywhere. The values are real measurements:
- 10 basic CloudWatch metrics
- 21 heartbeats
- 21 pool snapshots
- 20 per-minute request summaries covering 39 `GET /expenses` requests

It contains no errors, no account IDs, no IP addresses, no hostnames, and no request bodies or personal data. The raw application log and the collection manifest are not committed. Use it as the healthy, no-anomaly control dataset.
