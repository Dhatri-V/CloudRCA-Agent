"""R3: observed Splitwise topology comes only from identifiers co-recorded in one record, with its evidence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cloudrca_backend.contracts import (
    ComponentKind,
    Layer,
    NormalizedEvent,
    Provenance,
    ProvenanceKind,
    Severity,
)
from cloudrca_backend.normalization import normalize_file
from cloudrca_backend.pipeline import RcaPipeline, incident_topology
from cloudrca_backend.routing import (
    AWS_HOST_UNAVAILABLE,
    RoutedEvent,
    load_routing_config,
    route_and_resolve,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "splitwise" / "cloudrca_records.jsonl"
ROUTING = load_routing_config(ROOT / "config" / "routing" / "aiops2025.yaml")


def routed_fixture() -> dict[str, RoutedEvent]:
    return {event.provenance.record_id: route_and_resolve(event, ROUTING) for event in normalize_file(FIXTURE).events}  # type: ignore[misc]


def edges(routed: RoutedEvent) -> set[tuple[str, str, str]]:
    return {(item.source.component_id, item.relationship_type.value, item.target.component_id) for item in routed.topology.relationships}


def cloudrca_event(event_id: str, layer: Layer, **fields: Any) -> NormalizedEvent:
    attributes = fields.pop("attributes", {})
    return NormalizedEvent(
        event_id=event_id, timestamp=datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc), source="splitwise.applog", layer=layer,
        severity=Severity.ERROR, message="SYNTHETIC: test record",
        metadata={"record_kind": "app.error", "source_attributes": attributes},
        provenance=Provenance(kind=ProvenanceKind.OBSERVED, source="splitwise.applog", record_id=event_id), **fields,
    )


def test_relationships_come_only_from_identifiers_in_the_same_record() -> None:
    routed = routed_fixture()
    assert edges(routed["summary-1001"]) == {("splitwise-api", "runs_on", "i-0synthetic01")}
    assert edges(routed["err-1001-b"]) == {("splitwise-api", "runs_on", "i-0synthetic01")}
    assert edges(routed["err-1001-a"]) == {
        ("splitwise-sqlite", "runs_on", "i-0synthetic01"),
        ("splitwise-api", "depends_on", "splitwise-sqlite"),
    }
    # VM-only records name no service or database, so they state no service/database relationship.
    assert edges(routed["metric-cpu-1000"]) == set() and edges(routed["hb-1000"]) == set()


def test_each_relationship_cites_the_event_that_supports_it() -> None:
    routed = routed_fixture()
    for item in routed.values():
        for relationship in item.topology.relationships:
            assert relationship.provenance.kind is ProvenanceKind.OBSERVED
            assert [reference.reference_id for reference in relationship.evidence] == [item.event.event_id]
            assert relationship.validity is not None and relationship.validity.start == item.event.timestamp


def test_repeated_observations_merge_their_evidence_without_new_relationships() -> None:
    routed = routed_fixture()
    topology = incident_topology(tuple(routed.values()))
    by_edge = {(item.source.component_id, item.relationship_type.value, item.target.component_id): item for item in topology.relationships}
    assert set(by_edge) == {
        ("splitwise-api", "runs_on", "i-0synthetic01"),
        ("splitwise-sqlite", "runs_on", "i-0synthetic01"),
        ("splitwise-api", "depends_on", "splitwise-sqlite"),
    }
    runs_on = by_edge[("splitwise-api", "runs_on", "i-0synthetic01")]
    assert {reference.reference_id for reference in runs_on.evidence} == {routed["summary-1001"].event.event_id, routed["err-1001-b"].event.event_id}
    assert runs_on.validity is not None and runs_on.validity.start < runs_on.validity.end


def test_no_hypervisor_relationship_or_host_is_invented() -> None:
    for item in routed_fixture().values():
        assert all(relationship.target.kind is not ComponentKind.HOST for relationship in item.topology.relationships)
        assert item.event.host_id is None
        host_gaps = [gap for gap in item.topology.unavailable if gap.target_kind is ComponentKind.HOST]
        assert [gap.reason for gap in host_gaps] == [AWS_HOST_UNAVAILABLE]


def test_missing_identifiers_yield_explicit_gaps_not_relationships() -> None:
    no_instance = route_and_resolve(cloudrca_event("app-no-vm", Layer.APPLICATION, service="splitwise-api"), ROUTING)
    assert no_instance.topology.relationships == ()
    [gap] = no_instance.topology.unavailable
    assert gap.source.component_id == "splitwise-api" and "does not name the instance" in gap.reason
    no_reporter = route_and_resolve(cloudrca_event("db-no-reporter", Layer.DATABASE, database_id="splitwise-sqlite", vm_id="i-1"), ROUTING)
    assert edges(no_reporter) == {("splitwise-sqlite", "runs_on", "i-1")}  # no depends_on without a named service


def test_conflicting_identifiers_yield_no_relationship() -> None:
    same_as_instance = route_and_resolve(cloudrca_event("app-conflict", Layer.APPLICATION, service="i-1", vm_id="i-1"), ROUTING)
    assert same_as_instance.topology.relationships == ()
    assert any("Conflicting identifiers" in gap.reason for gap in same_as_instance.topology.unavailable)
    reporter_is_database = route_and_resolve(
        cloudrca_event("db-conflict", Layer.DATABASE, database_id="shared", vm_id="i-1", attributes={"reporting_service": "shared"}), ROUTING
    )
    assert edges(reporter_is_database) == {("shared", "runs_on", "i-1")}
    assert any("Conflicting identifiers" in gap.reason for gap in reporter_is_database.topology.unavailable)


def test_one_database_seen_on_two_instances_stays_two_separate_observations() -> None:
    first = route_and_resolve(cloudrca_event("db-on-1", Layer.DATABASE, database_id="splitwise-sqlite", vm_id="i-1"), ROUTING)
    second = route_and_resolve(cloudrca_event("db-on-2", Layer.DATABASE, database_id="splitwise-sqlite", vm_id="i-2"), ROUTING)
    topology = incident_topology((first, second))
    assert {(item.target.component_id, tuple(ref.reference_id for ref in item.evidence)) for item in topology.relationships} == {
        ("i-1", ("db-on-1",)), ("i-2", ("db-on-2",)),
    }


def test_component_names_alone_never_create_relationships() -> None:
    mentions_database = route_and_resolve(
        cloudrca_event("app-mentions", Layer.APPLICATION, service="splitwise-api", vm_id="i-1"), ROUTING
    )
    assert ("splitwise-api", "depends_on", "splitwise-sqlite") not in edges(mentions_database)
    named_like_database = route_and_resolve(
        cloudrca_event("app-named", Layer.APPLICATION, service="splitwise-sqlite", vm_id="i-1"), ROUTING
    )
    assert edges(named_like_database) == {("splitwise-sqlite", "runs_on", "i-1")}
    assert all(item.source.kind is ComponentKind.SERVICE for item in named_like_database.topology.relationships)


def test_aiops2025_topology_is_unchanged() -> None:
    batch = normalize_file(ROOT / "tests" / "fixtures" / "aiops2025" / "logs.parquet")
    for item in (route_and_resolve(event, ROUTING) for event in batch.events):
        assert [relationship.provenance.source for relationship in item.topology.relationships] == ["AIOps2025 co-recorded pod/node placement"]
        assert [gap.reason for gap in item.topology.unavailable] == ["AIOps2025 does not provide real VM-to-physical-host placement."]


def test_main_rca_receives_topology_and_may_cite_it(scripted_jcode: Any, agents_for: Any) -> None:
    class CitesTopology(scripted_jcode):  # type: ignore[misc, valid-type]
        def respond(self, prompt: str) -> str:
            text = super().respond(prompt)
            if "CLOUDRCA_MAIN_RCA_PROMPT_VERSION=" not in prompt:
                return text
            payload = json.loads(next(line for line in prompt.splitlines() if line.startswith("INPUT=")).removeprefix("INPUT="))
            relationship_id = payload["topology"]["relationships"][0]["relationship_id"]
            report = json.loads(text)
            report["evidence"].append({"evidence_id": "cite-topology", "kind": "topology", "reference_id": relationship_id})
            return json.dumps(report)

    transport = CitesTopology()
    result = RcaPipeline(lambda _: (FIXTURE.name, FIXTURE.read_bytes()), lambda: agents_for(transport), ROUTING)({"dataset_id": "d"})
    [rca_input] = transport.prompts["main_rca"]
    relationships = rca_input["topology"]["relationships"]
    assert {(item["source"]["component_id"], item["relationship_type"], item["target"]["component_id"]) for item in relationships} == {
        ("splitwise-api", "runs_on", "i-0synthetic01"),
        ("splitwise-sqlite", "runs_on", "i-0synthetic01"),
        ("splitwise-api", "depends_on", "splitwise-sqlite"),
    }
    assert all(item["evidence_reference_ids"] for item in relationships)
    assert any(gap["target_kind"] == "host" and gap["reason"] == AWS_HOST_UNAVAILABLE for gap in rca_input["topology"]["unavailable"])
    [report] = [payload for kind, payload in result.artifacts if kind.value == "report"]
    assert any(item["kind"] == "topology" for item in report["evidence"])
    [incident] = [payload for kind, payload in result.artifacts if kind.value == "incident"]
    assert len(incident["topology"]["relationships"]) == 3  # persisted with the incident, evidence IDs included


def test_main_rca_prompt_states_topology_is_not_causation(scripted_jcode: Any, agents_for: Any) -> None:
    from cloudrca_backend.correlation import CorrelationGraph
    from cloudrca_backend.incident_grouping import GroupingConfig, IncidentGrouper
    from cloudrca_backend.main_rca_agent import MainRcaRequest, build_main_rca_prompt

    routed = tuple(routed_fixture().values())
    [incident] = IncidentGrouper(GroupingConfig()).group(routed).incidents
    prompt = build_main_rca_prompt(
        MainRcaRequest(incident=incident, graph=CorrelationGraph(incident_id=incident.incident_id, nodes=()), topology=incident_topology(routed)), "v1"
    )
    assert "it is not evidence that one component caused another's failure" in prompt
