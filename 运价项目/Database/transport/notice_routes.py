"""Announcement persistence and HTTP route adapters."""
import json
import uuid
from core.contracts import ACCOUNT_AVATAR_ALL_IDS, ACCOUNT_AVATAR_PREF, ADMIN_ROLES, ok
from core.notice_contracts import (NOTICES_ROUTE, NOTICE_SAVE_ROUTE, NOTICE_DELETE_ROUTE,
    NOTICE_PIN_ROUTE, NOTICE_PAGE_SIZE, NOTICE_PAGE_MAX)
from services.notices import (notice_expiry, notice_form, notice_matches, pin_transition,
                             notice_targets, notice_scope_matches, notice_scope_values)
from services.display_preferences import preferred_values
from core.contracts import DISPLAY_GROUPS_PARAM


def build_notice_routes(connect, now, log, on_pin_request=None, on_pin_result=None):
    def groups(conn):
        return [r[0] for r in conn.execute('SELECT name FROM chat_groups WHERE active=1 ORDER BY name')]

    def routes(conn):
        return [r[0] for r in conn.execute('SELECT name FROM routes WHERE active=1 ORDER BY COALESCE(sort,900),name')]

    def row_for(conn, body):
        row = conn.execute('SELECT * FROM announcements WHERE id=? AND deleted=0', (str(body.get('id') or ''),)).fetchone()
        if not row:
            raise ValueError('公告不存在或已删除')
        if body.get('revision') != row['revision']:
            raise ValueError('公告已被更新，请刷新后重试')
        return dict(row)

    def actor(body):
        return {'id':body['_actor_id'], 'name':body['_actor_display']}

    def update(conn, row, body, fields, after_write=None):
        stamp = now()
        values = dict(fields, updated_at=stamp, revision=row['revision'] + 1)
        result = conn.execute('UPDATE announcements SET ' + ','.join(key+'=?' for key in values)
            + ' WHERE id=? AND revision=? AND deleted=0', (*values.values(), row['id'], row['revision']))
        if result.rowcount != 1:
            raise ValueError('公告已被更新，请刷新后重试')
        next_row = dict(row, **values)
        if after_write:
            after_write(conn, next_row, actor(body), stamp)
        conn.commit()
        log('INFO', '公告操作 %s by %s' % (row['id'], body['_actor']))
        return ok({'id':row['id'], 'revision':values['revision'], **fields})

    def listing(query):
        conn = connect()
        try:
            limit = max(1, min(int(query.get('limit') or NOTICE_PAGE_SIZE), NOTICE_PAGE_MAX))
            offset = max(0, int(query.get('offset') or 0))
            today = now()[:10]
            where, args = ['deleted=0', '(expires_on IS NULL OR expires_on>=?)'], [today]
            display_groups = preferred_values(query, DISPLAY_GROUPS_PARAM)
            selected_groups = notice_scope_values(json.loads(query.get('groups') or '[]'))
            selected_routes = notice_scope_values(json.loads(query.get('routes') or '[]'))
            legacy_group = str(query.get('group') or '').strip()
            if legacy_group:
                selected_groups = [legacy_group]
            if display_groups or selected_groups or selected_routes:
                scope_rows = conn.execute('SELECT id,group_name,target_groups_json,target_routes_json FROM announcements WHERE deleted=0')
                ids = [row['id'] for row in scope_rows
                       if notice_scope_matches(dict(row), selected_groups or display_groups, selected_routes,
                                               include_global=not bool(legacy_group))]
                if ids:
                    where.append('id IN (' + ','.join('?' for _ in ids) + ')')
                    args.extend(ids)
                else:
                    where.append('1=0')
            for key, col in (('tag','tags_json'),):
                value = str(query.get(key) or '').strip()
                if value:
                    if key == 'tag':
                        where.append(col+' LIKE ?')
                        escaped = json.dumps(value, ensure_ascii=False).replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
                        where[-1] += " ESCAPE '\\'"
                        args.append('%'+escaped+'%')
                    else:
                        where.append(col+'=?'); args.append(value)
            if query.get('pending') == '1':
                if query.get('_role') not in ADMIN_ROLES:
                    raise PermissionError('只有管理员可查看置顶审核列表')
                where.append("pin_state='pending'")
            clause = ' AND '.join(where)
            order = " ORDER BY CASE WHEN pin_state='approved' THEN 0 ELSE 1 END, created_at DESC, id DESC"
            query_text = str(query.get('q') or '').strip()
            if query_text:
                candidates = [dict(r) for r in conn.execute('SELECT * FROM announcements WHERE '+clause+order, args)]
                for row in candidates:
                    row['tags'] = json.loads(row.pop('tags_json'))
                candidates = [row for row in candidates if notice_matches(row, query_text)]
                total, rows = len(candidates), candidates[offset:offset+limit]
            else:
                total = conn.execute('SELECT COUNT(*) FROM announcements WHERE '+clause, args).fetchone()[0]
                rows = [dict(r) for r in conn.execute('SELECT * FROM announcements WHERE '+clause+order
                    + ' LIMIT ? OFFSET ?', (*args, limit, offset))]
                for row in rows:
                    row['tags'] = json.loads(row.pop('tags_json'))
            author_ids = sorted({row['author_id'] for row in rows})
            avatars = {}
            if author_ids:
                placeholders = ','.join('?' for _ in author_ids)
                for account in conn.execute('SELECT user_id,preferences_json FROM operators WHERE user_id IN ('+placeholders+')', author_ids):
                    try:
                        avatar_id = json.loads(account['preferences_json'] or '{}').get(ACCOUNT_AVATAR_PREF)
                    except (TypeError, ValueError):
                        avatar_id = None
                    avatars[account['user_id']] = avatar_id if avatar_id in ACCOUNT_AVATAR_ALL_IDS else None
            for row in rows:
                row['avatar_id'] = avatars.get(row['author_id'])
                row['target_groups'], row['target_routes'] = notice_targets(row)
                row.pop('target_groups_json', None)
                row.pop('target_routes_json', None)
            tags = sorted({tag for r in conn.execute('SELECT tags_json FROM announcements WHERE deleted=0 AND (expires_on IS NULL OR expires_on>=?)', (today,)) for tag in json.loads(r[0])})
            return ok({'rows':rows,'total':total,'limit':limit,'offset':offset,'groups':groups(conn),'routes':routes(conn),'tags':tags,
                       'expiry_supported':True})
        finally:
            conn.close()

    def save(body):
        conn = connect()
        try:
            row = row_for(conn, body) if body.get('id') else None
            current_groups, current_routes = notice_targets(row) if row else ([], [])
            form = dict(body)
            if row:
                if row['author_id'] != body['_actor_id'] and body['_role'] not in ADMIN_ROLES:
                    raise PermissionError('只能编辑自己的公告')
                if 'target_groups' not in body and 'group_name' not in body:
                    form['target_groups'] = current_groups
                if 'target_routes' not in body:
                    form['target_routes'] = current_routes
            values = notice_form(form, groups(conn) + current_groups, routes(conn) + current_routes)
            fields = {'title':values['title'],'body_html':values['body_html'],'group_name':values['group_name'],
                      'tags_json':json.dumps(values['tags'], ensure_ascii=False),
                      'target_groups_json':json.dumps(values['target_groups'], ensure_ascii=False),
                      'target_routes_json':json.dumps(values['target_routes'], ensure_ascii=False)}
            if 'expires_on' in body:
                fields['expires_on'] = notice_expiry(body['expires_on'], now()[:10])
            if body.get('id'):
                if row['author_id'] != body['_actor_id'] and body['_role'] not in ADMIN_ROLES:
                    raise PermissionError('只能编辑自己的公告')
                # A changed pinned post must not bypass sales pin approval.
                if row['pin_state'] in ('approved','pending') and body['_role'] == 'sales':
                    fields.update(pin_state='pending',pin_reviewed_by=None,pin_reviewed_at=None)
                after_write = on_pin_request if fields.get('pin_state') == 'pending' else None
                return update(conn,row,body,fields,after_write)
            nid, stamp = 'notice_'+uuid.uuid4().hex, now()
            pin = pin_transition('none','request',body['_role'],True) if body.get('pin') else 'none'
            conn.execute('INSERT INTO announcements(id,title,body_html,group_name,tags_json,target_groups_json,target_routes_json,author_id,author_name,created_at,updated_at,expires_on,pin_state,pin_requested_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (nid, fields['title'],fields['body_html'],fields['group_name'],fields['tags_json'],fields['target_groups_json'],fields['target_routes_json'],body['_actor_id'],body['_actor_display'],stamp,stamp,fields.get('expires_on'),pin,body['_actor'] if body.get('pin') else None))
            if pin == 'pending' and on_pin_request:
                on_pin_request(conn, {'id':nid, 'title':fields['title'], 'author_id':body['_actor_id'],
                    'author_name':body['_actor_display'], 'revision':1, 'pin_state':pin}, actor(body), stamp)
            conn.commit()
            log('INFO', '公告发布 %s by %s' % (nid, body['_actor']))
            return ok({'id':nid,'revision':1,'pin_state':pin})
        finally:
            conn.close()

    def delete(body):
        conn = connect()
        try:
            row = row_for(conn, body)
            if row['author_id'] != body['_actor_id'] and body['_role'] not in ADMIN_ROLES:
                raise PermissionError('只能删除自己的公告')
            return update(conn,row,body,{'deleted':1,'pin_state':'none'})
        finally:
            conn.close()

    def pin(body):
        conn = connect()
        try:
            row = row_for(conn,body)
            state = pin_transition(row['pin_state'],body.get('action'),body['_role'],row['author_id']==body['_actor_id'])
            fields = {'pin_state':state}
            if body.get('action') in ('approve','reject'):
                fields.update(pin_reviewed_by=body['_actor'],pin_reviewed_at=now())
            elif body.get('action') == 'request':
                fields.update(pin_requested_by=body['_actor'],pin_reviewed_by=None,pin_reviewed_at=None)
            action = body.get('action')
            after_write = None
            if action == 'request' and state == 'pending':
                after_write = on_pin_request
            elif action in ('approve','reject','unpin') and on_pin_result:
                after_write = lambda current_conn, notice, current_actor, stamp: on_pin_result(
                    current_conn, notice, current_actor, action, stamp, action in ('approve','reject'))
            return update(conn,row,body,fields,after_write)
        finally:
            conn.close()

    return ({NOTICES_ROUTE:listing}, {NOTICE_SAVE_ROUTE:save, NOTICE_DELETE_ROUTE:delete, NOTICE_PIN_ROUTE:pin})
