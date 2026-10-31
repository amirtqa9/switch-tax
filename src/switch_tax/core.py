"""Deterministic accounting for agent session switches."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime
from typing import Iterable


class TraceError(ValueError):
    pass


KINDS = {"switch", "handoff", "context_load", "tool_call", "tool_result", "progress", "wait", "annotation"}
CATEGORIES = {"handoff", "orientation", "setup", "validation", "productive", "external_wait", "unknown"}


def _time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise TraceError(f"timestamp needs a timezone: {value}")
        return parsed
    except (AttributeError, ValueError) as exc:
        raise TraceError(f"invalid timestamp: {value}") from exc


def _ms(start: datetime, end: datetime) -> int:
    return round((end - start).total_seconds() * 1000)


def validate(events: Iterable[dict]) -> list[dict]:
    rows = list(events)
    ids = set()
    sessions: dict[str, int] = {}
    session_tasks: dict[str, str] = {}
    switches = set()
    for index, event in enumerate(rows, 1):
        if not isinstance(event, dict) or event.get("schema_version") != 1:
            raise TraceError(f"event {index}: expected schema_version 1")
        for field in ("event_id", "task_id", "session_id", "kind"):
            if not isinstance(event.get(field), str) or not event[field]:
                raise TraceError(f"event {index}: missing {field}")
        if event["kind"] not in KINDS:
            raise TraceError(f"event {index}: unknown kind")
        if event["event_id"] in ids:
            raise TraceError(f"event {index}: duplicate event_id")
        ids.add(event["event_id"])
        session_task = session_tasks.setdefault(event["session_id"], event["task_id"])
        if session_task != event["task_id"]:
            raise TraceError(f"event {index}: session_id belongs to another task")
        seq = event.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= sessions.get(event["session_id"], -1):
            raise TraceError(f"event {index}: seq must increase within session")
        sessions[event["session_id"]] = seq
        category = event.get("category")
        if category is not None and category not in CATEGORIES:
            raise TraceError(f"event {index}: unknown category")
        for field in ("links", "handoff_event_ids"):
            if field in event:
                refs = event[field]
                if (not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs)
                        or len(refs) != len(set(refs))):
                    raise TraceError(f"event {index}: {field} must be an array of unique nonempty event IDs")
        if "usage" in event:
            usage = event["usage"]
            if not isinstance(usage, dict):
                raise TraceError(f"event {index}: usage must be an object")
            for field in ("input_tokens", "output_tokens"):
                value = usage.get(field)
                if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                    raise TraceError(f"event {index}: usage.{field} must be a nonnegative integer or null")
            cost = usage.get("cost_usd")
            if cost is not None and (not isinstance(cost, (int, float)) or isinstance(cost, bool)
                                     or not math.isfinite(cost) or cost < 0):
                raise TraceError(f"event {index}: usage.cost_usd must be a finite nonnegative number or null")
        start, end = _time(event.get("timestamp_start")), _time(event.get("timestamp_end"))
        if start and end and end < start:
            raise TraceError(f"event {index}: negative interval")
        if event["kind"] == "switch":
            for field in ("switch_id", "from_session", "to_session", "at"):
                if not isinstance(event.get(field), str) or not event[field]:
                    raise TraceError(f"event {index}: missing {field}")
            if event["switch_id"] in switches:
                raise TraceError(f"event {index}: duplicate switch_id")
            switches.add(event["switch_id"])
            _time(event["at"])
            if event["from_session"] == event["to_session"]:
                raise TraceError(f"event {index}: switch sessions must differ")
    by_id = {event["event_id"]: event for event in rows}
    positions = {event["event_id"]: index for index, event in enumerate(rows)}
    for event in rows:
        for ref in event.get("links", []) + event.get("handoff_event_ids", []):
            if ref not in by_id:
                raise TraceError(f"event {event['event_id']}: unknown reference {ref}")
            if by_id[ref]["task_id"] != event["task_id"]:
                raise TraceError(f"event {event['event_id']}: reference {ref} belongs to another task")
        if event["kind"] == "switch":
            for field in ("from_session", "to_session"):
                owner = session_tasks.get(event[field])
                if owner is not None and owner != event["task_id"]:
                    raise TraceError(f"switch {event['switch_id']}: {field} belongs to another task")
            for ref in event.get("handoff_event_ids", []):
                target = by_id[ref]
                if target["kind"] != "handoff":
                    raise TraceError(f"switch {event['switch_id']}: handoff reference {ref} is not a handoff event")
                if target["session_id"] != event["from_session"]:
                    raise TraceError(f"switch {event['switch_id']}: handoff is in wrong session")
                if target["task_id"] != event["task_id"] or positions[ref] >= positions[event["event_id"]]:
                    raise TraceError(f"switch {event['switch_id']}: handoff must precede switch in the same task")
    return rows


def _union_ms(events: list[dict]) -> int | None:
    if not events:
        return 0
    intervals = []
    for event in events:
        start, end = _time(event.get("timestamp_start")), _time(event.get("timestamp_end"))
        if start is None or end is None:
            return None
        intervals.append((start, end))
    intervals.sort()
    first_start, first_end = intervals[0]
    total = 0
    for start, end in intervals[1:]:
        if start <= first_end:
            first_end = max(first_end, end)
        else:
            total += _ms(first_start, first_end)
            first_start, first_end = start, end
    return total + _ms(first_start, first_end)


def _usage(events: list[dict], key: str) -> int | float | None:
    if not events:
        return 0
    values = []
    for event in events:
        value = (event.get("usage") or {}).get(key)
        if value is None:
            return None
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise TraceError(f"event {event['event_id']}: invalid usage {key}")
        values.append(value)
    return sum(values)


def _classify(event: dict, before_progress: bool, previous_checks: set[tuple]) -> tuple[str, str]:
    if event.get("category"):
        return event["category"], "explicit"
    kind = event["kind"]
    if kind == "handoff":
        return "handoff", "kind"
    if kind == "wait":
        return "external_wait", "kind"
    if kind == "progress":
        return "productive", "kind"
    if kind == "context_load" and before_progress:
        return "orientation", "kind"
    if kind == "tool_call" and before_progress:
        action = event.get("action")
        if action in {"repo_search", "context_read"}:
            return "orientation", "action"
        if action == "environment_setup":
            return "setup", "action"
        if action == "check":
            fingerprint = event.get("command_fingerprint")
            revision = event.get("source_revision")
            if fingerprint and revision and (fingerprint, revision) in previous_checks:
                return "validation", "same-command-and-revision"
            return "productive", "new-check"
    return "unknown", "insufficient-evidence"


def _metrics(events: list[dict], boundary: datetime, progress: dict | None) -> dict:
    intervals = [event for event in events if event["kind"] != "switch"]
    measured = _union_ms(intervals)
    progress_at = _time(progress.get("timestamp_start")) if progress else None
    elapsed = _ms(boundary, progress_at) if progress_at and progress_at >= boundary else None
    waits = [e for e in intervals if e.get("_category") == "external_wait"]
    wait_ms = _union_ms(waits)
    usage_events = [e for e in intervals if e.get("usage")]
    return {
        "observed_wall_ms": elapsed,
        "active_event_ms": measured,
        "external_wait_ms": wait_ms,
        "net_observed_ms": elapsed - wait_ms if elapsed is not None and wait_ms is not None else None,
        "tool_calls": sum(e["kind"] == "tool_call" for e in intervals),
        "input_tokens": _usage(usage_events, "input_tokens") if usage_events else None,
        "output_tokens": _usage(usage_events, "output_tokens") if usage_events else None,
        "cost_usd": _usage(usage_events, "cost_usd") if usage_events else None,
        "coverage": {"timed_events": sum(bool(e.get("timestamp_start") and e.get("timestamp_end")) for e in intervals),
                     "total_events": len(intervals), "usage_events": len(usage_events)},
    }


def analyze(events: Iterable[dict], labels: dict | None = None) -> dict:
    rows = validate(events)
    labels = labels or {}
    if not isinstance(labels, dict):
        raise TraceError("labels must be an object")
    by_id = {e["event_id"]: e for e in rows}
    switches = [e for e in rows if e["kind"] == "switch"]
    positions = {e["event_id"]: index for index, e in enumerate(rows)}
    claimed = set()
    results = []
    warnings = []
    for switch in switches:
        boundary = _time(switch["at"])
        switch_position = positions[switch["event_id"]]
        previous_checks = set()
        for prior in rows[:switch_position]:
            if prior["kind"] == "tool_call" and prior.get("action") == "check" and prior.get("check_passed") is True:
                if prior["session_id"] == switch["from_session"] and prior["task_id"] == switch["task_id"]:
                    fingerprint, revision = prior.get("command_fingerprint"), prior.get("source_revision")
                    if fingerprint and revision:
                        previous_checks.add((fingerprint, revision))
        next_switch_position = next((positions[e["event_id"]] for e in switches
                                     if positions[e["event_id"]] > switch_position
                                     and e["from_session"] == switch["to_session"]
                                     and e["task_id"] == switch["task_id"]), len(rows))
        destination = [e for e in rows[switch_position + 1:next_switch_position]
                       if e["session_id"] == switch["to_session"] and e["task_id"] == switch["task_id"]
                       and e["kind"] != "switch" and e["event_id"] not in claimed]
        destination.sort(key=lambda e: (e.get("seq", 0), e["event_id"]))
        progress = next((e for e in destination if e["kind"] == "progress" and e.get("verified") is True), None)
        if progress:
            destination = [e for e in destination if e["seq"] <= progress["seq"]]
        handoff = [by_id[ref] for ref in switch.get("handoff_event_ids", []) if ref not in claimed]
        selected = handoff + destination
        classified = []
        for event in selected:
            item = dict(event)
            override = labels.get(event["event_id"])
            if override is not None:
                if not isinstance(override, dict) or override.get("category") not in CATEGORIES or not override.get("reason"):
                    raise TraceError(f"invalid label for {event['event_id']}")
                item["category"] = override["category"]
                category, basis = override["category"], "annotation"
            else:
                category, basis = _classify(item, progress is None or item.get("seq", 0) <= progress["seq"], previous_checks)
            item["_category"] = category
            classified.append(item)
        claimed.update(e["event_id"] for e in selected)
        categories = defaultdict(list)
        for event in classified:
            categories[event["_category"]].append(event)
        metrics = _metrics(classified, boundary, progress)
        result = {
            "switch_id": switch["switch_id"], "from_session": switch["from_session"],
            "to_session": switch["to_session"], "status": "complete" if progress else "incomplete",
            "first_progress_event_id": progress["event_id"] if progress else None,
            "metrics": metrics,
            "categories": {name: {"event_ids": [e["event_id"] for e in categories[name]],
                                  "active_event_ms": _union_ms(categories[name]),
                                  "tool_calls": sum(e["kind"] == "tool_call" for e in categories[name]),
                                  "input_tokens": _usage([e for e in categories[name] if e.get("usage")], "input_tokens") if any(e.get("usage") for e in categories[name]) else None,
                                  "output_tokens": _usage([e for e in categories[name] if e.get("usage")], "output_tokens") if any(e.get("usage") for e in categories[name]) else None,
                                  "cost_usd": _usage([e for e in categories[name] if e.get("usage")], "cost_usd") if any(e.get("usage") for e in categories[name]) else None}
                           for name in sorted(CATEGORIES)},
            "classifications": [{"event_id": e["event_id"], "category": e["_category"],
                                 "basis": ("annotation" if e["event_id"] in labels else
                                           _classify(e, True, previous_checks)[1])}
                                for e in classified],
        }
        if not progress:
            warnings.append(f"switch {switch['switch_id']}: no verified productive event")
        elif metrics["observed_wall_ms"] is None:
            warnings.append(f"switch {switch['switch_id']}: progress timestamp missing or before boundary")
        if metrics["active_event_ms"] is None:
            warnings.append(f"switch {switch['switch_id']}: event timing incomplete")
        results.append(result)
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"schema_version": 1, "input_hash": digest, "switches": results,
            "aggregates": {"switches": len(results), "complete": sum(r["status"] == "complete" for r in results),
                           "tool_calls": sum(r["metrics"]["tool_calls"] for r in results)},
            "warnings": warnings}
