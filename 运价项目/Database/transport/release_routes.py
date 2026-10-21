"""Read-only release-policy transport adapter."""
from __future__ import annotations

from typing import Any, Dict

from core.contracts import RELEASE_DECISION_ROUTE, fail, ok
from core.settings import DeploymentSettings
from services.release_policy import decide_release


def build_release_post_routes(settings: DeploymentSettings) -> Dict[str, Any]:
    """Expose a decision preview without connecting it to a publication write path."""
    def decide(body: Dict[str, Any]) -> Dict[str, Any]:
        facts = body.get("facts")
        if not isinstance(facts, dict):
            return fail("facts must be an object", "invalid_request")
        return ok(decide_release(facts, settings.release_mode))

    return {RELEASE_DECISION_ROUTE: decide}
