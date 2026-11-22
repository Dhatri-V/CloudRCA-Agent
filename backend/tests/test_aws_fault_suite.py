"""The fault suite's safety contract, exercised with a scripted AWS/SSH shell and a fake clock (no AWS, no EC2).

Covered: dry run makes no changes; mutations need --execute and typed approval; the instance is stopped only if the
suite started it; cleanup always runs and is verified; incomplete cleanup exits 2; faults are time-bounded on the
instance; the database is backed up and restored through SQLite and verified.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from backend.tests.test_rca_comparison import passing_artifacts
from scripts import fault_suite_remote as remote
from scripts.aws_fault_suite import (
    MUTATING_AWS,
    READ_ONLY_AWS,
    ApprovalRefused,
    DryRunViolation,
    Executor,
    FaultSuite,
    Gate,
)

INSTANCE = "i-0abcdef0123456789"
SG = "sg-0suite000000001"
IP = "203.0.113.7"
START = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


class FakeWorld:
    """AWS + SSH + scp double with mutable instance state; records every command."""

    def __init__(self, *, state: str = "stopped", credits: str = "standard", rule_ip: str = "198.51.100.1", burners: str = "0") -> None:
        self.state, self.credits, self.rule_ip, self.burners = state, credits, rule_ip, burners
        self.now = START
        self.calls: list[tuple[str, ...]] = []
        self.ssh_scripts: list[str] = []
        self.fail_on: str | None = None

    def operation(self, command: Sequence[str]) -> tuple[str, str]:
        index = 1
        while command[index].startswith("--"):
            index += 2
        return command[index], command[index + 1]

    def __call__(self, command: Sequence[str], stdin: str | None) -> str:
        command = list(command)
        if command[0] == "ssh":
            assert stdin is not None
            self.ssh_scripts.append(stdin)
            self.calls.append(("ssh",))
            if self.fail_on and self.fail_on in stdin:
                raise RuntimeError(f"scripted failure on {self.fail_on}")
            return self.ssh_reply(stdin)
        if command[0] == "scp":
            self.calls.append(("scp",))
            heartbeats = [
                {"event": "heartbeat", "ts": (self.now - timedelta(minutes=minute)).isoformat().replace("+00:00", "Z"),
                 "service": "splitwise-api", "instance_id": INSTANCE, "load_1m": 0.1, "cpu_count": 2}
                for minute in range(0, 400, 1)
            ]
            Path(command[-1]).write_text("\n".join(json.dumps(item) for item in heartbeats) + "\n")
            return ""
        service, operation = self.operation(command)
        self.calls.append((service, operation, command[command.index("--profile") + 1]))
        return json.dumps(self.aws(service, operation))

    def ssh_reply(self, script: str) -> str:
        if "backup_logical_sha256" in script:
            return "backup_logical_sha256=hash-before\nservice=active\n"
        if "restored_logical_sha256" in script:
            return "restored_logical_sha256=hash-before\nservice=active\n"
        if "echo service=" in script and "uptime_s" in script:
            expected = re.search(r"expected_db_logical_sha256=(\S+)", script)
            lines = ["service=active", "api=200", "fault_units=", f"burners={self.burners}", "marker=absent", "uptime_s=99999",
                     "db_integrity=ok", "db_logical_sha256=hash-before"]
            if expected:
                lines.append(f"expected_db_logical_sha256={expected.group(1)}")
            return "\n".join(lines) + "\n"
        return "ok=1\n"

    def aws(self, service: str, operation: str) -> dict[str, Any]:
        stamp = self.now.isoformat()
        replies: dict[tuple[str, str], Any] = {
            ("sts", "get-caller-identity"): {"Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/CloudRcaReadOnly/cloudrca-collector"},
            ("ec2", "describe-instances"): {"Reservations": [{"Instances": [{
                "InstanceId": INSTANCE, "State": {"Name": self.state}, "InstanceType": "t3.small",
                "PublicDnsName": "ec2-203-0-113-9.example.compute.amazonaws.com" if self.state == "running" else "",
                "Placement": {"AvailabilityZone": "ap-southeast-2c"}}]}]},
            ("ec2", "describe-instance-credit-specifications"): {"InstanceCreditSpecifications": [{"CpuCredits": self.credits}]},
            ("ec2", "describe-instance-attribute"): {"InstanceInitiatedShutdownBehavior": {"Value": "stop"}},
            ("ec2", "describe-security-group-rules"): {"SecurityGroupRules": [{"IsEgress": False, "FromPort": 22, "CidrIpv4": f"{self.rule_ip}/32"}]},
            ("ec2", "describe-instance-status"): {"InstanceStatuses": [{"InstanceStatus": {"Status": "ok"}, "SystemStatus": {"Status": "ok"}}]},
            ("ec2", "authorize-security-group-ingress"): {"SecurityGroupRules": [{"SecurityGroupRuleId": "sgr-0suiteadded"}]},
            ("ec2", "revoke-security-group-ingress"): {"Return": True},
            ("cloudwatch", "get-metric-data"): {"MetricDataResults": [{"Id": "cpu", "Timestamps": [stamp], "Values": [1.0], "StatusCode": "Complete"}]},
        }
        if (service, operation) == ("ec2", "start-instances"):
            self.state = "running"
            return {}
        if (service, operation) == ("ec2", "stop-instances"):
            self.state = "stopped"
            return {}
        return dict(replies[(service, operation)])

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    def mutating_aws(self) -> list[tuple[str, str]]:
        return [call[:2] for call in self.calls if len(call) == 3 and call[:2] in MUTATING_AWS]


def typed(*tokens: str) -> Gate:
    answers = list(tokens)
    return Gate(read=lambda prompt: answers.pop(0) if answers else "no", interactive=lambda: True)


def suite(tmp_path: Path, world: FakeWorld, *, execute: bool, gate: Gate | None = None, analyze: Any = None,
          scenarios: Sequence[str] = ("app_bug", "db_lock", "vm_pressure")) -> tuple[FaultSuite, Path]:
    ssh_config = tmp_path / "ssh_config"
    ssh_config.write_text("Host splitwise-ec2\n  HostName old.example.com\n  User ec2-user\n  HostKeyAlias splitwise-ec2\n")
    executor = Executor(world, execute=execute, readonly_profile="cloudrca-readonly", operator_profile="cloudrca-operator",
                        region="ap-southeast-2", ssh_host="splitwise-ec2")
    built = FaultSuite(
        executor, gate or typed(), scenarios=scenarios, run_dir=tmp_path / "run", instance_id=INSTANCE, security_group=SG,
        clock=lambda: world.now, sleep=world.sleep, public_ip=lambda: IP, ollama_ok=lambda: True,
        analyze=analyze or (lambda dataset: passing_artifacts(dataset.parent.name, INSTANCE)), ssh_config=ssh_config, out=lambda _: None,
    )
    return built, ssh_config


@pytest.fixture(autouse=True)
def llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDRCA_QWEN_MODEL", "qwen-test")
    monkeypatch.setenv("CLOUDRCA_GLM_MODEL", "main-test")


def test_dry_run_makes_no_aws_or_ec2_change(tmp_path: Path) -> None:
    world = FakeWorld()
    built, ssh_config = suite(tmp_path, world, execute=False)
    before = ssh_config.read_text()
    assert built.run() == 0
    assert world.mutating_aws() == [] and ("ssh",) not in world.calls and ("scp",) not in world.calls
    assert {call[:2] for call in world.calls if len(call) == 3} <= READ_ONLY_AWS
    assert world.state == "stopped" and ssh_config.read_text() == before
    assert all(not entry["mutating"] for entry in built.x.log)


def test_the_executor_refuses_changes_without_execute_approval_or_allowlist() -> None:
    world = FakeWorld()
    dry = Executor(world, execute=False, readonly_profile="ro", operator_profile="op", region="ap-southeast-2", ssh_host="h")
    with pytest.raises(DryRunViolation, match="dry run refused"):
        dry.aws("ec2", "start-instances", "--instance-ids", INSTANCE, approval="start")
    with pytest.raises(DryRunViolation, match="dry run refused"):
        dry.ssh(remote.start_cpu_burner(60), mutating=True, approval="vm_pressure")
    live = Executor(world, execute=True, readonly_profile="ro", operator_profile="op", region="ap-southeast-2", ssh_host="h")
    with pytest.raises(DryRunViolation, match="no approval"):
        live.aws("ec2", "stop-instances", "--instance-ids", INSTANCE, approval="stop")
    with pytest.raises(DryRunViolation, match="not allowlisted"):
        live.aws("ec2", "terminate-instances", "--instance-ids", INSTANCE)
    assert world.calls == []
    live.approved.add("stop")
    live.aws("ec2", "stop-instances", "--instance-ids", INSTANCE, approval="stop")
    assert world.calls == [("ec2", "stop-instances", "op")]  # mutations always use the operator profile


def test_approval_needs_the_exact_token_and_a_terminal() -> None:
    with pytest.raises(ApprovalRefused, match="interactive terminal"):
        Gate(read=lambda _: "start", interactive=lambda: False).approve("start", "x")
    with pytest.raises(ApprovalRefused, match="not approved"):
        Gate(read=lambda _: "yes", interactive=lambda: True).approve("start", "x")
    Gate(read=lambda _: "start", interactive=lambda: True).approve("start", "x")


def test_a_full_run_from_stopped_restores_everything_and_stops_the_instance(tmp_path: Path) -> None:
    world = FakeWorld()
    built, ssh_config = suite(tmp_path, world, execute=True, gate=typed("sg", "start", "app_bug", "db_lock", "vm_pressure"))
    assert built.run() == 0
    mutations = world.mutating_aws()
    assert mutations[:2] == [("ec2", "authorize-security-group-ingress"), ("ec2", "start-instances")]
    assert mutations[-2:] == [("ec2", "revoke-security-group-ingress"), ("ec2", "stop-instances")]
    assert all(call[2] == "cloudrca-operator" for call in world.calls if len(call) == 3 and call[:2] in MUTATING_AWS)
    assert world.state == "stopped"
    assert "ec2-203-0-113-9.example.compute.amazonaws.com" in ssh_config.read_text()
    assert (tmp_path / "ssh_config.cloudrca-backup").read_text().count("old.example.com") == 1
    def position(marker: str) -> int:
        return next(index for index, script in enumerate(world.ssh_scripts) if marker in script)

    backup, arm = position("src.backup(dst)"), position(f"--unit={remote.SAFETY_RESTORE_UNIT} --on-active")
    lock, restore = position(f"systemd-run --unit={remote.unit('dblock')}"), position(f"systemctl stop {remote.SAFETY_RESTORE_UNIT}.timer")
    assert backup < arm < lock < restore  # verified backup and independent safety restore exist before the lock is held
    scripts = "\n".join(world.ssh_scripts)
    assert "shutdown -h +" in scripts and "shutdown -c" in scripts
    report = (tmp_path / "run" / "report.md").read_text()
    assert report.count("PASS") == 4 and "CLEANUP INCOMPLETE" not in report
    for name in ("healthy_precheck", "app_bug", "db_lock", "vm_pressure"):
        frozen = (tmp_path / "run" / "expectations" / f"{name}.expected.json").read_bytes()
        assert frozen == built.expectations[name].raw


def test_an_instance_that_was_already_running_is_left_running(tmp_path: Path) -> None:
    world = FakeWorld(state="running", rule_ip=IP)
    built, _ = suite(tmp_path, world, execute=True, gate=typed("app_bug"), scenarios=["app_bug"])
    assert built.run() == 0
    assert world.mutating_aws() == [] and world.state == "running"
    assert not any("shutdown -h" in script for script in world.ssh_scripts)


def test_a_failure_mid_scenario_still_restores_the_database_and_stops_the_instance(tmp_path: Path) -> None:
    world = FakeWorld(rule_ip=IP)
    world.fail_on = f"systemd-run --unit={remote.unit('dblock')}"  # the lock-holder start fails after the backup and safety restore were armed
    built, _ = suite(tmp_path, world, execute=True, gate=typed("start", "db_lock"), scenarios=["db_lock"])
    assert built.run() == 1
    assert any("restored_logical_sha256" in script for script in world.ssh_scripts)
    assert world.state == "stopped" and world.mutating_aws()[-1] == ("ec2", "stop-instances")
    assert "scripted failure" in (tmp_path / "run" / "report.md").read_text()


def test_unverified_recovery_is_reported_as_manual_intervention(tmp_path: Path) -> None:
    world = FakeWorld(state="running", rule_ip=IP, burners="2")
    built, _ = suite(tmp_path, world, execute=True, gate=typed("vm_pressure"), scenarios=["vm_pressure"])
    assert built.run() == 2
    report = (tmp_path / "run" / "report.md").read_text()
    assert "CLEANUP INCOMPLETE: manual intervention required" in report and "2 CPU burner process(es) still running" in report


def test_a_refused_fault_approval_injects_nothing_and_still_cleans_up(tmp_path: Path) -> None:
    world = FakeWorld(rule_ip=IP)
    built, _ = suite(tmp_path, world, execute=True, gate=typed("start", "no"), scenarios=["app_bug"])
    assert built.run() == 1
    assert not any("/expenses/9000" in script for script in world.ssh_scripts)
    assert world.state == "stopped"


def test_a_failed_healthy_precheck_blocks_every_fault(tmp_path: Path) -> None:
    world = FakeWorld(state="running", rule_ip=IP)
    unhealthy = lambda dataset: passing_artifacts("app_bug", INSTANCE)  # noqa: E731 - signals and a report on a quiet window
    built, _ = suite(tmp_path, world, execute=True, gate=typed("app_bug"), analyze=unhealthy, scenarios=["app_bug"])
    assert built.run() == 1
    assert not any("/expenses/9000" in script for script in world.ssh_scripts)


def test_unlimited_credits_refuse_vm_pressure_before_any_change(tmp_path: Path) -> None:
    world = FakeWorld(credits="unlimited")
    built, _ = suite(tmp_path, world, execute=True, gate=typed("start", "vm_pressure"))
    assert built.run() == 1
    assert world.mutating_aws() == [] and ("ssh",) not in world.calls


def test_every_fault_is_bounded_on_the_instance_by_systemd() -> None:
    for script in (remote.start_traffic(480), remote.start_lock_holder(420), remote.start_cpu_burner(1800)):
        assert "systemd-run" in script and "RuntimeMaxSec=" in script and "KillMode=control-group" in script
    assert "RuntimeMaxSec=1800" in remote.start_cpu_burner(1800)
    assert "--on-active=1380s" in remote.arm_db_safety_restore(1380) and remote.MARKER in remote.arm_db_safety_restore(1380)


def test_database_backup_and_restore_go_through_sqlite_and_are_verified() -> None:
    backup, restore = remote.backup_db_script(), remote.restore_db_script()
    assert ".backup(" in backup and "PRAGMA integrity_check" in backup and "iterdump" in backup
    assert ".backup(" in restore and "wal_checkpoint(TRUNCATE)" in restore and "integrity" in restore
    assert not re.search(r"rm[^\n]*-(wal|shm|journal)", backup + restore)  # journal/WAL files are never deleted by hand
    assert "trap 'sudo systemctl start splitwise.service' EXIT" in backup and "trap 'sudo systemctl start splitwise.service' EXIT" in restore
    guarded = remote.arm_db_safety_restore(60)
    assert f"[ -e {remote.MARKER} ] || exit 0" in guarded  # the safety restore does nothing once the suite has restored


@pytest.mark.parametrize(("status", "problem"), [
    ({"service": "failed"}, "splitwise.service is failed"),
    ({"api": "500"}, "GET /expenses returned 500"),
    ({"fault_units": "cloudrca-fault-cpu.service"}, "fault units still active"),
    ({"burners": "1"}, "CPU burner"),
    ({"marker": "present"}, "restore marker still present"),
    ({"db_integrity": "corrupt"}, "integrity_check"),
    ({"expected_db_logical_sha256": "a", "db_logical_sha256": "b"}, "differs from the verified pre-test backup"),
])
def test_each_unrecovered_property_is_named(status: dict[str, str], problem: str) -> None:
    healthy = {"service": "active", "api": "200", "fault_units": "", "burners": "0", "marker": "absent", "db_integrity": "ok"}
    problems = remote.recovery_problems({**healthy, **status})
    assert len(problems) == 1 and problem in problems[0]
    assert remote.recovery_problems(healthy) == []


def test_the_operator_policy_is_scoped_and_denies_destruction() -> None:
    policy = json.loads((Path(__file__).resolve().parents[2] / "config/aws/cloudrca-test-operator-policy.json").read_text())
    allow = [item for item in policy["Statement"] if item["Effect"] == "Allow"]
    scoped = {action: item["Resource"] for item in allow for action in item["Action"] if item["Resource"] != "*"}
    assert set(scoped) == {"ec2:StartInstances", "ec2:StopInstances", "ec2:AuthorizeSecurityGroupIngress", "ec2:RevokeSecurityGroupIngress"}
    assert all("${INSTANCE_ID}" in resource or "${SECURITY_GROUP_ID}" in resource for resource in scoped.values())
    unscoped = {action for item in allow if item["Resource"] == "*" for action in item["Action"]}
    assert all(action.startswith("ec2:Describe") for action in unscoped)
    [deny] = [item for item in policy["Statement"] if item["Effect"] == "Deny"]
    assert {"ec2:TerminateInstances", "ec2:RunInstances", "ec2:ModifyInstanceAttribute", "iam:*"} <= set(deny["Action"])


def test_mutating_and_read_only_allowlists_do_not_overlap() -> None:
    assert not (READ_ONLY_AWS & MUTATING_AWS)
    assert ("ec2", "terminate-instances") not in READ_ONLY_AWS | MUTATING_AWS
    shutil.which("aws")  # the suite never needs AWS during tests
