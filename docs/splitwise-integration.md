# Splitwise on EC2: on-demand cross-layer RCA

CloudRCA analyses Splitwise failures on demand: collect a window of application logs and EC2 metrics, convert them into CloudRCA records, and upload the file in the dashboard. No CloudWatch agent or new AWS resources are required.

```text
Splitwise JSON-lines log (pulled from the instance, or a CloudWatch Logs export)
  + aws cloudwatch get-metric-data (basic EC2 metrics, read-only)
  -> scripts/convert_cloudwatch_export.py -> CloudRCA JSONL
  -> Streamlit upload -> normalization -> A1/A2 + application evidence -> correlation -> main RCA
```

## Splitwise log lines

One JSON object per line with `event`, a timezone-aware `ts`, `service`, and `instance_id`:

| `event` | Fields the converter uses |
| --- | --- |
| `request` | `request_id`, `method`, `route` (template, e.g. `/expenses/{expense_id}`), `status`, `duration_ms`, `db_ms`, `is_write`, `in_flight` |
| `error` | `reported_by` (`application` or `database`, required), `error_class`, `message`, `request_id`, `method`, `route`, `status`, `database_id` (required when `reported_by=database`), `operation`, `duration_ms`, `db_wait_ms` |
| `heartbeat` | `load_1m`, `load_5m`, `cpu_count`, `mem_available_mb`, `mem_total_mb`, `disk_free_mb`, `disk_total_mb` |

Logs must not contain request bodies, SQL parameters, credentials, or personal data (titles, amounts, names). The converter copies only the fields above, so anything else is dropped even if logged by mistake.

## Collect (read-only, after you start the instance yourself)

```bash
aws cloudwatch get-metric-data \
  --start-time 2026-10-30T09:45:00Z --end-time 2026-10-30T10:10:00Z \
  --metric-data-queries file://metric_queries.json --output json > metric_data.json
```

`metric_queries.json` lists `MetricStat` queries with the `InstanceId` dimension, for example `CPUUtilization`, `CPUCreditBalance`, `StatusCheckFailed`, `EBSIOBalance%`. Copy the application log off the instance (for example with `scp`), or export it with `aws logs filter-log-events --log-group-name /splitwise/app --output json > logs.json` if you later ship logs to CloudWatch. Required permissions: `cloudwatch:GetMetricData`, `ec2:DescribeInstances` (and `logs:FilterLogEvents` only for the CloudWatch Logs option).

## Convert

```bash
.venv/bin/python scripts/convert_cloudwatch_export.py \
  --app-log app.jsonl --metric-data metric_data.json --metric-queries metric_queries.json \
  --output splitwise-incident.jsonl
```

If you exported the log from CloudWatch Logs instead, pass it with `--logs-json` (and `--log-group` to keep the group name):

```bash
.venv/bin/python scripts/convert_cloudwatch_export.py \
  --logs-json logs.json --log-group /splitwise/app \
  --metric-data metric_data.json --metric-queries metric_queries.json \
  --output splitwise-incident.jsonl
```

Use one log source per event range: passing the same events through both `--app-log` and `--logs-json` would duplicate errors and double request counts. Metrics-only conversion requires `--start` and `--end`.

- Window: 15 minutes before the first valid error (or 5xx request) to 5 minutes after the last (`--before-minutes`, `--after-minutes`), or explicit `--start`/`--end`. Without errors and without an explicit window the converter refuses rather than exporting everything.
- Every valid error becomes its own `app.error` record; only `request` lines are summarized per window (`--window-seconds`, default 60) into counts, 5xx count, writes, p95/max duration, max DB time, and peak in-flight. Summaries carry no per-request details.
- Errors without `reported_by`, malformed lines, naive timestamps, unknown event types, metric results without a query or an `InstanceId` dimension, and null datapoints are skipped and counted in the report printed to stderr.
