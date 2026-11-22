#!/usr/bin/env python3
"""One-command CloudRCA fault suite for the existing Splitwise EC2 instance (dry run by default).

  .venv/bin/python scripts/aws_fault_suite.py                 # dry run: read-only checks and the full plan
  .venv/bin/python scripts/aws_fault_suite.py --execute       # gated run of healthy_precheck, app_bug, db_lock, vm_pressure

Dry run makes no AWS or EC2 changes: the executor refuses every mutating AWS operation and every mutating SSH script
unless --execute is given AND the matching typed approval gate was passed. --execute refuses without a terminal.

AWS access (least privilege):
  cloudrca-readonly  describe the instance and credits, read CloudWatch metrics (all inspection and collection)
  cloudrca-operator  start/stop this instance and add/remove this security group's ingress rules only
                     (config/aws/cloudrca-test-operator-policy.json; created only with your approval)

Expectations are frozen from config/aws/scenarios before anything runs (see scripts/rca_comparison.py). Cleanup runs
in a finally block, every recovery property is verified, and an incomplete cleanup exits 2 with manual steps.
Costs printed by this tool are estimates, not guarantees.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import collect_splitwise_incident, rca_comparison
from scripts import fault_suite_remote as remote

ROOT = Path(__file__).resolve().parents[1]
INSTANCE_ID = "i-002b290b78fc93abe"
REGION = "ap-southeast-2"
SECURITY_GROUP = "sg-08de7ca0821045741"
SSH_ALIAS = "splitwise-ec2"
SCENARIOS = ("healthy_precheck", "app_bug", "db_lock", "vm_pressure")
FAULTS = SCENARIOS[1:]
# Estimates only (on-demand Linux, ap-southeast-2); check current AWS pricing before relying on them.
HOURLY_ESTIMATE_USD = {"t3.small": 0.0264, "public_ipv4": 0.005}

READ_ONLY_AWS = frozenset({
    ("sts", "get-caller-identity"), ("ec2", "describe-instances"), ("ec2", "describe-instance-credit-specifications"),
    ("ec2", "describe-instance-status"), ("ec2", "describe-instance-attribute"), ("ec2", "describe-security-group-rules"),
    ("cloudwatch", "get-metric-data"),
})
MUTATING_AWS = frozenset({
    ("ec2", "start-instances"), ("ec2", "stop-instances"),
    ("ec2", "authorize-security-group-ingress"), ("ec2", "revoke-security-group-ingress"),
})


@dataclass(frozen=True)
class Durations:
    """Seconds; the defaults keep each window long enough for 5-minute basic CloudWatch metrics."""

    boot_settle: int = 15 * 60  # Standard-mode credits start near zero after a start
    healthy: int = 10 * 60
    app_bug: int = 8 * 60
    db_lock: int = 8 * 60
    vm_pressure: int = 30 * 60
    cloudwatch_timeout: int = 25 * 60
    poll: int = 30

    def scenario(self, name: str) -> int:
        return int(getattr(self, "healthy" if name == "healthy_precheck" else name))


class DryRunViolation(RuntimeError):
    """A mutating action was attempted without --execute and an approved gate."""


class ApprovalRefused(RuntimeError):
    """The operator did not type the approval token."""


Shell = Callable[[Sequence[str], str | None], str]


def run_shell(command: Sequence[str], stdin: str | None = None) -> str:
    result = subprocess.run(list(command), input=stdin, capture_output=True, text=True, timeout=900, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(command[:6])} failed (exit {result.returncode}): {result.stderr.strip()[:400]}")
    return result.stdout


class Executor:
    """The only path to AWS and SSH. Mutations need execute mode and an approval token for that exact action."""

    def __init__(self, shell: Shell, *, execute: bool, readonly_profile: str, operator_profile: str, region: str, ssh_host: str,
                 instance_id: str = INSTANCE_ID) -> None:
        self.shell, self.execute, self.instance_id = shell, execute, instance_id
        self.readonly_profile, self.operator_profile, self.region, self.ssh_host = readonly_profile, operator_profile, region, ssh_host
        self.approved: set[str] = set()
        self.log: list[dict[str, Any]] = []
        self.on_remote_change: Callable[[], None] = lambda: None

    def _allow(self, approval: str, description: str) -> None:
        if not self.execute:
            raise DryRunViolation(f"dry run refused a change: {description}")
        if approval not in self.approved:
            raise DryRunViolation(f"no approval for {approval!r}: {description}")

    def aws(self, service: str, operation: str, *args: str, approval: str | None = None) -> Any:
        key = (service, operation)
        if key in MUTATING_AWS:
            self._allow(approval or "", f"aws {service} {operation}")
            profile = self.operator_profile
        elif key in READ_ONLY_AWS:
            profile = self.operator_profile if operation in {"describe-instance-status", "describe-instance-attribute", "describe-security-group-rules"} else self.readonly_profile
        else:
            raise DryRunViolation(f"AWS operation not allowlisted: {service} {operation}")
        command = ["aws", "--region", self.region, "--output", "json", "--profile", profile, service, operation, *args]
        self.log.append({"kind": "aws", "mutating": key in MUTATING_AWS, "command": " ".join(command)})
        text = self.shell(command, None)
        return json.loads(text) if text.strip() else {}

    def ssh(self, script: str, *, mutating: bool, approval: str | None = None, label: str = "") -> str:
        if mutating:
            self._allow(approval or "", f"ssh {label or 'script'}")
            self.on_remote_change()
        self.log.append({"kind": "ssh", "mutating": mutating, "label": label})
        # Every script first proves, on the host itself, that it is the expected instance (exit 97 otherwise).
        command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15", self.ssh_host, "bash", "-s"]
        return self.shell(command, remote.identity_guard(self.instance_id) + script)

    def collector_runner(self, command: Sequence[str]) -> str:
        """Runner for collect_splitwise_incident: it only issues its own read-only allowlist and scp."""
        self.log.append({"kind": "collect", "mutating": False, "command": " ".join(command[:8])})
        return self.shell(command, None)


class Gate:
    """Typed approval: the operator must type the exact token. Refuses when stdin is not a terminal."""

    def __init__(self, read: Callable[[str], str] = input, interactive: Callable[[], bool] = lambda: sys.stdin.isatty()) -> None:
        self._read, self._interactive = read, interactive

    def approve(self, token: str, description: str) -> None:
        if not self._interactive():
            raise ApprovalRefused("approval needs an interactive terminal; nothing was changed")
        answer = self._read(f"\n{description}\nType '{token}' to approve, anything else to refuse: ").strip()
        if answer != token:
            raise ApprovalRefused(f"not approved: {token}")


@dataclass
class Preflight:
    identity_readonly: str = ""
    identity_operator: str = ""
    state: str = ""
    instance_type: str = ""
    cpu_credits: str = ""
    shutdown_behavior: str = ""
    public_ip: str = ""
    ssh_rule_ok: bool | None = None
    ollama_ok: bool = False
    llm_env_ok: bool = False
    problems: list[str] = field(default_factory=list)


@dataclass
class RunState:
    started_instance: bool = False
    added_rule_id: str | None = None
    rule_attempted: bool = False
    preexisting_rule_ids: frozenset[str] = frozenset()
    guard_armed: bool = False
    db_backup_hash: str | None = None
    db_lock_active: bool = False
    remote_touched: bool = False
    results: list[dict[str, Any]] = field(default_factory=list)
    manual_actions: list[str] = field(default_factory=list)


def masked_arn(arn: str) -> str:
    return re.sub(r"::(\d{8})(\d{4}):", r"::****\2:", arn)


class FaultSuite:
    def __init__(
        self,
        executor: Executor,
        gate: Gate,
        *,
        scenarios: Sequence[str],
        run_dir: Path,
        instance_id: str = INSTANCE_ID,
        security_group: str = SECURITY_GROUP,
        durations: Durations = Durations(),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        public_ip: Callable[[], str] | None = None,
        ollama_ok: Callable[[], bool] | None = None,
        analyze: Callable[[Path], list[dict[str, Any]]] | None = None,
        ssh_config: Path = Path.home() / ".ssh" / "config",
        out: Callable[[str], None] = print,
    ) -> None:
        unknown = [name for name in scenarios if name not in FAULTS]
        if unknown:
            raise SystemExit(f"unknown scenario(s): {unknown}; choose from {list(FAULTS)}")
        self.x, self.gate = executor, gate
        self.scenarios = ["healthy_precheck", *[name for name in FAULTS if name in scenarios]]
        self.run_dir, self.instance_id, self.security_group, self.d = run_dir, instance_id, security_group, durations
        self.clock, self.sleep, self.out = clock, sleep, out
        self.public_ip = public_ip or (lambda: run_shell(["curl", "-s", "-m", "10", "https://checkip.amazonaws.com"]).strip())
        self.ollama_ok = ollama_ok or _ollama_ok
        self.analyze = analyze or analyze_with_pipeline
        self.ssh_config = ssh_config
        self.expectations = rca_comparison.freeze(self.scenarios)  # before anything else can happen
        self.state = RunState()
        self._attempted_ip = ""
        self.x.on_remote_change = lambda: setattr(self.state, "remote_touched", True)

    # ---------- read-only ----------
    def preflight(self) -> Preflight:
        p = Preflight()
        p.identity_readonly = masked_arn(str(self.x.aws("sts", "get-caller-identity").get("Arn", "")))
        if ":assumed-role/CloudRcaReadOnly/" not in p.identity_readonly:
            p.problems.append(f"read-only profile is not the CloudRcaReadOnly role: {p.identity_readonly}")
        reservations = self.x.aws("ec2", "describe-instances", "--instance-ids", self.instance_id).get("Reservations", [])
        instance = (reservations[0].get("Instances") or [{}])[0] if reservations else {}
        p.state, p.instance_type = (instance.get("State") or {}).get("Name", ""), instance.get("InstanceType", "")
        credits = self.x.aws("ec2", "describe-instance-credit-specifications", "--instance-ids", self.instance_id)
        p.cpu_credits = ((credits.get("InstanceCreditSpecifications") or [{}])[0]).get("CpuCredits", "")
        if p.state not in {"stopped", "running"}:
            p.problems.append(f"instance is {p.state or 'unknown'}; the suite only starts from stopped or running")
        if "vm_pressure" in self.scenarios and p.cpu_credits != "standard":
            p.problems.append(f"CPU credits are {p.cpu_credits!r}; vm_pressure requires standard (unlimited bills surplus CPU)")
        try:
            operator = self.x.shell(["aws", "--profile", self.x.operator_profile, "sts", "get-caller-identity", "--output", "json"], None)
            p.identity_operator = masked_arn(str(json.loads(operator).get("Arn", "")))
            attribute = self.x.aws("ec2", "describe-instance-attribute", "--instance-id", self.instance_id, "--attribute", "instanceInitiatedShutdownBehavior")
            p.shutdown_behavior = (attribute.get("InstanceInitiatedShutdownBehavior") or {}).get("Value", "")
            rules = self.x.aws("ec2", "describe-security-group-rules", "--filters", f"Name=group-id,Values={self.security_group}").get("SecurityGroupRules", [])
            p.public_ip = self.public_ip()
            p.ssh_rule_ok = any(
                not rule.get("IsEgress") and rule.get("FromPort") == 22 and rule.get("CidrIpv4") == f"{p.public_ip}/32" for rule in rules
            )
        except Exception as exc:  # noqa: BLE001 - reported, never fatal in a dry run
            p.problems.append(f"operator profile unavailable ({str(exc)[:160]}); required for --execute")
        if p.shutdown_behavior and p.shutdown_behavior != "stop":
            p.problems.append(f"instance shutdown behavior is {p.shutdown_behavior!r}; the safety guard needs 'stop'")
        p.ollama_ok = self.ollama_ok()
        p.llm_env_ok = all(os.environ.get(name) for name in ("CLOUDRCA_QWEN_MODEL", "CLOUDRCA_GLM_MODEL"))
        if not p.ollama_ok:
            p.problems.append("Ollama is not serving a qwen2.5-coder 16k model")
        if not p.llm_env_ok:
            p.problems.append("LLM settings not loaded (run: set -a; source .env; set +a)")
        return p

    def estimate_seconds(self, p: Preflight) -> int:
        total = sum(self.d.scenario(name) + 10 * 60 + 5 * 60 for name in self.scenarios)  # window + CloudWatch lag + RCA
        return total + (self.d.boot_settle + 5 * 60 if p.state == "stopped" else 0)

    def plan(self, p: Preflight) -> list[str]:
        hours = self.estimate_seconds(p) / 3600
        cost = hours * (HOURLY_ESTIMATE_USD.get(p.instance_type or "t3.small", 0.0264) + HOURLY_ESTIMATE_USD["public_ipv4"])
        steps = ["Frozen expectations: " + ", ".join(f"{name} sha256={item.sha256[:12]}" for name, item in self.expectations.items())]
        if p.ssh_rule_ok is False:
            steps.append(f"[AWS change, approval] add SSH ingress {p.public_ip}/32 to {self.security_group}; removed again at cleanup")
        if p.state == "stopped":
            steps += [
                f"[AWS change, approval] start {self.instance_id}; it is stopped again at cleanup because the suite started it",
                f"[EC2 change] schedule an OS shutdown guard (instance shutdown behavior: {p.shutdown_behavior or 'unknown'}); cancelled at cleanup",
                f"update HostName for '{SSH_ALIAS}' in {self.ssh_config} (backup kept); the pinned host key must still match",
            ]
        else:
            steps.append(f"instance is {p.state}: it is left {p.state} at the end")
        for name in self.scenarios:
            gate = "" if name == "healthy_precheck" else "[fault, approval] "
            steps.append(f"{gate}{name}: {self.expectations[name].spec['scenario']} (~{self.d.scenario(name) // 60} min, bounded by systemd RuntimeMaxSec)")
            if name == "db_lock":
                steps.append("  db_lock: verified backup via SQLite backup API; on-instance safety restore timer; restore + integrity/hash check after")
            steps.append(f"  then: wait for CloudWatch, collect with {self.x.readonly_profile}, validate signals, run CloudRCA, compare with frozen expectation")
        steps.append("cleanup (always, even on error): stop fault units, restore DB if needed, verify recovery, cancel guard, revert SG rule, stop instance if started")
        steps.append(f"estimated instance time {hours:.1f} h, estimated cost about US${cost:.2f} (an estimate, not a guarantee); OpenRouter free model only")
        return steps

    # ---------- execute ----------
    def run(self) -> int:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "expectations").mkdir(exist_ok=True)
        for name, frozen in self.expectations.items():
            (self.run_dir / "expectations" / f"{name}.expected.json").write_bytes(frozen.raw)
        p = self.preflight()
        self.out("Preflight: " + json.dumps({k: v for k, v in vars(p).items() if k != "problems"}))
        for line in self.plan(p):
            self.out(f"  - {line}")
        if not self.x.execute:
            self.out("Dry run: no AWS or EC2 changes were made. Re-run with --execute to be asked for each approval.")
            return 1 if p.problems else 0
        if p.problems:
            self.out("Refusing to execute:\n  " + "\n  ".join(p.problems))
            return 1
        try:
            self._approve_setup(p)
            self._setup(p)
            for name in self.scenarios:
                if name != "healthy_precheck":
                    self.gate.approve(name, f"Inject fault '{name}': {self.expectations[name].spec['scenario']}")
                    self.x.approved.add(name)
                self.state.results.append(self._scenario(name))
                if self.state.results[-1]["recovery"]:
                    self.out(f"Recovery after {name} failed; no further faults will be injected.")
                    break
                if name == "healthy_precheck" and not self.state.results[-1]["comparison"]["passed"]:
                    self.out("Healthy precheck failed; no faults will be injected.")
                    break
        except ApprovalRefused as exc:
            self.out(f"Stopped: {exc}")
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - collector/converter exit via SystemExit; all reach cleanup and the report
            self.out(f"Error: {exc}")
            self.state.results.append({"scenario": "suite", "error": str(exc)[:500]})
        finally:
            self._cleanup(p)
        return self._report(p)

    def _approve_setup(self, p: Preflight) -> None:
        self.gate.approve("begin", f"Begin the fault suite on {self.instance_id} (healthy precheck traffic, then one gate per fault)")
        self.x.approved.add("cleanup")
        if p.ssh_rule_ok is False:
            self.gate.approve("sg", f"Add SSH ingress {p.public_ip}/32 to {self.security_group} (removed at cleanup)")
            self.x.approved.add("sg")
        if p.state == "stopped":
            self.gate.approve("start", f"Start {self.instance_id} and stop it again at cleanup (estimate {self.estimate_seconds(p) / 3600:.1f} h)")
            self.x.approved.update({"start", "stop", "guard"})

    def _setup(self, p: Preflight) -> None:
        if p.ssh_rule_ok is False:
            # Record what existed and that we are about to add a rule, so an ambiguous failure can be reconciled.
            self._attempted_ip = p.public_ip
            self.state.preexisting_rule_ids = frozenset(str(rule.get("SecurityGroupRuleId")) for rule in self._ssh_rules(p.public_ip))
            self.state.rule_attempted = True
            result = self.x.aws("ec2", "authorize-security-group-ingress", "--group-id", self.security_group, "--ip-permissions",
                                f"IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{{CidrIp={p.public_ip}/32,Description=cloudrca-fault-suite}}]",
                                approval="sg")
            self.state.added_rule_id = (result.get("SecurityGroupRules") or [{}])[0].get("SecurityGroupRuleId")
        if p.state == "stopped":
            # Set before dispatch: if the call times out after AWS accepted it, cleanup still stops the instance
            # (stopping an instance that never started is harmless). An instance that was running is never stopped.
            self.state.started_instance = True
            self.x.aws("ec2", "start-instances", "--instance-ids", self.instance_id, approval="start")
            self._wait_status_ok()
            self._update_ssh_host()
            self._arm_guard(self.d.boot_settle + 30 * 60)
        status = remote.parse_status(self.x.ssh(remote.status_script(), mutating=False, label="status"))
        if status.get("marker") == "present":
            self.state.manual_actions.append(
                f"a previous db_lock restore did not complete: restore {remote.DB_PATH} from {remote.BACKUP_PATH} "
                f"(verify against {remote.BACKUP_HASH}), then remove {remote.MARKER}"
            )
            raise RuntimeError("a previous db_lock restore did not complete (marker present); no traffic or faults were started")
        if status.get("service") != "active" or status.get("api") != "200":
            raise RuntimeError(f"Splitwise is not healthy before testing: {status}")
        uptime = int(status.get("uptime_s", "0") or 0)
        if uptime < self.d.boot_settle:
            self.out(f"Waiting {self.d.boot_settle - uptime} s for CPU credits to settle after boot")
            self.sleep(self.d.boot_settle - uptime)

    def _ssh_rules(self, ip: str) -> list[dict[str, Any]]:
        rules = self.x.aws("ec2", "describe-security-group-rules", "--filters", f"Name=group-id,Values={self.security_group}").get("SecurityGroupRules", [])
        return [rule for rule in rules if not rule.get("IsEgress") and rule.get("FromPort") == 22 and rule.get("CidrIpv4") == f"{ip}/32"]

    def _reconcile_rule(self, ip: str) -> None:
        """After an ambiguous authorize call, find only the rule this run created (never a preexisting one)."""
        created = [rule for rule in self._ssh_rules(ip) if str(rule.get("SecurityGroupRuleId")) not in self.state.preexisting_rule_ids]
        if len(created) > 1:
            raise RuntimeError(f"cannot tell which SSH rule this run created: {[rule.get('SecurityGroupRuleId') for rule in created]}")
        self.state.added_rule_id = str(created[0]["SecurityGroupRuleId"]) if created else None

    def _arm_guard(self, seconds: int) -> None:
        """(Re)arm the on-instance guard; it cleans up and powers off only if the suite stops cancelling it."""
        self.x.ssh(remote.arm_shutdown_guard(max(1, seconds // 60)), mutating=True, approval="guard", label="arm shutdown guard")
        self.state.guard_armed = True

    def _wait_status_ok(self) -> None:
        for _ in range(60):
            statuses = self.x.aws("ec2", "describe-instance-status", "--instance-ids", self.instance_id).get("InstanceStatuses", [])
            if statuses and statuses[0].get("InstanceStatus", {}).get("Status") == "ok" and statuses[0].get("SystemStatus", {}).get("Status") == "ok":
                return
            self.sleep(self.d.poll)
        raise RuntimeError("instance status checks did not pass")

    def _update_ssh_host(self) -> None:
        reservations = self.x.aws("ec2", "describe-instances", "--instance-ids", self.instance_id).get("Reservations", [])
        dns = reservations[0]["Instances"][0].get("PublicDnsName", "")
        if not dns:
            raise RuntimeError("instance has no public DNS name")
        text = self.ssh_config.read_text()
        block = re.compile(rf"(^Host {re.escape(SSH_ALIAS)}\n(?:[ \t]+.*\n)*?[ \t]+HostName )\S+", re.MULTILINE)
        if not block.search(text):
            raise RuntimeError(f"no 'Host {SSH_ALIAS}' block with HostName in {self.ssh_config}")
        self.ssh_config.with_suffix(".cloudrca-backup").write_text(text)
        self.ssh_config.write_text(block.sub(lambda match: match.group(1) + dns, text, count=1))

    def _scenario(self, name: str) -> dict[str, Any]:
        seconds = self.d.scenario(name)
        if self.state.guard_armed:  # scenario + CloudWatch wait + collection/RCA, with margin
            self._arm_guard(seconds + self.d.cloudwatch_timeout + 30 * 60)
        start = self.clock()
        if name == "db_lock":
            backup = remote.parse_status(self.x.ssh(remote.backup_db_script(), mutating=True, approval=name, label="verified DB backup"))
            self.state.db_backup_hash = backup.get("backup_logical_sha256")
            if not self.state.db_backup_hash:
                raise RuntimeError("database backup was not verified; db_lock not started")
            self.x.ssh(remote.arm_db_safety_restore(seconds + 15 * 60), mutating=True, approval=name, label="arm safety restore")
            self.state.db_lock_active = True
        script = {
            "healthy_precheck": remote.start_traffic(seconds),
            "app_bug": remote.start_traffic(seconds, [(method, f"/expenses/{900000 + index}", 60 + index * 40) for index in range(6) for method in ("PUT", "DELETE")]),
            "db_lock": remote.start_traffic(seconds, post_every=3) + remote.start_lock_holder(seconds - 60),
            "vm_pressure": remote.start_traffic(seconds) + remote.start_cpu_burner(seconds),
        }[name]
        self.x.ssh(script, mutating=True, approval=name if name != "healthy_precheck" else "cleanup", label=f"start {name}")
        self.sleep(seconds)
        end = self.clock()
        self.x.ssh(remote.stop_faults_script(), mutating=True, approval="cleanup", label="stop faults")
        if name == "db_lock":
            self._restore_db()
        recovery = self._verify_recovery(f"after {name}")
        self._wait_for_cloudwatch(end)
        window = ((start - timedelta(minutes=2)).replace(second=0, microsecond=0), (end + timedelta(minutes=2)).replace(second=0, microsecond=0))
        out_dir = self.run_dir / name
        collect_args = collect_splitwise_incident.build_parser().parse_args([
            "--instance-id", self.instance_id, "--region", self.x.region, "--profile", self.x.readonly_profile,
            "--start", window[0].isoformat().replace("+00:00", "Z"), "--end", window[1].isoformat().replace("+00:00", "Z"),
            "--ssh-host", SSH_ALIAS, "--out-dir", str(out_dir),
        ])
        collect_splitwise_incident.collect(collect_args, self.x.collector_runner)
        artifacts = self.analyze(out_dir / "incident.jsonl")
        (out_dir / "rca_artifacts.json").write_text(json.dumps(artifacts, indent=1, default=str))
        comparison = rca_comparison.compare(self.expectations[name], artifacts, self.instance_id)
        return {"scenario": name, "window": [item.isoformat() for item in window], "recovery": recovery, "comparison": comparison.as_dict()}

    def _restore_db(self) -> None:
        restored = remote.parse_status(self.x.ssh(remote.restore_db_script(), mutating=True, approval="cleanup", label="restore DB"))
        if restored.get("restored_logical_sha256") != self.state.db_backup_hash:
            raise RuntimeError("database restore could not be verified against the backup")
        self.state.db_lock_active = False

    def _verify_recovery(self, when: str) -> list[str]:
        status = remote.parse_status(self.x.ssh(remote.verify_recovery_script(self.state.db_backup_hash), mutating=False, label=f"verify recovery {when}"))
        problems = remote.recovery_problems(status)
        for problem in problems:
            self.state.manual_actions.append(f"{when}: {problem}")
        return problems

    def _wait_for_cloudwatch(self, end: datetime) -> None:
        deadline = self.clock() + timedelta(seconds=self.d.cloudwatch_timeout)
        query = json.dumps([{"Id": "cpu", "MetricStat": {"Metric": {"Namespace": "AWS/EC2", "MetricName": "CPUUtilization",
                             "Dimensions": [{"Name": "InstanceId", "Value": self.instance_id}]}, "Period": 300, "Stat": "Average"}}])
        while self.clock() < deadline:
            data = self.x.aws("cloudwatch", "get-metric-data", "--metric-data-queries", query,
                              "--start-time", (end - timedelta(minutes=30)).isoformat(), "--end-time", (end + timedelta(minutes=10)).isoformat())
            stamps = [datetime.fromisoformat(str(item).replace("Z", "+00:00")) for result in data.get("MetricDataResults", []) for item in result.get("Timestamps", [])]
            if stamps and max(stamps) >= end - timedelta(minutes=5):
                return
            self.sleep(self.d.poll)
        raise RuntimeError("CloudWatch metrics did not cover the window in time")

    def _cleanup(self, p: Preflight) -> None:
        """Always runs. Each step is attempted even if an earlier one fails; every failure becomes a manual action."""

        def stop_faults() -> None:
            self.x.ssh(remote.stop_faults_script(), mutating=True, approval="cleanup", label="stop faults")

        def verify() -> None:
            self._verify_recovery("final")

        def cancel_guard() -> None:
            self.x.ssh(remote.cancel_shutdown_guard(), mutating=True, approval="guard", label="cancel guard")

        def revoke_rule() -> None:
            self.x.aws("ec2", "revoke-security-group-ingress", "--group-id", self.security_group,
                       "--security-group-rule-ids", str(self.state.added_rule_id), approval="sg")

        steps: list[tuple[str, Callable[[], None]]] = []
        if self.state.remote_touched:
            steps.append(("stop fault units", stop_faults))
            if self.state.db_lock_active:
                steps.append(("restore database", self._restore_db))
            steps.append(("verify recovery", verify))
        # When the suite started the instance, the guard is NOT cancelled: if the stop below fails, the guard still
        # powers it off. A confirmed stop clears the guard, because transient systemd timers do not survive a stop.
        if self.state.guard_armed and not self.state.started_instance:
            steps.append(("cancel shutdown guard", cancel_guard))
        if self.state.rule_attempted and not self.state.added_rule_id:
            steps.append(("find the SSH rule this run may have created", lambda: self._reconcile_rule(self._attempted_ip)))
        steps.append(("remove temporary SSH rule", lambda: revoke_rule() if self.state.added_rule_id else None))
        if self.state.started_instance:
            steps.append(("stop instance (the suite started it)", self._stop_instance))
        for label, action in steps:
            try:
                action()
            except Exception as exc:  # noqa: BLE001
                self.state.manual_actions.append(f"cleanup '{label}' failed: {str(exc)[:300]}")
        try:
            final = self.x.aws("ec2", "describe-instances", "--instance-ids", self.instance_id)["Reservations"][0]["Instances"][0]["State"]["Name"]
        except Exception as exc:  # noqa: BLE001
            final = f"unknown ({str(exc)[:120]})"
        expected = "stopped" if self.state.started_instance else p.state
        if final != expected:
            self.state.manual_actions.append(f"instance is {final}, expected {expected}")

    def _stop_instance(self) -> None:
        self.x.aws("ec2", "stop-instances", "--instance-ids", self.instance_id, approval="stop")
        for _ in range(40):
            state = self.x.aws("ec2", "describe-instances", "--instance-ids", self.instance_id)["Reservations"][0]["Instances"][0]["State"]["Name"]
            if state == "stopped":
                return
            self.sleep(self.d.poll)
        raise RuntimeError("instance did not reach stopped")

    def _report(self, p: Preflight) -> int:
        passed = [item for item in self.state.results if item.get("comparison", {}).get("passed")]
        lines = ["# CloudRCA fault suite", "", f"Instance `{self.instance_id}` ({p.instance_type}), started by suite: {self.state.started_instance}", ""]
        lines += ["| Scenario | Result | Failed checks |", "|---|---|---|"]
        for item in self.state.results:
            comparison = item.get("comparison", {})
            failed = [check["check"] for check in comparison.get("checks", []) if not check["passed"]]
            lines.append(f"| {item.get('scenario')} | {'PASS' if comparison.get('passed') else 'FAIL'} | {', '.join(failed) or item.get('error', '')} |")
        if self.state.manual_actions:
            lines += ["", "## CLEANUP INCOMPLETE: manual intervention required", *[f"- {item}" for item in self.state.manual_actions]]
        (self.run_dir / "report.md").write_text("\n".join(lines) + "\n")
        (self.run_dir / "results.json").write_text(json.dumps({"results": self.state.results, "manual_actions": self.state.manual_actions,
                                                              "actions": self.x.log}, indent=1, default=str))
        self.out("\n".join(lines))
        if self.state.manual_actions:
            return 2
        return 0 if len(passed) == len(self.scenarios) else 1


def _ollama_ok() -> bool:
    try:
        tags = json.loads(run_shell(["curl", "-s", "-m", "5", "http://localhost:11434/api/tags"]))
        return any("qwen2.5-coder" in str(item.get("name")) and "16k" in str(item.get("name")) for item in tags.get("models", []))
    except Exception:  # noqa: BLE001
        return False


def analyze_with_pipeline(dataset: Path) -> list[dict[str, Any]]:
    """Run CloudRCA in-process with the configured local Qwen specialists and free main RCA model."""
    from cloudrca_backend.pipeline import RcaPipeline, production_agents
    from cloudrca_backend.routing import load_routing_config

    pipeline = RcaPipeline(lambda _: (dataset.name, dataset.read_bytes()), production_agents, load_routing_config(ROOT / "config/routing/aiops2025.yaml"))
    result = pipeline({"dataset_id": dataset.parent.name})
    return [{"kind": kind.value, "payload": payload} for kind, payload in result.artifacts]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true", help="run for real; every AWS change and fault still needs typed approval")
    parser.add_argument("--scenarios", default=",".join(FAULTS), help=f"comma-separated subset of {','.join(FAULTS)} (healthy_precheck always runs first)")
    parser.add_argument("--readonly-profile", default="cloudrca-readonly")
    parser.add_argument("--operator-profile", default="cloudrca-operator")
    parser.add_argument("--run-dir", type=Path, default=ROOT / "collections" / f"fault-suite-{datetime.now(UTC):%Y%m%dT%H%M%SZ}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    executor = Executor(run_shell, execute=args.execute, readonly_profile=args.readonly_profile, operator_profile=args.operator_profile,
                        region=REGION, ssh_host=SSH_ALIAS, instance_id=INSTANCE_ID)
    suite = FaultSuite(executor, Gate(), scenarios=[item.strip() for item in args.scenarios.split(",") if item.strip()], run_dir=args.run_dir)
    return suite.run()


if __name__ == "__main__":
    raise SystemExit(main())
