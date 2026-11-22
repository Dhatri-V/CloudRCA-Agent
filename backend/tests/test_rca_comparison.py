"""Scenario expectations are frozen before execution and graded strictly; nothing here contacts AWS or EC2."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from scripts.rca_comparison import (
    LOCK_FILE,
    SCENARIO_DIR,
    ExpectationError,
    compare,
    freeze,
    lock_entries,
    verify_unchanged,
)

INSTANCE = "i-0suite00000000001"
ALL = ["healthy_precheck", "app_bug", "db_lock", "vm_pressure"]


def event(event_id: str, kind: str, layer: str, message: str = "m", **attributes: Any) -> dict[str, Any]:
    return {"kind": "event", "payload": {"event_id": event_id, "layer": layer, "message": message,
                                        "metadata": {"record_kind": kind, "source_attributes": attributes}}}


def passing_artifacts(name: str, instance: str = INSTANCE) -> list[dict[str, Any]]:
    """Hand-built artifacts that satisfy each frozen expectation exactly (used by the suite tests too)."""
    if name == "healthy_precheck":
        return [event("e-hb", "host.heartbeat", "vm_guest_os"),
                {"kind": "incident", "payload": {"analysis_outcome": "no_anomaly_detected", "anomaly_signals": {"count": 0, "signals": []}}}]
    errors, signals, hypothesis, cited = {
        "app_bug": ([event("e-put", "app.error", "application", "AttributeError: 'NoneType' object has no attribute 'title'", error_class="AttributeError", reported_by="application"),
                     event("e-del", "app.error", "application", "sqlalchemy.orm.exc.UnmappedInstanceError: Class 'builtins.NoneType' is not mapped",
                           error_class="sqlalchemy.orm.exc.UnmappedInstanceError", reported_by="application")],
                    ["severity", "http_5xx"], ("application", "splitwise-api"), "e-put"),
        "db_lock": ([event("e-lock", "app.error", "database", "sqlalchemy.exc.OperationalError: database is locked",
                           error_class="sqlalchemy.exc.OperationalError", reported_by="database")],
                    ["severity", "http_5xx"], ("database", "splitwise-sqlite"), "e-lock"),
        "vm_pressure": ([event("e-credits", "aws.cloudwatch.metric", "vm_guest_os", metric_name="CPUCreditBalance", value=0)],
                        ["cpu_credits_exhausted", "load_per_cpu"], ("vm_guest_os", instance), "e-credits"),
    }[name]
    citation = [{"evidence_id": "c1", "kind": "event", "reference_id": cited}]
    return [
        *errors,
        {"kind": "incident", "payload": {"analysis_outcome": "rca_reported", "topology": {"relationships": []},
                                         "anomaly_signals": {"count": len(signals), "signals": [{"event_id": cited, "check": item, "observed": "x"} for item in signals]}}},
        {"kind": "report", "payload": {"root_cause_layer": hypothesis[0], "root_cause_component": {"component_id": hypothesis[1]},
                                       "evidence": citation, "causal_chain": [{"evidence": citation}], "alternative_hypotheses": [{"evidence": citation}]}},
    ]


def test_the_committed_lock_pins_every_expectation_file() -> None:
    assert json.loads(LOCK_FILE.read_text()) == lock_entries()
    assert set(lock_entries()) == {f"{name}.expected.json" for name in ALL}


def test_expectations_record_the_probed_splitwise_behaviour() -> None:
    frozen = freeze(ALL)
    app_errors = {item["error_class"]: item["message_contains"] for item in frozen["app_bug"].spec["required_errors"]}
    assert app_errors == {"AttributeError": "'NoneType' object has no attribute 'title'", "sqlalchemy.orm.exc.UnmappedInstanceError": "is not mapped"}
    [lock] = frozen["db_lock"].spec["required_errors"]
    assert (lock["reported_by"], lock["message_contains"]) == ("database", "database is locked")
    assert frozen["db_lock"].spec["expected_hypothesis"] == {"layer": "database", "component_id": "splitwise-sqlite"}
    assert frozen["vm_pressure"].resolved(INSTANCE)["expected_hypothesis"]["component_id"] == INSTANCE
    assert "85629fc" in frozen["app_bug"].spec["verified_against"]


def test_a_changed_expectation_is_refused_before_anything_runs(tmp_path: Path) -> None:
    directory = tmp_path / "scenarios"
    shutil.copytree(SCENARIO_DIR, directory)
    spec = json.loads((directory / "db_lock.expected.json").read_text())
    spec["expected_hypothesis"]["layer"] = "application"  # e.g. edited to match an observed RCA answer
    (directory / "db_lock.expected.json").write_text(json.dumps(spec))
    with pytest.raises(ExpectationError, match="does not match expectations.lock.json"):
        freeze(["db_lock"], directory)
    freeze(["app_bug"], directory)  # untouched files still load


def test_an_on_disk_change_during_the_run_fails_grading(tmp_path: Path) -> None:
    directory = tmp_path / "scenarios"
    shutil.copytree(SCENARIO_DIR, directory)
    frozen = freeze(["app_bug"], directory)["app_bug"]
    verify_unchanged(frozen, directory)
    (directory / "app_bug.expected.json").write_text("{}")
    with pytest.raises(ExpectationError, match="changed on disk during the run"):
        compare(frozen, passing_artifacts("app_bug"), INSTANCE, directory)


@pytest.mark.parametrize("name", ALL)
def test_matching_artifacts_pass_every_check(name: str) -> None:
    result = compare(freeze([name])[name], passing_artifacts(name), INSTANCE)
    assert result.passed, [item for item in result.checks if not item["passed"]]
    assert result.expectation_sha256 == lock_entries()[f"{name}.expected.json"]


@pytest.mark.parametrize(("name", "mutate", "failed"), [
    ("app_bug", lambda a: a[-1]["payload"].update(root_cause_layer="database"), "layer"),
    ("db_lock", lambda a: a[-1]["payload"]["root_cause_component"].update(component_id="splitwise-api"), "component"),
    ("app_bug", lambda a: a[-1]["payload"]["evidence"].append({"reference_id": "invented-id"}), "citations_known"),
    ("app_bug", lambda a: a.pop(1), "error:sqlalchemy.orm.exc.UnmappedInstanceError"),
    ("vm_pressure", lambda a: a[1]["payload"]["anomaly_signals"]["signals"].pop(), "signal:load_per_cpu"),
    ("db_lock", lambda a: a.pop(), "one_report"),
    ("db_lock", lambda a: a[1]["payload"].update(analysis_outcome="rca_failed"), "outcome"),
    ("healthy_precheck", lambda a: a[1]["payload"]["anomaly_signals"]["signals"].append({"check": "http_5xx"}), "no_signals"),
])
def test_any_mismatch_fails_and_names_the_check(name: str, mutate: Any, failed: str) -> None:
    artifacts = passing_artifacts(name)
    mutate(artifacts)
    result = compare(freeze([name])[name], artifacts, INSTANCE)
    assert not result.passed and failed in {item["check"] for item in result.checks if not item["passed"]}


def test_a_missing_incident_is_a_failure_not_a_skip() -> None:
    result = compare(freeze(["app_bug"])["app_bug"], [], INSTANCE)
    assert not result.passed and result.checks == [{"check": "one_incident", "passed": False, "detail": "0 incident(s)"}]
