"""Redacting adapter for local task-event JSONL exports.

Source rows use ``type``, ``id``, ``task``, ``session``, and ``seq``. Optional
fields are ``start``, ``end``, ``usage``, ``category``, ``links``, and, for
switches, ``switch_id``, ``from``, ``to``, ``at``, and ``handoff_ids``. Tool
calls may have ``action``, ``command`` (or ``command_fingerprint``),
``revision``, and ``check_passed``. Progress may have ``verified``.
Unlisted fields, including prompts, arguments, and tool responses, are dropped.
"""

from __future__ import annotations

import hashlib
from typing import Iterable

from .core import TraceError, validate


KINDS = {"switch": "switch", "handoff": "handoff", "context": "context_load",
         "tool_call": "tool_call", "tool_result": "tool_result", "progress": "progress",
         "wait": "wait", "annotation": "annotation"}
ACTIONS = {"check", "repo_search", "context_read", "environment_setup"}


def _id(namespace: str, value: object, index: int) -> str:
    if not isinstance(value, str) or not value:
        raise TraceError(f"source row {index}: {namespace} must be a nonempty string")
    return hashlib.sha256(f"{namespace}:{value}".encode("utf-8")).hexdigest()


def _refs(value: object, namespace: str, index: int) -> list[str]:
    if not isinstance(value, list):
        raise TraceError(f"source row {index}: {namespace} must be an array")
    return [_id("event", item, index) for item in value]


def import_trace(rows: Iterable[dict]) -> list[dict]:
    """Normalize and validate events without retaining raw text fields."""
    events = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise TraceError(f"source row {index}: expected an object")
        source_type = row.get("type")
        kind = KINDS.get(source_type) if isinstance(source_type, str) else None
        if kind is None:
            raise TraceError(f"source row {index}: unknown type")
        event = {"schema_version": 1, "event_id": _id("event", row.get("id"), index),
                 "task_id": _id("task", row.get("task"), index),
                 "session_id": _id("session", row.get("session"), index),
                 "seq": row.get("seq"), "kind": kind}
        for old, new in (("start", "timestamp_start"), ("end", "timestamp_end"),
                         ("category", "category")):
            if old in row:
                if old == "category" and not isinstance(row[old], str):
                    raise TraceError(f"source row {index}: category must be a string")
                event[new] = row[old]
        if "links" in row:
            event["links"] = _refs(row["links"], "links", index)
        if "usage" in row:
            if not isinstance(row["usage"], dict):
                raise TraceError(f"source row {index}: usage must be an object")
            event["usage"] = {key: row["usage"][key] for key in
                              ("input_tokens", "output_tokens", "cost_usd") if key in row["usage"]}
        if kind == "switch":
            event.update(switch_id=_id("switch", row.get("switch_id"), index),
                         from_session=_id("session", row.get("from"), index),
                         to_session=_id("session", row.get("to"), index), at=row.get("at"))
            if "handoff_ids" in row:
                event["handoff_event_ids"] = _refs(row["handoff_ids"], "handoff_ids", index)
        elif kind == "tool_call":
            if isinstance(row.get("action"), str) and row["action"] in ACTIONS:
                event["action"] = row["action"]
            command = row.get("command", row.get("command_fingerprint"))
            if command is not None:
                event["command_fingerprint"] = _id("command", command, index)
            if "revision" in row:
                event["source_revision"] = _id("revision", row["revision"], index)
            if "check_passed" in row:
                if not isinstance(row["check_passed"], bool):
                    raise TraceError(f"source row {index}: check_passed must be a boolean")
                event["check_passed"] = row["check_passed"]
        elif kind == "progress" and "verified" in row:
            if not isinstance(row["verified"], bool):
                raise TraceError(f"source row {index}: verified must be a boolean")
            event["verified"] = row["verified"]
        events.append(event)
    return validate(events)
