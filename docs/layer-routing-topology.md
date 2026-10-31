# Layer routing and topology

Issue #6 adds deterministic routing after normalization. It does not call an AI model and does not run specialist agents.

```text
Canonical event
      ↓
configured routing rules
      ↓
database / VM / hypervisor / unknown / ambiguous
      ↓
observed topology + explicit unavailable relationships
```

## Routing rules

The AIOps2025 aliases and fallback patterns live in `config/routing/aiops2025.yaml`. Exact component identifiers have the highest precedence, exact metadata aliases come next, and message fallback patterns come last. Exact metadata therefore wins over a vague message match. Each result includes its confidence, explanation, matching evidence, and candidate layers.

Examples:

- `database_id = redis-cart-0` routes to **database** with confidence `1.0`.
- `vm_id = vm-worker-03` without a more specific component routes to **VM** with confidence `1.0`.
- `service = libvirt` routes to **hypervisor** using an exact configured alias with confidence `0.95`.
- `message = "database connection failed"` uses a configured fallback with confidence `0.55` when stronger metadata is absent.
- No match produces **unknown** with confidence `0.0`.
- Equally strong matches for different layers produce **ambiguous** and retain every winning match.

Load and use the rules:

```python
from cloudrca_backend.routing import load_routing_config, route_and_resolve

config = load_routing_config("config/routing/aiops2025.yaml")
routed = route_and_resolve(event, config)
print(routed.routing.layer, routed.routing.explanation)
```

## Topology boundaries

The resolver follows the Issue #1 evidence boundary:

- A real AIOps2025 event that co-records a Redis pod and worker produces an **observed** Redis pod → VM relationship.
- TiDB, TiKV, and PD placement is reported as unavailable because the inspected records did not prove it.
- VM → physical host placement is reported as unavailable because AIOps2025 does not contain it.
- A VM → host relationship is added only when the caller supplies an explicit `TopologyAugmentation`. The relationship retains `augmented` or `synthetic` provenance, scenario ID, and generator version. An augmentation cannot claim to be observed.

The canonical Issue #4 and normalization Issue #5 contracts did not change. `RoutingDecision` is a separate envelope so `unknown` and `ambiguous` do not pretend to be infrastructure layers.

## Observed topology for CloudRCA records (Splitwise)

For CloudRCA records (`cloudrca_record`), `resolve_topology` creates a relationship only when one record names both identifiers:

| Identifiers in the same record | Relationship |
| --- | --- |
| application `service` + instance (`vm_id`) | service `runs_on` VM |
| database-reported `database_id` + instance | database `runs_on` VM |
| `reporting_service` (or an application error's `database_id_context`) + `database_id` | service `depends_on` database |

Relationships are `observed`, cite the supporting event, and have a stable ID per edge, so the pipeline merges repeated observations of one edge into a single relationship with pooled evidence and a widened validity window (`pipeline.incident_topology`). Component names never create relationships. Missing identifiers produce an explicit `unavailable` entry instead of a relationship; an identifier used for both endpoints is reported as conflicting and yields no relationship. The same database observed on two instances stays two separate observations. EC2 instances never get a host relationship: AWS does not expose hypervisor placement, which is recorded as `unavailable`. The merged topology is persisted with each incident and passed to the main RCA, whose prompt states that topology is structure, not evidence of causation. AIOps2025 topology rules are unchanged.
