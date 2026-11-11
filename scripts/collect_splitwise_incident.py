#!/usr/bin/env python3
"""Collect one Splitwise incident window from EC2 into a CloudRCA dataset (read-only; standard library only).

Runs only these read-only commands, through the AWS CLI and scp you already have configured:
  aws sts get-caller-identity                      confirm which account and identity are in use
  aws ec2 describe-instances                       instance state, type, and zone (recorded in instance.json)
  aws ec2 describe-instance-credit-specifications  burstable credit mode (standard or unlimited)
  aws cloudwatch get-metric-data                   basic EC2 metrics for the window (config/aws/metric_queries.template.json)
  scp <ssh-host>:<remote-log>                      copy the Splitwise JSON-lines log (or pass --app-log for a local copy)

Then it runs scripts/convert_cloudwatch_export.py on the window and writes, under --out-dir:
  instance.json, metric_queries.json, metric_data.json, app.jsonl, incident.jsonl (upload this), manifest.json
--out-dir must be new or empty; manifest.json is written last, so a directory without it is an incomplete collection.
app.jsonl is the raw log copy (the whole file, not only the window): keep it private and delete it when done.

  --check   only confirm the identity and describe the instance; nothing is copied or converted

Nothing is created, started, stopped, or modified in AWS, and credentials are never read or printed by this script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import convert_cloudwatch_export

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "config" / "aws" / "metric_queries.template.json"
INSTANCE_ID = re.compile(r"^i-[0-9a-f]{8}([0-9a-f]{9})?$")
REGION = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-\d$")
SSH_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]*$")  # an ~/.ssh/config alias or user@host; never an option
REMOTE_PATH = re.compile(r"^/[A-Za-z0-9._/-]+$")
MAX_WINDOW = timedelta(hours=24)
# The only AWS CLI operations this script may run; every one is read-only.
READ_ONLY_AWS = frozenset({
    ("sts", "get-caller-identity"),
    ("ec2", "describe-instances"),
    ("ec2", "describe-instance-credit-specifications"),
    ("cloudwatch", "get-metric-data"),
})

Runner = Callable[[Sequence[str]], str]


def run_command(command: Sequence[str]) -> str:
    """Run a command and return stdout; stderr is shown only on failure (it never contains credentials)."""
    result = subprocess.run(list(command), capture_output=True, text=True, timeout=300, check=False)
    if result.returncode != 0:
        raise SystemExit(f"{' '.join(command)} failed (exit {result.returncode}): {result.stderr.strip()[:500]}")
    return result.stdout


class Collector:
    def __init__(self, region: str, profile: str | None, runner: Runner = run_command) -> None:
        self._base = ["aws", "--region", region, "--output", "json", *(["--profile", profile] if profile else [])]
        self._runner = runner
        self.commands: list[list[str]] = []

    def aws(self, service: str, operation: str, *args: str) -> Any:
        if (service, operation) not in READ_ONLY_AWS:
            raise ValueError(f"refusing non-allowlisted AWS operation: {service} {operation}")
        command = [*self._base, service, operation, *args]
        self.commands.append(command)
        return json.loads(self._runner(command) or "{}")

    def scp(self, ssh_host: str, remote_log: str, destination: Path) -> None:
        command = ["scp", "-q", "-o", "BatchMode=yes", f"{ssh_host}:{remote_log}", str(destination)]
        self.commands.append(command)
        self._runner(command)


def parse_time(value: str) -> datetime:
    parsed = convert_cloudwatch_export.parse_time(value)
    if parsed is None:
        raise SystemExit(f"{value!r} is not an ISO 8601 timestamp with a timezone, e.g. 2026-11-12T09:00:00Z.")
    return parsed


def describe_instance(collector: Collector, instance_id: str) -> dict[str, Any]:
    reservations = collector.aws("ec2", "describe-instances", "--instance-ids", instance_id).get("Reservations", [])
    instances = [item for reservation in reservations for item in reservation.get("Instances", [])]
    if len(instances) != 1:
        raise SystemExit(f"describe-instances returned {len(instances)} instances for {instance_id}.")
    instance = instances[0]
    summary: dict[str, Any] = {
        "instance_id": instance.get("InstanceId"),
        "state": (instance.get("State") or {}).get("Name"),
        "instance_type": instance.get("InstanceType"),
        "availability_zone": (instance.get("Placement") or {}).get("AvailabilityZone"),
        "launch_time": instance.get("LaunchTime"),
        "monitoring": (instance.get("Monitoring") or {}).get("State"),
        "imds_http_tokens": (instance.get("MetadataOptions") or {}).get("HttpTokens"),
    }
    if str(summary["instance_type"] or "").startswith("t"):
        credits = collector.aws("ec2", "describe-instance-credit-specifications", "--instance-ids", instance_id)
        specs = credits.get("InstanceCreditSpecifications", [])
        summary["cpu_credits"] = specs[0].get("CpuCredits") if specs else None
    return summary


def render_queries(instance_id: str, template: Path) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = json.loads(template.read_text(encoding="utf-8").replace("${INSTANCE_ID}", instance_id))
    for query in queries:
        dimensions = query["MetricStat"]["Metric"]["Dimensions"]
        if {"Name": "InstanceId", "Value": instance_id} not in dimensions:
            raise SystemExit(f"metric query {query.get('Id')!r} must carry the InstanceId dimension.")
    return queries


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def collect(args: argparse.Namespace, runner: Runner = run_command) -> dict[str, Any]:
    if not INSTANCE_ID.match(args.instance_id):
        raise SystemExit("--instance-id must look like i-0123456789abcdef0.")
    if not REGION.match(args.region):
        raise SystemExit("--region must look like us-east-1.")
    collector = Collector(args.region, args.profile, runner)
    identity = collector.aws("sts", "get-caller-identity")
    account = str(identity.get("Account", ""))
    instance = describe_instance(collector, args.instance_id)
    print(f"identity: account ****{account[-4:]} | instance {instance['instance_id']} {instance['state']} "
          f"{instance['instance_type']} {instance['availability_zone']} credits={instance.get('cpu_credits')}", file=sys.stderr)
    if instance.get("cpu_credits") == "unlimited":
        print("note: unlimited CPU credits bill for sustained CPU above baseline instead of throttling.", file=sys.stderr)
    if args.check:
        return {"instance": instance, "commands": collector.commands}

    if not (args.start and args.end):
        raise SystemExit("--start and --end are required unless --check is given.")
    start, end = parse_time(args.start), parse_time(args.end)
    if not start < end <= start + MAX_WINDOW:
        raise SystemExit("--end must be after --start and within 24 hours of it.")
    if end > datetime.now(UTC) + timedelta(minutes=5):
        raise SystemExit("--end is in the future.")
    if bool(args.app_log) == bool(args.ssh_host):
        raise SystemExit("Give exactly one log source: --app-log (a local copy) or --ssh-host (copied with scp).")
    if args.ssh_host and not SSH_HOST.match(args.ssh_host):
        raise SystemExit("--ssh-host must be an ~/.ssh/config alias or user@host.")
    if not REMOTE_PATH.match(args.remote_log):
        raise SystemExit("--remote-log must be an absolute path of plain characters.")

    out = args.out_dir
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise SystemExit(f"--out-dir {out} already exists and is not empty; choose a new directory so collections never mix.")
    out.mkdir(parents=True, exist_ok=True)
    (out / "instance.json").write_text(json.dumps(instance, indent=2, default=str) + "\n", encoding="utf-8")
    queries_path, data_path, log_path, dataset = out / "metric_queries.json", out / "metric_data.json", out / "app.jsonl", out / "incident.jsonl"
    queries_path.write_text(json.dumps(render_queries(args.instance_id, args.queries_template), indent=2) + "\n", encoding="utf-8")
    metric_data = collector.aws(
        "cloudwatch", "get-metric-data", "--metric-data-queries", f"file://{queries_path}",
        "--start-time", convert_cloudwatch_export.iso(start), "--end-time", convert_cloudwatch_export.iso(end),
    )
    # A successful call can still carry failed or partial results; never convert an incomplete metric set.
    incomplete = {
        str(item.get("Id")): str(item.get("StatusCode"))
        for item in metric_data.get("MetricDataResults", []) if item.get("StatusCode", "Complete") != "Complete"
    }
    if incomplete:
        raise SystemExit(f"get-metric-data returned incomplete results {incomplete}; no dataset was written. Retry the collection.")
    data_path.write_text(json.dumps(metric_data, indent=2) + "\n", encoding="utf-8")
    if args.ssh_host:
        collector.scp(args.ssh_host, args.remote_log, log_path)
    else:
        shutil.copyfile(args.app_log, log_path)

    convert_cloudwatch_export.main([
        "--app-log", str(log_path), "--metric-data", str(data_path), "--metric-queries", str(queries_path),
        "--start", convert_cloudwatch_export.iso(start), "--end", convert_cloudwatch_export.iso(end), "--output", str(dataset),
    ])
    manifest = {
        "collected_at": convert_cloudwatch_export.iso(datetime.now(UTC)),
        "window": [convert_cloudwatch_export.iso(start), convert_cloudwatch_export.iso(end)],
        "region": args.region,
        "instance": instance,
        "commands": [" ".join(command) for command in collector.commands],
        "metric_results": {item.get("Id"): len(item.get("Values", [])) for item in metric_data.get("MetricDataResults", [])},
        "files": {path.name: sha256(path) for path in (queries_path, data_path, log_path, dataset)},
        "dataset_records": sum(1 for line in dataset.read_text(encoding="utf-8").splitlines() if line.strip()),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"dataset: {dataset} ({manifest['dataset_records']} records); upload it in the CloudRCA dashboard.", file=sys.stderr)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--profile", help="AWS CLI profile (for example an SSO profile); default: the CLI's default")
    parser.add_argument("--check", action="store_true", help="only confirm identity and describe the instance")
    parser.add_argument("--start", help="window start, ISO 8601 with timezone")
    parser.add_argument("--end", help="window end, ISO 8601 with timezone (at most 24 h after --start)")
    parser.add_argument("--ssh-host", help="~/.ssh/config alias for the instance; the log is copied with scp")
    parser.add_argument("--remote-log", default="/var/log/splitwise/app.jsonl")
    parser.add_argument("--app-log", type=Path, help="a local copy of the Splitwise log instead of --ssh-host")
    parser.add_argument("--queries-template", type=Path, default=TEMPLATE)
    parser.add_argument("--out-dir", type=Path, default=Path("collections") / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    return parser


def main(argv: list[str] | None = None, runner: Runner = run_command) -> int:
    collect(build_parser().parse_args(argv), runner)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
