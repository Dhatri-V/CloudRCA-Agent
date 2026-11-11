"""The EC2 collector runs only allowlisted read-only AWS calls, then converts one window into a CloudRCA dataset.

AWS and scp are replaced by a scripted runner: these tests never contact AWS.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from cloudrca_backend.normalization import normalize_file

from scripts.collect_splitwise_incident import READ_ONLY_AWS, TEMPLATE, Collector, main

ROOT = Path(__file__).resolve().parents[2]
APP_LOG = ROOT / "tests" / "fixtures" / "splitwise" / "export" / "app_log.jsonl"
INSTANCE = "i-0abc123def4567890"
WINDOW = ["--start", "2026-10-30T09:45:00Z", "--end", "2026-10-30T10:10:00Z"]


class ScriptedAws:
    """Answers the read-only calls with canned JSON and serves the fixture log for scp."""

    def __init__(self, credits: str = "standard", cpu_status: str = "Complete") -> None:
        self.credits, self.cpu_status = credits, cpu_status
        self.calls: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> str:
        command = list(command)
        self.calls.append(command)
        if command[0] == "scp":
            shutil.copyfile(APP_LOG, command[-1])
            return ""
        operation = self._operation(command)
        if operation == ("sts", "get-caller-identity"):
            return json.dumps({"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/cloudrca-readonly"})
        if operation == ("ec2", "describe-instances"):
            return json.dumps({"Reservations": [{"Instances": [{
                "InstanceId": INSTANCE, "State": {"Name": "running"}, "InstanceType": "t3.micro",
                "Placement": {"AvailabilityZone": "us-east-1a"}, "Monitoring": {"State": "disabled"},
                "MetadataOptions": {"HttpTokens": "required"},
            }]}]})
        if operation == ("ec2", "describe-instance-credit-specifications"):
            return json.dumps({"InstanceCreditSpecifications": [{"InstanceId": INSTANCE, "CpuCredits": self.credits}]})
        if operation == ("cloudwatch", "get-metric-data"):
            return json.dumps({"MetricDataResults": [
                {"Id": "cpu", "Timestamps": ["2026-10-30T10:00:00Z", "2026-10-30T09:55:00Z"], "Values": [92.5, 18.0], "StatusCode": self.cpu_status},
                {"Id": "credit_balance", "Timestamps": ["2026-10-30T10:00:00Z"], "Values": [0.0]},
                {"Id": "status_system", "Timestamps": [], "Values": []},
            ]})
        raise AssertionError(f"unexpected command: {command}")

    @staticmethod
    def _operation(command: list[str]) -> tuple[str, str]:
        index = 1
        while command[index].startswith("--"):
            index += 2
        return command[index], command[index + 1]

    def operations(self) -> list[tuple[str, str]]:
        return [("scp", "") if call[0] == "scp" else self._operation(call) for call in self.calls]


def collect(tmp_path: Path, aws: ScriptedAws, *extra: str) -> Path:
    out = tmp_path / "collection"
    assert main(["--instance-id", INSTANCE, "--region", "us-east-1", *WINDOW, "--out-dir", str(out), *extra], aws) == 0
    return out


def test_collection_runs_only_read_only_calls_and_produces_a_normalizable_dataset(tmp_path: Path) -> None:
    aws = ScriptedAws()
    out = collect(tmp_path, aws, "--ssh-host", "splitwise-ec2")
    assert set(aws.operations()) - {("scp", "")} <= READ_ONLY_AWS
    assert aws.operations() == [
        ("sts", "get-caller-identity"), ("ec2", "describe-instances"), ("ec2", "describe-instance-credit-specifications"),
        ("cloudwatch", "get-metric-data"), ("scp", ""),
    ]
    assert aws.calls[-1][:4] == ["scp", "-q", "-o", "BatchMode=yes"] and aws.calls[-1][4] == "splitwise-ec2:/var/log/splitwise/app.jsonl"
    batch = normalize_file(out / "incident.jsonl")
    assert batch.statistics.rejected == 0 and batch.statistics.accepted > 0
    records = [json.loads(line) for line in (out / "incident.jsonl").read_text().splitlines()]
    metrics = [record for record in records if record["cloudrca_record"] == "aws.cloudwatch.metric"]
    assert {(item["metric_name"], item["value"]) for item in metrics} == {("CPUUtilization", 92.5), ("CPUUtilization", 18.0), ("CPUCreditBalance", 0.0)}
    assert all(item["dimensions"] == {"InstanceId": INSTANCE} for item in metrics)
    assert "SYNTHETIC-PRIVATE" not in (out / "incident.jsonl").read_text()


def test_the_manifest_records_window_commands_counts_and_file_hashes(tmp_path: Path) -> None:
    out = collect(tmp_path, ScriptedAws(), "--ssh-host", "splitwise-ec2")
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["window"] == ["2026-10-30T09:45:00Z", "2026-10-30T10:10:00Z"]
    assert manifest["instance"]["cpu_credits"] == "standard" and manifest["instance"]["state"] == "running"
    assert manifest["metric_results"] == {"cpu": 2, "credit_balance": 1, "status_system": 0}
    assert set(manifest["files"]) == {"metric_queries.json", "metric_data.json", "app.jsonl", "incident.jsonl"}
    assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in manifest["files"].values())
    assert manifest["dataset_records"] == len((out / "incident.jsonl").read_text().splitlines())
    assert all(command.startswith(("aws --region us-east-1 --output json ", "scp ")) for command in manifest["commands"])


def test_a_local_log_copy_needs_no_ssh(tmp_path: Path) -> None:
    aws = ScriptedAws()
    out = collect(tmp_path, aws, "--app-log", str(APP_LOG), "--profile", "cloudrca-readonly")
    assert ("scp", "") not in aws.operations() and (out / "app.jsonl").read_bytes() == APP_LOG.read_bytes()
    assert all(call[:6] == ["aws", "--region", "us-east-1", "--output", "json", "--profile"] for call in aws.calls)


def test_check_only_describes_and_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    aws = ScriptedAws(credits="unlimited")
    out = tmp_path / "collection"
    assert main(["--instance-id", INSTANCE, "--region", "us-east-1", "--check", "--out-dir", str(out)], aws) == 0
    assert aws.operations() == [("sts", "get-caller-identity"), ("ec2", "describe-instances"), ("ec2", "describe-instance-credit-specifications")]
    assert not out.exists()
    printed = capsys.readouterr().err
    assert "****9012" in printed and "123456789012" not in printed  # only the account's last four digits
    assert "unlimited CPU credits bill" in printed


def test_operations_outside_the_read_only_allowlist_are_refused() -> None:
    collector = Collector("us-east-1", None, ScriptedAws())
    for service, operation in (("ec2", "start-instances"), ("ec2", "stop-instances"), ("logs", "put-log-events"), ("ec2", "modify-instance-attribute")):
        with pytest.raises(ValueError, match="non-allowlisted"):
            collector.aws(service, operation)
    assert collector.commands == []


@pytest.mark.parametrize(("arguments", "message"), [
    (["--instance-id", "i-123", "--region", "us-east-1", *WINDOW, "--ssh-host", "h"], "--instance-id"),
    (["--instance-id", INSTANCE, "--region", "useast1", *WINDOW, "--ssh-host", "h"], "--region"),
    (["--instance-id", INSTANCE, "--region", "us-east-1", "--ssh-host", "h"], "--start and --end are required"),
    (["--instance-id", INSTANCE, "--region", "us-east-1", "--start", "2026-10-30T10:00:00Z", "--end", "2026-10-30T09:00:00Z", "--ssh-host", "h"], "after --start"),
    (["--instance-id", INSTANCE, "--region", "us-east-1", "--start", "2026-10-28T00:00:00Z", "--end", "2026-10-30T00:00:00Z", "--ssh-host", "h"], "within 24 hours"),
    (["--instance-id", INSTANCE, "--region", "us-east-1", "--start", "2099-01-01T00:00:00Z", "--end", "2099-01-01T01:00:00Z", "--ssh-host", "h"], "future"),
    (["--instance-id", INSTANCE, "--region", "us-east-1", "--start", "2026-10-30T09:45:00", "--end", "2026-10-30T10:10:00Z", "--ssh-host", "h"], "timezone"),
    (["--instance-id", INSTANCE, "--region", "us-east-1", *WINDOW], "exactly one log source"),
    (["--instance-id", INSTANCE, "--region", "us-east-1", *WINDOW, "--ssh-host", "h", "--app-log", str(APP_LOG)], "exactly one log source"),
    (["--instance-id", INSTANCE, "--region", "us-east-1", *WINDOW, "--ssh-host=-oProxyCommand=x"], "--ssh-host"),
    (["--instance-id", INSTANCE, "--region", "us-east-1", *WINDOW, "--ssh-host", "h", "--remote-log", "app.jsonl;rm"], "--remote-log"),
])
def test_invalid_arguments_are_rejected_before_anything_is_copied(tmp_path: Path, arguments: list[str], message: str) -> None:
    aws = ScriptedAws()
    with pytest.raises(SystemExit, match=re.escape(message)):
        main([*arguments, "--out-dir", str(tmp_path / "collection")], aws)
    assert ("cloudwatch", "get-metric-data") not in aws.operations() and ("scp", "") not in aws.operations()


def test_the_metric_template_is_instance_scoped_with_one_statistic_per_metric() -> None:
    queries: list[dict[str, Any]] = json.loads(TEMPLATE.read_text())
    ids = [query["Id"] for query in queries]
    assert len(ids) == len(set(ids)) and all(re.fullmatch(r"[a-z][a-z0-9_]*", item) for item in ids)
    names = [query["MetricStat"]["Metric"]["MetricName"] for query in queries]
    assert len(names) == len(set(names))  # one series per metric, so summarized series never mix statistics
    for query in queries:
        stat = query["MetricStat"]
        assert stat["Metric"]["Namespace"] == "AWS/EC2" and stat["Period"] == 300  # free basic monitoring granularity
        assert stat["Metric"]["Dimensions"] == [{"Name": "InstanceId", "Value": "${INSTANCE_ID}"}]


@pytest.mark.parametrize("status", ["InternalError", "PartialData", "Forbidden"])
def test_incomplete_metric_results_stop_the_collection_without_a_dataset(tmp_path: Path, status: str) -> None:
    aws = ScriptedAws(cpu_status=status)
    out = tmp_path / "collection"
    with pytest.raises(SystemExit, match=f"incomplete results {{'cpu': '{status}'}}"):
        main(["--instance-id", INSTANCE, "--region", "us-east-1", *WINDOW, "--out-dir", str(out), "--ssh-host", "splitwise-ec2"], aws)
    assert not (out / "incident.jsonl").exists() and not (out / "manifest.json").exists()
    assert ("scp", "") not in aws.operations()


def test_an_existing_collection_is_never_overwritten(tmp_path: Path) -> None:
    out = collect(tmp_path, ScriptedAws(), "--ssh-host", "splitwise-ec2")
    before = {path.name: path.read_bytes() for path in out.iterdir()}
    aws = ScriptedAws()
    with pytest.raises(SystemExit, match="not empty"):
        main(["--instance-id", INSTANCE, "--region", "us-east-1", *WINDOW, "--out-dir", str(out), "--ssh-host", "splitwise-ec2"], aws)
    assert {path.name: path.read_bytes() for path in out.iterdir()} == before
    assert ("cloudwatch", "get-metric-data") not in aws.operations()


def test_the_example_unit_creates_its_log_directory_and_stays_on_localhost() -> None:
    unit = (ROOT / "config" / "aws" / "splitwise.service.example").read_text()
    assert "LogsDirectory=splitwise" in unit and "SPLITWISE_LOG_FILE=/var/log/splitwise/app.jsonl" in unit
    assert "--host 127.0.0.1" in unit and "0.0.0.0" not in unit
    # Matches the deployed instance: the checkout's existing .venv, with a 60 s heartbeat.
    assert "ExecStart=/home/ec2-user/splitwise-backend/.venv/bin/uvicorn main:app" in unit and "/venv/bin" not in unit
    assert "SPLITWISE_HEARTBEAT_SECONDS=60" in unit
