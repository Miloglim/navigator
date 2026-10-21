"""Administrator-only API monitor route adapters."""
from time import monotonic

from core.api_monitor_contracts import API_EVENTS_ROUTE, API_HEALTH_ROUTE, API_SUMMARY_ROUTE
from ops import api_monitor
from services.api_monitor import health_snapshot


def build_api_monitor_routes(connect, database_probe, server_started):
    def read(operation, query):
        connection = connect()
        try:
            return operation(connection, query)
        finally:
            connection.close()

    def health(_query):
        started = monotonic()
        database = database_probe()
        elapsed_ms = round((monotonic() - started) * 1000)
        return {"ok": True, "data": health_snapshot(
            database, elapsed_ms, monotonic() - server_started())}

    return {
        API_EVENTS_ROUTE: lambda query: read(api_monitor.events, query),
        API_SUMMARY_ROUTE: lambda query: read(api_monitor.summary, query),
        API_HEALTH_ROUTE: health,
    }
