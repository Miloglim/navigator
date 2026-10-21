"""Pure validation and runtime health projection for API monitoring."""
from core.api_monitor_contracts import (
    API_EVENT_DEFAULT_DAYS,
    API_EVENT_DEFAULT_LIMIT,
    API_EVENT_MAX_DAYS,
    API_EVENT_MAX_LIMIT,
    API_EVENT_MAX_PATH_LENGTH,
)


def monitor_query(query):
    try:
        days = int(query.get("days") or API_EVENT_DEFAULT_DAYS)
        limit = int(query.get("limit") or API_EVENT_DEFAULT_LIMIT)
        offset = int(query.get("offset") or 0)
    except (TypeError, ValueError) as error:
        raise ValueError("days/limit/offset 必须是整数") from error
    if not 1 <= days <= API_EVENT_MAX_DAYS:
        raise ValueError(f"days 必须在 1-{API_EVENT_MAX_DAYS} 之间")
    if not 1 <= limit <= API_EVENT_MAX_LIMIT or offset < 0:
        raise ValueError("limit/offset 无效")
    method = str(query.get("method") or "").upper()
    if method and method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ValueError("method 无效")
    path = str(query.get("path") or "").strip()
    if path and (not path.startswith("/api/") or len(path) > API_EVENT_MAX_PATH_LENGTH):
        raise ValueError("path 无效")
    user_id = str(query.get("user_id") or "").strip()
    failures_only = str(query.get("failures") or "") == "1"
    return {"days": days, "limit": limit, "offset": offset,
            "method": method, "path": path, "user_id": user_id,
            "failures_only": failures_only}


def health_snapshot(database, database_probe_ms, uptime_seconds):
    connected = bool(database.get("connected"))
    engine = str(database.get("engine") or "unknown")
    return {
        "status": "ok" if connected else "blocked",
        "service": "online",
        "uptime_seconds": max(0, int(uptime_seconds)),
        "database": {"connected": connected, "engine": engine,
                      "probe_ms": max(0, int(database_probe_ms)),
                      "error_type": str(database.get("error_type") or "")[:80]},
    }
