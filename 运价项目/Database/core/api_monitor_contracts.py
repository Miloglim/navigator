"""Contracts for administrator-only API and runtime monitoring."""
from typing import Final


API_EVENTS_ROUTE: Final = "/api/admin/monitor/api-events"
API_SUMMARY_ROUTE: Final = "/api/admin/monitor/api-summary"
API_HEALTH_ROUTE: Final = "/api/admin/monitor/health"
API_EVENT_TABLE: Final = "api_request_events"
API_EVENT_INDEX_COLUMNS: Final = ("created_at", "path", "user_id", "status_code")
API_EVENT_DEFAULT_DAYS: Final = 7
API_EVENT_MAX_DAYS: Final = 90
API_EVENT_DEFAULT_LIMIT: Final = 50
API_EVENT_MAX_LIMIT: Final = 200
API_EVENT_MAX_PATH_LENGTH: Final = 160
API_EVENT_MAX_REQUEST_ID_LENGTH: Final = 96
API_MONITOR_ROUTES: Final = frozenset({API_EVENTS_ROUTE, API_SUMMARY_ROUTE, API_HEALTH_ROUTE})

API_EVENT_DDL: Final = """CREATE TABLE IF NOT EXISTS api_request_events (
    request_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    user_id TEXT,
    role TEXT NOT NULL DEFAULT '',
    client_ip TEXT NOT NULL DEFAULT '',
    CHECK(status_code BETWEEN 100 AND 599),
    CHECK(duration_ms >= 0)
)"""
