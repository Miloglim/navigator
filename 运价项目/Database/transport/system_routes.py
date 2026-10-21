"""System-status routes isolated from the legacy dashboard handlers."""
from __future__ import annotations

from typing import Any, Callable, Dict

import manualdb
from core.contracts import API_VERSION, SYSTEM_HEALTH_ROUTE, SYSTEM_READINESS_ROUTE, ok
from core.settings import DeploymentSettings
from services.readiness import assess_readiness


def build_system_get_routes(db_path: Callable[[], str], settings: DeploymentSettings,
                            probe: Callable[[str], Dict[str, Any]] = manualdb.database_probe) -> Dict[str, Callable[[Dict[str, str]], Dict[str, Any]]]:
    """Return route handlers while keeping request parsing in the main transport."""
    def health(_: Dict[str, str]) -> Dict[str, Any]:
        database = probe(db_path())
        return ok({"api_version": API_VERSION, "status": "ok" if database["connected"] else "blocked", "database": database})

    def readiness(_: Dict[str, str]) -> Dict[str, Any]:
        return ok(assess_readiness(probe(db_path()), settings))

    return {SYSTEM_HEALTH_ROUTE: health, SYSTEM_READINESS_ROUTE: readiness}
