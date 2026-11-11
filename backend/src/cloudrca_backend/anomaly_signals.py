"""Deterministic anomaly signals: observed evidence that something is wrong, computed without an LLM.

A signal names the event that carries it, what was checked, and the observed value. Signals never name a cause:
they decide only whether an incident has anything to explain. Thresholds are saturation or failure conditions
(an error was logged, a status check failed, a resource is exhausted), not tuned anomaly scores.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .contracts import NormalizedEvent, Severity

NOTABLE_SEVERITIES = frozenset({Severity.WARNING, Severity.ERROR, Severity.CRITICAL})
LOAD_PER_CPU = 1.0  # run queue at least as long as the CPU count
MIN_FREE_RATIO = 0.10  # 10% or less of memory or disk left
CPU_SATURATED_PERCENT = 90.0
EBS_BALANCE_LOW_PERCENT = 10.0
CHECKS = (
    "warning/error/critical severity",
    "application error records",
    "HTTP 5xx responses",
    "connection pool saturation or overflow",
    f"load per CPU >= {LOAD_PER_CPU:g}",
    f"memory or disk free <= {MIN_FREE_RATIO:.0%}",
    "EC2 status check failures",
    f"CPUUtilization >= {CPU_SATURATED_PERCENT:g}%",
    "CPU credits exhausted or surplus credits charged",
    f"EBS burst balance <= {EBS_BALANCE_LOW_PERCENT:g}%",
)


@dataclass(frozen=True, slots=True)
class AnomalySignal:
    event_id: str
    check: str
    observed: str

    def as_dict(self) -> dict[str, str]:
        return {"event_id": self.event_id, "check": self.check, "observed": self.observed}


def _number(attributes: dict[str, object], name: str) -> float | None:
    value = attributes.get(name)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _ratio(attributes: dict[str, object], part: str, whole: str) -> float | None:
    numerator, denominator = _number(attributes, part), _number(attributes, whole)
    return numerator / denominator if numerator is not None and denominator else None


def event_signals(event: NormalizedEvent) -> list[AnomalySignal]:
    found: list[AnomalySignal] = []

    def flag(check: str, observed: str) -> None:
        found.append(AnomalySignal(event.event_id, check, observed))

    kind = event.metadata.get("record_kind")
    raw = event.metadata.get("source_attributes")
    attributes: dict[str, object] = dict(raw) if isinstance(raw, dict) else {}
    if event.severity in NOTABLE_SEVERITIES:
        flag("severity", event.severity.value)
    if kind == "app.error" and event.severity not in NOTABLE_SEVERITIES:
        flag("application_error", str(attributes.get("error_class") or "error record"))
    if kind == "app.request_summary" and (errors := _number(attributes, "error_5xx_count")):
        flag("http_5xx", f"error_5xx_count={errors:g}")
    if kind == "app.pool_snapshot":
        size, checked_out, overflow = (_number(attributes, name) for name in ("db_pool_size", "db_pool_checked_out", "db_pool_overflow"))
        if size and checked_out is not None and checked_out >= size:
            flag("pool_saturated", f"db_pool_checked_out={checked_out:g} of db_pool_size={size:g}")
        if overflow:
            flag("pool_overflow", f"db_pool_overflow={overflow:g}")
    if kind == "host.heartbeat":
        load, cpus = _number(attributes, "load_1m"), _number(attributes, "cpu_count")
        if load is not None and cpus and load / cpus >= LOAD_PER_CPU:
            flag("load_per_cpu", f"load_1m={load:g} on cpu_count={cpus:g}")
        for part, whole, check in (("mem_available_mb", "mem_total_mb", "memory_low"), ("disk_free_mb", "disk_total_mb", "disk_low")):
            ratio = _ratio(attributes, part, whole)
            if ratio is not None and ratio <= MIN_FREE_RATIO:
                flag(check, f"{part}/{whole}={ratio:.1%}")
    if kind == "aws.cloudwatch.metric":
        name, value = str(attributes.get("metric_name") or ""), _number(attributes, "value")
        if value is None:
            return found
        if name.startswith("StatusCheckFailed") and value > 0:
            flag("status_check_failed", f"{name}={value:g}")
        elif name == "CPUUtilization" and value >= CPU_SATURATED_PERCENT:
            flag("cpu_saturated", f"{name}={value:g}%")
        elif name == "CPUCreditBalance" and value <= 0:
            flag("cpu_credits_exhausted", f"{name}={value:g}")
        elif name == "CPUSurplusCreditsCharged" and value > 0:
            flag("cpu_surplus_charged", f"{name}={value:g}")
        elif name in {"EBSIOBalance%", "EBSByteBalance%"} and value <= EBS_BALANCE_LOW_PERCENT:
            flag("ebs_balance_low", f"{name}={value:g}")
    return found


def anomaly_signals(events: Iterable[NormalizedEvent]) -> tuple[AnomalySignal, ...]:
    """Every deterministic signal in the events, in event order."""
    return tuple(signal for event in events for signal in event_signals(event))
