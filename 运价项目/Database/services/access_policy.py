"""Pure account capabilities. Database state and HTTP details are supplied by callers."""
from core.contracts import (ROLE_ADMIN, ROLE_OPERATOR, ROLE_SALES, SALES_SCOPED_GET,
    OPS_OWNERSHIP_GET, OWNERSHIP_QUERY_PARAM, OWNERSHIP_MINE, OWNERSHIP_ALL)

ROLE_VIEWS = {
    ROLE_ADMIN: ("sales", "ops", "config", "account", "monitor"),
    ROLE_OPERATOR: ("sales", "ops", "account"),
    ROLE_SALES: ("sales", "account"),
}
SESSION_SECURITY_FIELDS = ("role", "active", "password_hash")


def account_identity(row):
    return {"user_id": row["user_id"], "name": row["name"], "display_name": (dict(row).get("display_name") or row["name"]), "role": row["role"],
            "must_change_password":bool(dict(row).get('must_change_password')),
            "views": list(ROLE_VIEWS.get(row["role"], ()))}


def scoped_sales_query(path, query, session):
    """Shared quote visibility; ownership is a self/all display filter, not permission."""
    if path not in SALES_SCOPED_GET:
        return query
    result = {k:v for k,v in query.items() if k not in ('viewer',OWNERSHIP_QUERY_PARAM)}
    default = OWNERSHIP_MINE if path in OPS_OWNERSHIP_GET else OWNERSHIP_ALL
    choice = query.get(OWNERSHIP_QUERY_PARAM, default)
    if choice not in (OWNERSHIP_MINE, OWNERSHIP_ALL):
        raise ValueError('ownership must be mine or all')
    if session and session['role'] == ROLE_OPERATOR and choice == OWNERSHIP_MINE:
        result['viewer'] = session['name']
    return result


def browser_write_allowed(origin, fetch_site, host, secure=False):
    """Browser cookie writes must come from this origin; CLI requests have no Origin."""
    if fetch_site == "cross-site":
        return False
    if origin:
        return origin == ("https://" if secure else "http://") + host
    return True


def record_in_scope(account, record):
    """Legacy routes/groups no longer impose business visibility or action permissions."""
    return bool(account and account.get('active'))


def session_security_changed(before, after):
    return bool(before and any(before.get(key) != after.get(key) for key in SESSION_SECURITY_FIELDS))

