"""Route adapter for the versioned external-Agent intake contract."""
from __future__ import annotations

from typing import Any, Callable, Dict

from core.agent_intake_contracts import (
    AGENT_INTAKE_ROUTE,
    AGENT_INTAKE_SCHEMA_VERSION,
    normalize_agent_intake,
)
from core.contracts import ok


def build_agent_intake_route(submit_pending: Callable[[Dict[str, Any]], Dict[str, Any]]):
    def submit(body: Any) -> Dict[str, Any]:
        request, agent = normalize_agent_intake(body)
        result = submit_pending(request)
        if not result.get("ok"):
            return result
        data = {
            "schema_version": AGENT_INTAKE_SCHEMA_VERSION,
            "kind": request["kind"],
            "submitted_by": agent["name"],
            "staged": result.get("staged", 0),
            "ids": result.get("ids", []),
            "preserved": result.get("preserved", 0),
            "preserved_ids": result.get("preserved_ids", []),
            "covered_ids": result.get("covered_ids", []),
            "entry_ids": result.get("entry_ids", []),
            "inherited": result.get("inherited", []),
            "check_failed": result.get("check_failed", 0),
            "check_passed": result.get("check_passed", 0),
            "validation_failures": result.get("validation_failures", []),
            "coverage": result.get("coverage", {}),
        }
        return ok(data)

    return {AGENT_INTAKE_ROUTE: submit}
