"""Pure validation for the account notification center."""
import json
from core.notification_contracts import (NOTIFICATION_ACTIONS, NOTIFICATION_PAGE_LIMIT,
    NOTIFICATION_PAGE_SIZE, NOTIFICATION_SERVER_FIELDS, NOTIFICATION_TEXT_LIMIT)


def page_request(query):
    limit = int(query.get('limit') or NOTIFICATION_PAGE_SIZE)
    offset = int(query.get('offset') or 0)
    if not 1 <= limit <= NOTIFICATION_PAGE_LIMIT or offset < 0:
        raise ValueError('消息分页参数无效')
    unread = str(query.get('unread') or '')
    if unread not in ('', '0', '1'):
        raise ValueError('未读筛选参数无效')
    return limit, offset, unread == '1'


def event_fields(event):
    required = ('kind', 'title', 'summary', 'dedupe_key')
    if not isinstance(event, dict) or any(not str(event.get(key) or '').strip() for key in required):
        raise ValueError('消息事件缺少必要字段')
    values = {key: str(event.get(key) or '').strip() for key in
              ('actor_id', 'actor_name', 'kind', 'title', 'summary', 'entity_type', 'entity_id', 'dedupe_key')}
    if any(len(values[key]) > NOTIFICATION_TEXT_LIMIT for key in ('actor_name', 'kind', 'title', 'summary', 'entity_type', 'entity_id', 'dedupe_key')):
        raise ValueError('消息事件字段过长')
    payload_value = event.get('payload') or {}
    if not isinstance(payload_value, dict):
        raise ValueError('消息附加数据必须是对象')
    values['payload_json'] = json.dumps(payload_value, ensure_ascii=False, separators=(',', ':'))
    return values


def read_request(body):
    allowed = {'id', 'all'} | NOTIFICATION_SERVER_FIELDS
    if not isinstance(body, dict) or set(body) - allowed:
        raise ValueError('已读请求包含未知字段')
    notification_id = str(body.get('id') or '').strip()
    all_rows = body.get('all') is True
    if bool(notification_id) == all_rows:
        raise ValueError('请选择一条消息或全部标为已读')
    return notification_id, all_rows


def action_request(body):
    allowed = {'id', 'action'} | NOTIFICATION_SERVER_FIELDS
    if not isinstance(body, dict) or set(body) - allowed:
        raise ValueError('消息操作包含未知字段')
    notification_id = str(body.get('id') or '').strip()
    action = str(body.get('action') or '').strip()
    if not notification_id or action not in NOTIFICATION_ACTIONS:
        raise ValueError('消息操作无效')
    return notification_id, action


def payload(value):
    try:
        result = json.loads(value or '{}')
    except (TypeError, ValueError) as error:
        raise ValueError('消息附加数据损坏') from error
    if not isinstance(result, dict):
        raise ValueError('消息附加数据损坏')
    return result
