"""Pure deployment-readiness assessment for the freight ledger."""
from __future__ import annotations

from typing import Any, Dict, List

from core.contracts import HEALTH_ATTENTION, HEALTH_BLOCKED, HEALTH_OK
from core.settings import DeploymentSettings

SQLITE_ENGINE = "sqlite"
POSTGRES_ENGINE = "postgresql"


def assess_readiness(database: Dict[str, Any], settings: DeploymentSettings) -> Dict[str, Any]:
    """Classify verified facts only; unknown integrations are never guessed as ready."""
    checks: List[Dict[str, str]] = []
    db_connected = bool(database.get("connected"))
    engine = str(database.get("engine") or "unknown")

    checks.append({
        "name": "database_connectivity",
        "status": HEALTH_OK if db_connected else HEALTH_BLOCKED,
        "detail": "database query succeeded" if db_connected else "database query failed",
    })
    checks.append({
        "name": "database_engine",
        "status": HEALTH_OK if engine == POSTGRES_ENGINE else HEALTH_ATTENTION,
        "detail": "PostgreSQL adapter active" if engine == POSTGRES_ENGINE else "SQLite is suitable for pilot use; migrate before high-availability production",
    })
    checks.append({
        "name": "backup_destination",
        "status": HEALTH_OK if settings.backup_target_configured else HEALTH_ATTENTION,
        "detail": "off-host backup destination configured" if settings.backup_target_configured else "only local startup backups are confirmed",
    })
    checks.append({
        "name": "dingtalk_sso",
        "status": HEALTH_OK if settings.dingtalk_sso_enabled else HEALTH_ATTENTION,
        "detail": "DingTalk SSO enabled" if settings.dingtalk_sso_enabled else "local account login remains active",
    })

    status = HEALTH_BLOCKED if any(item["status"] == HEALTH_BLOCKED for item in checks) else (
        HEALTH_ATTENTION if any(item["status"] == HEALTH_ATTENTION for item in checks) else HEALTH_OK
    )
    return {
        "status": status,
        "deployment_mode": settings.deployment_mode,
        "release_mode": settings.release_mode,
        "database_engine": engine,
        "checks": checks,
    }
