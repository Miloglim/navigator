"""Route-only terminal adapters; caller authentication is handled by the host."""
from core.admin_contracts import ADMIN_ACCOUNTS, ADMIN_ACCOUNT, ADMIN_PROFILE, ADMIN_SECURITY, ADMIN_EVENTS, ADMIN_RECORDS


def build_admin_routes(connect, repository, people, hash_password, now):
    from core.publisher_contracts import PUBLISHER_BIND_ROUTE
    from ops import publisher_identity
    from core.contracts import ok
    def read(operation, query):
        conn = connect()
        try:
            return operation(conn,query)
        finally:
            conn.close()
    def write(operation, body):
        conn = connect()
        try:
            return operation(conn,body)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return ({PUBLISHER_BIND_ROUTE:lambda q:read(lambda c,x:ok(publisher_identity.choices(c,x.get("user_id"))),q),
             ADMIN_ACCOUNTS:lambda q:read(lambda c,x:repository.list_accounts(c,x,people()),q),
             ADMIN_ACCOUNT:lambda q:read(lambda c,x:repository.detail(c,x.get('user_id'),people()),q),
             ADMIN_EVENTS:lambda q:read(repository.events,q),
             ADMIN_RECORDS:lambda q:read(repository.records,q)},
            {PUBLISHER_BIND_ROUTE:lambda b:write(lambda c,x:publisher_identity.bind(c,x,now()),b),
             ADMIN_ACCOUNTS:lambda b:write(lambda c,x:repository.create_account(c,x,now()),b),
             ADMIN_PROFILE:lambda b:write(lambda c,x:repository.save_section(c,x,now()),b),
             ADMIN_SECURITY:lambda b:write(lambda c,x:repository.security(c,x,hash_password,now()),b)})
