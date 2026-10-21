"""Persistence adapter for user-scoped notifications."""
import uuid
from core.contracts import ok
from core.notification_contracts import (NOTIFICATION_TABLE, NOTICE_PIN_REQUEST_KIND,
    NOTICE_PIN_RESULT_KIND, PASSWORD_RESET_REQUEST_KIND)
from services.notifications import action_request, event_fields, page_request, payload, read_request


def _insert(conn, recipient_ids, event, stamp):
    values = event_fields(event)
    inserted = 0
    for recipient_id in dict.fromkeys(str(value or '').strip() for value in recipient_ids):
        if not recipient_id:
            continue
        result = conn.execute(
            'INSERT INTO account_notifications(notification_id,recipient_id,actor_id,actor_name,kind,title,summary,entity_type,entity_id,payload_json,created_at,dedupe_key) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(recipient_id,dedupe_key) DO NOTHING',
            ('ntf_'+uuid.uuid4().hex, recipient_id, values['actor_id'], values['actor_name'] or '系统',
             values['kind'], values['title'], values['summary'], values['entity_type'], values['entity_id'],
             values['payload_json'], stamp, values['dedupe_key']))
        inserted += max(0, result.rowcount)
    return inserted


def notify_role(conn, role, event, stamp):
    recipients = [row[0] for row in conn.execute(
        "SELECT user_id FROM operators WHERE role=? AND active=1 AND password_hash IS NOT NULL AND password_hash<>''",
        (role,))]
    return _insert(conn, recipients, event, stamp)


def notify_user(conn, user_id, event, stamp):
    return _insert(conn, [user_id], event, stamp)


def replace_notice_request(conn, notice, actor, stamp):
    conn.execute("UPDATE account_notifications SET resolved_at=?,resolved_action='superseded',read_at=COALESCE(read_at,?) "
                 "WHERE kind=? AND entity_id=? AND resolved_at IS NULL",
                 (stamp, stamp, NOTICE_PIN_REQUEST_KIND, notice['id']))
    event = {'actor_id':actor['id'], 'actor_name':actor['name'], 'kind':NOTICE_PIN_REQUEST_KIND,
        'title':'公告置顶申请', 'summary':actor['name']+' 申请置顶《'+notice['title']+'》',
        'entity_type':'notice', 'entity_id':notice['id'],
        'payload':{'revision':notice['revision'], 'actions':['approve','reject']},
        'dedupe_key':'notice-pin-request:'+notice['id']+':'+str(notice['revision'])}
    return notify_role(conn, 'admin', event, stamp)


def resolve_notice_request(conn, notice, actor, action, stamp, notify_author=True):
    conn.execute("UPDATE account_notifications SET resolved_at=?,resolved_action=?,read_at=COALESCE(read_at,?) "
                 "WHERE kind=? AND entity_id=? AND resolved_at IS NULL",
                 (stamp, action, stamp, NOTICE_PIN_REQUEST_KIND, notice['id']))
    if not notify_author:
        return 0
    approved = action == 'approve'
    event = {'actor_id':actor['id'], 'actor_name':actor['name'], 'kind':NOTICE_PIN_RESULT_KIND,
        'title':'置顶申请已批准' if approved else '置顶申请未通过',
        'summary':'《'+notice['title']+'》'+('已置顶' if approved else '未通过置顶审核'),
        'entity_type':'notice', 'entity_id':notice['id'],
        'payload':{'revision':notice['revision'], 'result':action},
        'dedupe_key':'notice-pin-result:'+notice['id']+':'+str(notice['revision'])}
    return notify_user(conn, notice['author_id'], event, stamp)


