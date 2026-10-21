"""Prospector transport dispatch; account/storage logic lives in ops."""
from core.contracts import ok
from core.prospector_contracts import AUTHORIZE_ROUTE, TOKEN_ROUTE, LOGOUT_ROUTE
from ops import prospector_auth
from services.prospector_auth import public_user

def post(connection, path, body, account):
    if path == AUTHORIZE_ROUTE: return prospector_auth.authorize(connection, account, body)
    if path == TOKEN_ROUTE: return prospector_auth.token(connection, body)
    if path == LOGOUT_ROUTE: return prospector_auth.logout(connection, account)
    raise ValueError('unknown authorization route')

def me(account):
    return ok({'user':public_user(account)})
