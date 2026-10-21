"""Route composition; matching belongs to the pure service."""
from core.master_resolution_contracts import MASTER_RESOLVE_ROUTE
from services.master_resolution import validate_request


def build_master_resolution_routes(connect, get_master):
    def resolve(body):
        group, items = validate_request(body)
        connection = connect()
        try:
            resolver = get_master(connection).name_resolver
            return {"ok": True, "data": [resolver.resolve(item["type"], item["value"], group) for item in items]}
        finally:
            connection.close()
    return {MASTER_RESOLVE_ROUTE: resolve}
