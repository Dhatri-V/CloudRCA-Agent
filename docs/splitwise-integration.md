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
| `heartbeat` | `load_1m`, `load_5m`, `cpu_count`, `mem_available_mb`, `mem_total_mb`, `disk_free_mb`, `disk_total_mb`, `db_pool_size`, `db_pool_checked_out`, `db_pool_overflow` |

`db_pool_size`, `db_pool_checked_out`, and `db_pool_overflow` are optional non-negative integer snapshots emitted by Splitwise R6 (pool capacity, checked-out connections, and connections opened above capacity; R6 clamps overflow to zero). The SQLAlchemy pool lives inside the application process, so the converter splits each heartbeat line: VM/OS measures become a `host.heartbeat` record (VM layer, A2) and the pool counts become an `app.pool_snapshot` record (application layer, with the line's `service` and `instance_id`). Pool counts on a line without `service` are skipped and reported, never attributed.

Logs must not contain request bodies, SQL parameters, credentials, or personal data (titles, amounts, names). The converter copies only the fields above, so anything else is dropped even if logged by mistake.

## Collect with one command (read-only)

`scripts/collect_splitwise_incident.py` runs the whole collection for one incident window. Its only AWS calls are a fixed, read-only allowlist:
- `sts get-caller-identity`
- `ec2 describe-instances`
- `ec2 describe-instance-credit-specifications`
- `cloudwatch get-metric-data`

It then copies the log with `scp`, runs the converter, and writes everything to `collections/<timestamp>/`, which is gitignored:
- `incident.jsonl`: upload this in the dashboard
- the raw inputs
- `manifest.json`: the window, the commands run, per-metric datapoint counts, and SHA-256 hashes of the files

```bash
# Confirm the identity, instance state, and CPU credit mode; nothing is copied.
.venv/bin/python scripts/collect_splitwise_incident.py --instance-id i-0123456789abcdef0 --region us-east-1 --check

# Collect a window: the log over SSH (an ~/.ssh/config alias), plus basic EC2 metrics.
.venv/bin/python scripts/collect_splitwise_incident.py --instance-id i-0123456789abcdef0 --region us-east-1 \
  --start 2026-11-12T09:40:00Z --end 2026-11-12T10:10:00Z --ssh-host splitwise-ec2
```

Use `--app-log` instead of `--ssh-host` for a log you already copied, and `--profile` for a named or SSO profile. Supporting files are in `config/aws/`:

| File | Purpose |
| --- | --- |
| `metric_queries.template.json` | Ten free basic-monitoring EC2 metrics: CPU, CPU credits (including `CPUSurplusCreditsCharged`), status checks, EBS balances, write ops and network. One statistic per metric, 300 s period, `${INSTANCE_ID}` filled in. |
| `cloudrca-collector-readonly-policy.json` | The minimal IAM policy for the laptop identity: the three Describe/Get actions above. `sts:GetCallerIdentity` needs no permission. |
| `splitwise.service.example` | A systemd unit that runs Splitwise on `127.0.0.1:8000` and logs to `/var/log/splitwise/app.jsonl`. It is reachable only through an SSH tunnel. |

Burstable (`t3`/`t4g`) instances default to **unlimited** CPU credits, which bill for sustained CPU above baseline instead of throttling it. `--check` reports the mode. Switch the instance to **standard** before any CPU-pressure test.

## Collect manually (read-only, after you start the instance yourself)

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
