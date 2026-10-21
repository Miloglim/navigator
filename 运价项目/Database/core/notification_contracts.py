"""User notification contracts; no runtime dependencies."""
from typing import TypedDict

NOTIFICATIONS_ROUTE = '/api/notifications'
NOTIFICATION_COUNT_ROUTE = '/api/notifications/unread-count'
NOTIFICATION_READ_ROUTE = '/api/notifications/read'
NOTIFICATION_ACTION_ROUTE = '/api/notifications/action'
NOTIFICATION_GET_ROUTES = frozenset({NOTIFICATIONS_ROUTE, NOTIFICATION_COUNT_ROUTE})
NOTIFICATION_POST_ROUTES = frozenset({NOTIFICATION_READ_ROUTE, NOTIFICATION_ACTION_ROUTE})
NOTIFICATION_TABLE = 'account_notifications'
NOTIFICATION_PAGE_SIZE = 20
NOTIFICATION_PAGE_LIMIT = 100
NOTIFICATION_TEXT_LIMIT = 240
NOTIFICATION_ACTIONS = frozenset({'approve', 'reject'})
NOTIFICATION_SERVER_FIELDS = frozenset({'_actor', '_actor_id', '_actor_display', '_role'})
NOTICE_PIN_REQUEST_KIND = 'notice_pin_request'
NOTICE_PIN_RESULT_KIND = 'notice_pin_result'
PASSWORD_RESET_REQUEST_KIND = 'password_reset_request'

NOTIFICATION_DDL = """CREATE TABLE IF NOT EXISTS account_notifications (
    notification_id TEXT PRIMARY KEY, recipient_id TEXT NOT NULL,
    actor_id TEXT, actor_name TEXT NOT NULL, kind TEXT NOT NULL,
    title TEXT NOT NULL, summary TEXT NOT NULL,
    entity_type TEXT, entity_id TEXT, payload_json TEXT NOT NULL DEFAULT '{}',
    read_at TEXT, resolved_at TEXT, resolved_action TEXT,
    created_at TEXT NOT NULL, dedupe_key TEXT NOT NULL,
    UNIQUE(recipient_id, dedupe_key))"""


class NotificationEvent(TypedDict, total=False):
    actor_id: str
    actor_name: str
    kind: str
    title: str
    summary: str
    entity_type: str
    entity_id: str
    payload: dict
    dedupe_key: str


class NotificationAction(TypedDict):
    id: str
    action: str