def replace_password_reset_request(conn, account, stamp):
    event = {'actor_id':account['user_id'], 'actor_name':account['display_name'],
        'kind':PASSWORD_RESET_REQUEST_KIND, 'title':'密码重置申请',
        'summary':account['display_name']+' 提交了密码重置申请，请在人员账号库核实身份后处理',
        'entity_type':'account_recovery', 'entity_id':account['user_id'], 'payload':{}}
    current = conn.execute(
        "SELECT dedupe_key FROM account_notifications WHERE kind=? AND entity_type='account_recovery' "
        "AND entity_id=? AND resolved_at IS NULL LIMIT 1",
        (PASSWORD_RESET_REQUEST_KIND, account['user_id'])).fetchone()
    event['dedupe_key'] = current['dedupe_key'] if current else (
        'password-reset-request:'+account['user_id']+':'+uuid.uuid4().hex)
    values = event_fields(event)
    refreshed = conn.execute(
        "UPDATE account_notifications SET actor_id=?,actor_name=?,title=?,summary=?,payload_json=?,created_at=?,read_at=NULL "
        "WHERE kind=? AND entity_type='account_recovery' AND entity_id=? AND resolved_at IS NULL",
        (values['actor_id'], values['actor_name'], values['title'], values['summary'], values['payload_json'], stamp,
         PASSWORD_RESET_REQUEST_KIND, account['user_id'])).rowcount
    return refreshed + notify_role(conn, 'admin', event, stamp)


def resolve_password_reset_request(conn, user_id, stamp):
    if not user_id:
        return 0
    return conn.execute(
        "UPDATE account_notifications SET resolved_at=?,resolved_action='resolved',read_at=COALESCE(read_at,?) "
        "WHERE kind=? AND entity_type='account_recovery' AND entity_id=? AND resolved_at IS NULL",
        (stamp, stamp, PASSWORD_RESET_REQUEST_KIND, user_id)).rowcount


def list_notifications(conn, query):
    limit, offset, unread_only = page_request(query)
    user_id = str(query.get('_actor_id') or '')
    where = 'recipient_id=?' + (' AND read_at IS NULL' if unread_only else '')
    total = conn.execute('SELECT COUNT(*) FROM '+NOTIFICATION_TABLE+' WHERE '+where, (user_id,)).fetchone()[0]
    rows = [dict(row) for row in conn.execute(
        'SELECT notification_id,actor_name,kind,title,summary,entity_type,entity_id,payload_json,read_at,resolved_at,resolved_action,created_at '
        'FROM '+NOTIFICATION_TABLE+' WHERE '+where+' ORDER BY created_at DESC,notification_id DESC LIMIT ? OFFSET ?',
        (user_id, limit, offset))]
    for row in rows:
        row['payload'] = payload(row.pop('payload_json'))
        row['unread'] = row['read_at'] is None
        row['actionable'] = row['resolved_at'] is None and bool(row['payload'].get('actions'))
    return ok({'rows':rows, 'total':total, 'limit':limit, 'offset':offset, 'unread_only':unread_only})


def unread_count(conn, query):
    count = conn.execute('SELECT COUNT(*) FROM '+NOTIFICATION_TABLE+' WHERE recipient_id=? AND read_at IS NULL',
                         (str(query.get('_actor_id') or ''),)).fetchone()[0]
    return ok({'count':count})


def mark_read(conn, body, stamp):
    notification_id, all_rows = read_request(body)
    if all_rows:
        changed = conn.execute('UPDATE '+NOTIFICATION_TABLE+' SET read_at=? WHERE recipient_id=? AND read_at IS NULL',
                               (stamp, body['_actor_id'])).rowcount
    else:
        changed = conn.execute('UPDATE '+NOTIFICATION_TABLE+' SET read_at=COALESCE(read_at,?) WHERE notification_id=? AND recipient_id=?',
                               (stamp, notification_id, body['_actor_id'])).rowcount
        if changed != 1:
            raise ValueError('消息不存在或不属于当前账号')
    conn.commit()
    return ok({'changed':changed})


def action_target(conn, body):
    notification_id, action = action_request(body)
    row = conn.execute('SELECT notification_id,kind,entity_type,entity_id,payload_json,resolved_at,resolved_action '
                       'FROM '+NOTIFICATION_TABLE+' WHERE notification_id=? AND recipient_id=?',
                       (notification_id, body['_actor_id'])).fetchone()
    if not row:
        raise ValueError('消息不存在或不属于当前账号')
    target = dict(row)
    target['payload'] = payload(target.pop('payload_json'))
    if target['resolved_at']:
        if target['resolved_action'] == action:
            return target
        raise ValueError('这条申请已经处理')
    if action not in target['payload'].get('actions', []):
        raise PermissionError('这条消息不支持该操作')
    target['action'] = action
    return target
