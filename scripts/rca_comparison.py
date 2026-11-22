"""Frozen scenario expectations and the comparison of a CloudRCA run against them (standard library only).

Expectations live in config/aws/scenarios/*.expected.json and are pinned by SHA-256 in expectations.lock.json.
They are loaded and hashed before any fault is injected, and re-verified before grading, so an expectation cannot be
changed after seeing an RCA result without the change showing up in a reviewed lock-file diff.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCENARIO_DIR = ROOT / "config" / "aws" / "scenarios"
LOCK_FILE = SCENARIO_DIR / "expectations.lock.json"


class ExpectationError(RuntimeError):
    """An expectation is missing, malformed, or does not match the committed lock."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def lock_entries(directory: Path = SCENARIO_DIR) -> dict[str, str]:
    """SHA-256 of every expectation file, as written to expectations.lock.json."""
    return {path.name: sha256_bytes(path.read_bytes()) for path in sorted(directory.glob("*.expected.json"))}


@dataclass(frozen=True)
class FrozenExpectation:
    name: str
    raw: bytes
    sha256: str
    spec: dict[str, Any]

    def resolved(self, instance_id: str) -> dict[str, Any]:
        """The spec with ${INSTANCE_ID} filled in; the frozen bytes are never modified."""
        resolved: dict[str, Any] = json.loads(self.raw.decode("utf-8").replace("${INSTANCE_ID}", instance_id))
        return resolved


def freeze(names: list[str], directory: Path = SCENARIO_DIR, lock_file: Path | None = None) -> dict[str, FrozenExpectation]:
    """Load and verify expectations before execution; any mismatch with the lock refuses the run."""
    lock_path = lock_file or directory / "expectations.lock.json"
    try:
        lock: dict[str, str] = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExpectationError(f"expectation lock {lock_path} is missing or unreadable") from exc
    frozen: dict[str, FrozenExpectation] = {}
    for name in names:
        path = directory / f"{name}.expected.json"
        if not path.is_file():
            raise ExpectationError(f"no expectation file for scenario {name!r}")
        raw = path.read_bytes()
        digest = sha256_bytes(raw)
        if lock.get(path.name) != digest:
            raise ExpectationError(f"{path.name} does not match expectations.lock.json; expectations change only in a reviewed PR")
        spec = json.loads(raw)
        if spec.get("name") != name or spec.get("expected_outcome") not in {"no_anomaly_detected", "rca_reported"}:
            raise ExpectationError(f"{path.name} is malformed")
        frozen[name] = FrozenExpectation(name, raw, digest, spec)
    return frozen


def verify_unchanged(frozen: FrozenExpectation, directory: Path = SCENARIO_DIR) -> None:
    """Before grading: the bytes used for grading still hash to the value recorded before injection."""
    if sha256_bytes(frozen.raw) != frozen.sha256:
        raise ExpectationError(f"frozen expectation {frozen.name} changed in memory")
    path = directory / f"{frozen.name}.expected.json"
    if not path.is_file() or sha256_bytes(path.read_bytes()) != frozen.sha256:
        raise ExpectationError(f"{path.name} changed on disk during the run")


@dataclass
class Comparison:
    scenario: str
    expectation_sha256: str
    checks: list[dict[str, Any]] = field(default_factory=list)

    def check(self, name: str, passed: bool, detail: str) -> None:
        self.checks.append({"check": name, "passed": bool(passed), "detail": detail})

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(item["passed"] for item in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {"scenario": self.scenario, "expectation_sha256": self.expectation_sha256, "passed": self.passed, "checks": self.checks}


def _of(artifacts: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [item["payload"] for item in artifacts if item.get("kind") == kind]


def compare(frozen: FrozenExpectation, artifacts: list[dict[str, Any]], instance_id: str, directory: Path = SCENARIO_DIR) -> Comparison:
    """Grade one run. Every check must pass; a missing artifact is a failure, never a skip."""
    verify_unchanged(frozen, directory)
    spec = frozen.resolved(instance_id)
    result = Comparison(frozen.name, frozen.sha256)
    incidents = _of(artifacts, "incident")
    events = {str(item["event_id"]): item for item in _of(artifacts, "event")}
    result.check("one_incident", len(incidents) == 1, f"{len(incidents)} incident(s)")
    if len(incidents) != 1:
        return result
    incident = incidents[0]
    outcome = incident.get("analysis_outcome")
    result.check("outcome", outcome == spec["expected_outcome"], f"expected {spec['expected_outcome']}, got {outcome}")
    signals = (incident.get("anomaly_signals") or {}).get("signals", [])
    found = {item.get("check") for item in signals}
    if spec.get("forbidden_signals") == "any":
        result.check("no_signals", not signals, f"{len(signals)} signal(s): {sorted(found)}")
    for name in spec.get("required_signals", []):
        result.check(f"signal:{name}", name in found, f"present: {sorted(found)}")
    errors = [item for item in events.values() if (item.get("metadata") or {}).get("record_kind") == "app.error"]
    for required in spec.get("required_errors", []):
        matched = [
            item for item in errors
            if (attributes := (item.get("metadata") or {}).get("source_attributes") or {}).get("error_class") == required["error_class"]
            and attributes.get("reported_by") == required["reported_by"]
            and required["message_contains"] in str(item.get("message", ""))
        ]
        result.check(f"error:{required['error_class']}", bool(matched), f"{len(matched)} matching error event(s)")
    if spec["expected_outcome"] != "rca_reported":
        result.check("no_report", not _of(artifacts, "report"), "no RCA report for a healthy window")
        return result
    reports = _of(artifacts, "report")
    result.check("one_report", len(reports) == 1, f"{len(reports)} report(s)")
    if len(reports) != 1:
        return result
    report = reports[0]
    hypothesis = spec["expected_hypothesis"]
    result.check("layer", report.get("root_cause_layer") == hypothesis["layer"], f"expected {hypothesis['layer']}, got {report.get('root_cause_layer')}")
    component = (report.get("root_cause_component") or {}).get("component_id")
    result.check("component", component == hypothesis["component_id"], f"expected {hypothesis['component_id']}, got {component}")
    references = [
        *report.get("evidence", []),
        *(item for link in report.get("causal_chain", []) for item in link.get("evidence", [])),
        *(item for alternative in report.get("alternative_hypotheses", []) for item in alternative.get("evidence", [])),
    ]
    known = set(events) | {item["finding_id"] for item in _of(artifacts, "finding")} | {
        item["relationship_id"] for item in (incident.get("topology") or {}).get("relationships", [])
    }
    unknown = sorted({str(item.get("reference_id")) for item in references} - known)
    result.check("citations_known", not unknown and bool(references), f"{len(references)} citation(s), unknown: {unknown}")
    kinds = set(spec.get("supporting_evidence_kinds", []))
    supporting = sorted({
        str(item.get("reference_id")) for item in references
        if (events.get(str(item.get("reference_id"))) or {}).get("metadata", {}).get("record_kind") in kinds
    })
    result.check("supporting_evidence_cited", bool(supporting), f"{len(supporting)} cited event(s) of kinds {sorted(kinds)}")
    return result
