"""Route-only adapters for user notifications."""
from core.contracts import ok
from core.notification_contracts import (NOTIFICATIONS_ROUTE, NOTIFICATION_COUNT_ROUTE,
    NOTIFICATION_READ_ROUTE, NOTIFICATION_ACTION_ROUTE)


def build_notification_routes(connect, repository, action_handler, now):
    def read(operation, query):
        conn = connect()
        try:
            return operation(conn, query)
        finally:
            conn.close()

    def write(operation, body):
        conn = connect()
        try:
            return operation(conn, body, now())
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def action(body):
        conn = connect()
        try:
            target = repository.action_target(conn, body)
        finally:
            conn.close()
        if target.get('resolved_at'):
            return ok({'id':target['notification_id'], 'action':target['resolved_action'], 'already_resolved':True})
        result = action_handler(target, body)
        data = dict(result.get('data') or {})
        data.update(notification_id=target['notification_id'], action=target['action'])
        return ok(data)

    return ({NOTIFICATIONS_ROUTE:lambda q:read(repository.list_notifications, q),
             NOTIFICATION_COUNT_ROUTE:lambda q:read(repository.unread_count, q)},
            {NOTIFICATION_READ_ROUTE:lambda b:write(repository.mark_read, b),
             NOTIFICATION_ACTION_ROUTE:action})
