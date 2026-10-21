"""Versioned external-Agent intake contract. This module performs no IO."""
from __future__ import annotations

from typing import Any, Dict, TypedDict

AGENT_INTAKE_ROUTE = "/api/agent/intake/v1"
AGENT_INTAKE_SCHEMA_VERSION = "1.0"
AGENT_INTAKE_KINDS = ("rates", "space", "group_suggestion")
AGENT_INTAKE_MAX_RECORDS = 1000
AGENT_INTAKE_AGENT_NAME_MAX = 128
AGENT_INTAKE_AGENT_VERSION_MAX = 64
AGENT_INTAKE_REQUIRED_RECORD_FIELDS = ("来源群", "消息ID", "消息原文")
AGENT_INTAKE_ALLOWED_KEYS = frozenset({
    "schema_version", "kind", "agent", "records",
    "preserve_other_sources", "preserve_existing_source_rows",
})


class AgentIntakeIdentity(TypedDict):
    name: str
    version: str


class AgentIntakeResult(TypedDict):
    schema_version: str
    kind: str
    submitted_by: str
    staged: int
    ids: list[int]
    preserved: int
    preserved_ids: list[int]
    covered_ids: list[int]
    entry_ids: list[str]
    inherited: list[Dict[str, Any]]
    check_failed: int
    check_passed: int
    validation_failures: list[Dict[str, Any]]
    coverage: Dict[str, Any]


def normalize_agent_intake(body: Any) -> tuple[Dict[str, Any], AgentIntakeIdentity]:
    """Validate the public envelope and produce the existing pending request shape."""
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    unknown = sorted(set(body) - AGENT_INTAKE_ALLOWED_KEYS)
    if unknown:
        raise ValueError("unsupported fields: " + ", ".join(unknown))
    if body.get("schema_version") != AGENT_INTAKE_SCHEMA_VERSION:
        raise ValueError("schema_version must be 1.0")
    kind = body.get("kind")
    if kind not in AGENT_INTAKE_KINDS:
        raise ValueError("kind must be rates, space or group_suggestion")
    records = body.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("records must be a non-empty array")
    if len(records) > AGENT_INTAKE_MAX_RECORDS:
        raise ValueError(f"records must contain at most {AGENT_INTAKE_MAX_RECORDS} items")

    clean: list[Dict[str, Any]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"records[{index}] must be an object")
        missing = [field for field in AGENT_INTAKE_REQUIRED_RECORD_FIELDS
                   if not isinstance(record.get(field), str) or not record[field].strip()]
        if missing:
            raise ValueError(f"records[{index}] missing " + ", ".join(missing))
        clean.append(dict(record))

    agent = body.get("agent")
    if not isinstance(agent, dict):
        raise ValueError("agent object is required")
    submitted_by = str(agent.get("name") or "").strip()
    version = str(agent.get("version") or "").strip()
    if not submitted_by or len(submitted_by) > AGENT_INTAKE_AGENT_NAME_MAX:
        raise ValueError("agent.name must be 1-128 characters")
    if len(version) > AGENT_INTAKE_AGENT_VERSION_MAX:
        raise ValueError("agent.version is too long")

    preserve_other = body.get("preserve_other_sources", True)
    preserve_existing = body.get("preserve_existing_source_rows", False)
    if not isinstance(preserve_other, bool) or not isinstance(preserve_existing, bool):
        raise ValueError("preserve flags must be booleans")
    request = {
        "kind": kind,
        "origin": "agent",
        "records": clean,
        "preserve_other_sources": preserve_other,
        "preserve_existing_source_rows": preserve_existing,
    }
    return request, {"name": submitted_by, "version": version}
