"""Account self-service routes. Authentication and persistence are injected."""
from core.contracts import AUTH_REGISTER_ROUTE, AUTH_FORGOT_ROUTE, AUTH_PASSWORD_ROUTE, ACCOUNT_RECOVERY_ROUTE


def build_account_routes(register, forgot, change, requests):
    return ({ACCOUNT_RECOVERY_ROUTE: lambda query: requests()},
            {AUTH_REGISTER_ROUTE: register, AUTH_FORGOT_ROUTE: forgot, AUTH_PASSWORD_ROUTE: change})


def build_profile_routes(read, save):
    from core.contracts import ACCOUNT_PROFILE_ROUTE
    return ({ACCOUNT_PROFILE_ROUTE: lambda session, ip: read(session, ip)},
            {ACCOUNT_PROFILE_ROUTE: lambda session, ip, body: save(session, ip, body)})
