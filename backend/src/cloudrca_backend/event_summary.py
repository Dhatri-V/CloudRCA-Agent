"""Evidence-preserving event summarization shared by the main RCA and the specialist prompts.

Small batches are listed in full. Larger ones keep distinct warnings/errors (and, when supplied, events carrying a
deterministic anomaly signal) verbatim, first and last occurrences of repeats verbatim, and every other event as a
per-component value series with its UTC time offset, so nothing is dropped and time order is kept.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

from .contracts import NormalizedEvent, Severity

VERBATIM_EVENT_LIMIT = 40
NOTABLE_HEAD, NOTABLE_TAIL = 3, 2  # occurrences of one repeated warning/error kept verbatim at each end
NOTABLE_SEVERITIES = frozenset({Severity.WARNING, Severity.ERROR, Severity.CRITICAL})
IDENTIFIERS = ("service", "vm_id", "pod_id", "database_id", "host_id")
SUMMARY_NOTE = (
    "Each distinct warning/error (first and last occurrences of repeats) and each series' endpoints are "
    "listed in events; every other event is in event_series with its values and UTC time, and specialist-cited "
    "events there are named in cited_event_ids. Cite the series' event IDs when relying on a series."
)


@dataclass(frozen=True, slots=True)
class EventSummary:
    verbatim: list[NormalizedEvent]  # in time order
    series: list[dict[str, object]]
    summary: dict[str, object]  # empty when every event is listed verbatim


def cited_event_ids(value: object) -> set[str]:
    """Event IDs cited anywhere inside specialist finding payloads (evidence_event_ids lists)."""
    if isinstance(value, dict):
        found = {item for item in value.get("evidence_event_ids", []) if isinstance(item, str)} if isinstance(value.get("evidence_event_ids"), list) else set()
        return found.union(*(cited_event_ids(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(cited_event_ids(item) for item in value))
    return set()


def measures(event: NormalizedEvent) -> dict[str, float]:
    attributes = event.metadata.get("source_attributes")
    if not isinstance(attributes, dict):
        return {}
    return {key: float(value) for key, value in attributes.items() if isinstance(value, (int, float)) and not isinstance(value, bool)}


def compact_number(value: float) -> float | int:
    """Lossless and compact: integral values print without a trailing .0."""
    return int(value) if float(value).is_integer() else round(value, 3)


def series_key(event: NormalizedEvent) -> tuple[str, ...]:
    attributes = event.metadata.get("source_attributes")
    metric = attributes.get("metric_name") if isinstance(attributes, dict) else None
    ids = tuple(getattr(event, name) or "" for name in IDENTIFIERS)
    return (str(event.metadata.get("record_kind") or event.source), event.layer.value, str(metric or ""), *ids)


def summarize_events(
    events: Iterable[NormalizedEvent],
    *,
    cited: Collection[str] = frozenset(),
    signals: Mapping[str, str] | None = None,
    limit: int = VERBATIM_EVENT_LIMIT,
    note: str = SUMMARY_NOTE,
) -> EventSummary:
    """Summarize events above ``limit``; ``signals`` maps event IDs to an anomaly check that must stay visible.

    Every condensed value is kept with its UTC time, and each series names real event IDs (first, last, and the
    minimum and maximum of each measure) so conclusions stay citeable.
    """
    ordered = sorted(events, key=lambda item: (item.timestamp, item.event_id))
    if len(ordered) <= limit:
        return EventSummary(ordered, [], {})
    signals = signals or {}
    groups: dict[tuple[str, ...], list[NormalizedEvent]] = {}
    notable: dict[tuple[str, ...], list[NormalizedEvent]] = {}
    verbatim: dict[str, NormalizedEvent] = {}
    for event in ordered:
        if event.severity in NOTABLE_SEVERITIES or event.metadata.get("record_kind") == "app.error":
            notable.setdefault((*series_key(event), event.severity.value, event.message), []).append(event)
        elif event.event_id in signals:
            notable.setdefault((*series_key(event), "signal", signals[event.event_id]), []).append(event)
        else:
            groups.setdefault(series_key(event), []).append(event)
    # A repeated warning/error/signal (same kind, component, and severity+message or check) keeps its first and last
    # occurrences verbatim; the occurrences in between join a series that keeps each one's time and numeric fields.
    for key, members in notable.items():
        for event in members[:NOTABLE_HEAD] + members[-NOTABLE_TAIL:]:
            verbatim[event.event_id] = event
        if len(members) > NOTABLE_HEAD + NOTABLE_TAIL:
            groups.setdefault(key, []).extend(members[NOTABLE_HEAD:-NOTABLE_TAIL])
    series: list[dict[str, object]] = []
    for key, members in groups.items():
        repeated_notable = len(key) > len(series_key(members[0]))
        if not repeated_notable:  # routine series endpoints stay verbatim for context (notable head/tail already are)
            for endpoint in (members[0], members[-1]):
                verbatim[endpoint.event_id] = endpoint
        inner = [event for event in members if event.event_id not in verbatim]
        if not inner:
            continue
        sample = inner[0]
        label: dict[str, object] = {}
        if repeated_notable and key[-2] == "signal":
            label = {"signal": key[-1], "repeated_signal": True}
        elif repeated_notable:
            label = {"severity": key[-2], "message": key[-1], "repeated_notable": True}
        entry: dict[str, object] = {
            "record_kind": key[0], "layer": key[1], **({"metric_name": key[2]} if key[2] else {}),
            **label,
            **{name: getattr(sample, name) for name in IDENTIFIERS if getattr(sample, name)},
            "count": len(inner),
            # Specialist-cited events inside a series keep their ID, indexed into offsets_s/values, so they stay citeable.
            **({"cited_event_ids": {event.event_id: index for index, event in enumerate(inner) if event.event_id in cited}}
               if any(event.event_id in cited for event in inner) else {}),
            "first": {"timestamp": inner[0].timestamp.isoformat().replace("+00:00", "Z"), "event_id": inner[0].event_id},
            "last": {"timestamp": inner[-1].timestamp.isoformat().replace("+00:00", "Z"), "event_id": inner[-1].event_id},
        }
        # Times are whole-second offsets (milliseconds kept when present) from first.timestamp.
        entry["offsets_s"] = [compact_number((event.timestamp - inner[0].timestamp).total_seconds()) for event in inner]
        names = sorted({name for event in inner for name in measures(event)})
        if names:
            columns = {name: [measures(event).get(name) for event in inner] for name in names}
            # A measure with one value throughout is stated once; every other measure keeps one value per event.
            constant = {name: compact_number(column[0]) for name, column in columns.items() if column[0] is not None and len(set(column)) == 1}
            entry["values"] = {name: [None if value is None else compact_number(value) for value in column] for name, column in columns.items() if name not in constant}
            if constant:
                entry["constant"] = constant
            extremes: dict[str, dict[str, str]] = {}
            for name in names:
                present = [event for event in inner if name in measures(event)]
                if present and name not in constant:
                    extremes[name] = {
                        "min_event_id": min(present, key=lambda event: measures(event)[name]).event_id,
                        "max_event_id": max(present, key=lambda event: measures(event)[name]).event_id,
                    }
            entry["extremes"] = extremes
        else:
            messages: dict[str, int] = {}
            for event in inner:
                messages[event.message] = messages.get(event.message, 0) + 1
            entry["messages"] = dict(sorted(messages.items(), key=lambda item: -item[1])[:5])
            entry["distinct_messages"] = len(messages)
        series.append(entry)
    condensed = sum(int(str(item["count"])) for item in series)
    listed = sorted(verbatim.values(), key=lambda item: (item.timestamp, item.event_id))
    summary: dict[str, object] = {
        "total_events": len(ordered), "verbatim_events": len(listed), "condensed_events": condensed, "series": len(series),
        "note": note,
    }
    return EventSummary(listed, series, summary)
