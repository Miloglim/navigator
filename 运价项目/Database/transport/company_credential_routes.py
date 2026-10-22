"""Thin local-admin routes for rotating company gateway credentials."""

from core.company_credentials_contracts import (COMPANY_CREDENTIAL_ROUTE, COMPANY_TOKEN_REFRESH_ROUTE,
    COMPANY_TOKEN_REFRESH_NOW_ROUTE, COMPANY_TOKEN_REFRESH_ENABLE_ROUTE, COMPANY_TOKEN_REFRESH_DISABLE_ROUTE,
    COMPANY_TOKEN_REFRESH_CONNECT_ROUTE)
from core.contracts import ok
from services.company_credentials import require_local_admin, validate_credential_update


def build_company_credential_routes(store, refresher=None):
    def status(query):
        require_local_admin(query.get("_role"), query.get("_client_ip"))
        return ok(store.status(query.get("_actor_id")))

    def save(body):
        require_local_admin(body.get("_role"), body.get("_client_ip"))
        credentials = validate_credential_update(body)
        return ok(store.save(credentials, body.get("_actor_id")))

    get_routes = {COMPANY_CREDENTIAL_ROUTE: status}
    post_routes = {COMPANY_CREDENTIAL_ROUTE: save}
    if refresher is not None:
        def refresh_status(query):
            require_local_admin(query.get("_role"), query.get("_client_ip"))
            return ok(refresher.poll_connection(query.get("_actor_id")))

        def configure(body):
            require_local_admin(body.get("_role"), body.get("_client_ip"))
            return ok(refresher.configure(body.get("refresh_token"), body.get("_actor_id")))

        def refresh_now(body):
            require_local_admin(body.get("_role"), body.get("_client_ip"))
            return ok(refresher.refresh(body.get("_actor_id"), force=True))

        def disable(body):
            require_local_admin(body.get("_role"), body.get("_client_ip"))
            return ok(refresher.disable(body.get("_actor_id")))

        def enable(body):
            require_local_admin(body.get("_role"), body.get("_client_ip"))
            return ok(refresher.enable(body.get("_actor_id")))

        def connect(body):
            require_local_admin(body.get("_role"), body.get("_client_ip"))
            return ok(refresher.connect_browser(body.get("_actor_id")))

        get_routes[COMPANY_TOKEN_REFRESH_ROUTE] = refresh_status
        post_routes.update({COMPANY_TOKEN_REFRESH_ROUTE: configure,
                            COMPANY_TOKEN_REFRESH_NOW_ROUTE: refresh_now,
                            COMPANY_TOKEN_REFRESH_ENABLE_ROUTE: enable,
                            COMPANY_TOKEN_REFRESH_DISABLE_ROUTE: disable,
                            COMPANY_TOKEN_REFRESH_CONNECT_ROUTE: connect})
    return get_routes, post_routes
