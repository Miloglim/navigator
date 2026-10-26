"""Administrator persistence adapter. Only whitelisted public facts leave this module."""
import json
import time
import uuid
from datetime import date, datetime, timedelta
from core.admin_contracts import ADMIN_PAGE_SIZE, ADMIN_PAGE_LIMIT, ADMIN_SERVER_FIELDS, ADMIN_LOGIN_FAILURE_WINDOW_SECONDS
from core.contracts import ACCOUNT_ROLES, ADMIN_ROLES, ROLE_SUPERADMIN, ok
from services.admin_accounts import profile_fields, signature, login_state, security_request

PUBLIC_COLUMNS = "user_id,name,publisher_id,publisher_name,display_name,role,active,created_at,password_changed_at,CASE WHEN password_hash IS NOT NULL AND password_hash<>'' THEN 1 ELSE 0 END AS has_password"


def record_event(conn, user_id, action, actor_name, actor_id, details, stamp):
    conn.execute('INSERT INTO account_events(event_id,user_id,actor_id,actor_name,action,detail_json,created_at) VALUES(?,?,?,?,?,?,?)',
        ('evt_'+uuid.uuid4().hex,user_id,actor_id or 'system',actor_name or '系统',action,json.dumps(details,ensure_ascii=False),stamp))


def _account(conn, user_id):
    row = conn.execute('SELECT '+PUBLIC_COLUMNS+' FROM operators WHERE user_id=?',(str(user_id or ''),)).fetchone()
    if not row:
        raise ValueError('账号不存在，请刷新名单')
    result = dict(row)
    result['display_name'] = result['display_name'] or result['name']
    result['login_status'] = login_state(result)
    return result


def _facts(conn, user_id):
    account = _account(conn,user_id)
    row = conn.execute('SELECT profile_json,experience_json,updated_at,updated_by FROM account_profiles WHERE user_id=?',(user_id,)).fetchone()
    profile = json.loads(row['profile_json']) if row else {}
    experience = json.loads(row['experience_json']) if row else {}
    security = conn.execute('SELECT must_change_password,revision FROM account_security WHERE user_id=?',(user_id,)).fetchone()
    required = bool(security and security[0])
    account['security_revision'] = security[1] if security else 0
    return account,profile,experience,required


def _lock(conn):
    if getattr(conn,'engine','') == 'postgres':
        conn.execute('LOCK TABLE operators IN SHARE ROW EXCLUSIVE MODE')
    elif not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')


def _checked(conn, body):
    _lock(conn)
    facts = _facts(conn,body.get('user_id'))
    if body.get('expected_signature') != signature(*facts):
        raise ValueError('账号资料已变化，请刷新后再保存')
    return facts


def _page(query):
    return min(ADMIN_PAGE_LIMIT,max(1,int(query.get('limit') or ADMIN_PAGE_SIZE))), max(0,int(query.get('offset') or 0))


def list_accounts(conn, query, people):
    import manualdb
    rows = manualdb.list_operators(conn)
    recent = {r['user_id']:r['stamp'] for r in conn.execute("SELECT user_id,MAX(created_at) AS stamp FROM account_events WHERE action='login_success' GROUP BY user_id")}
    profiles = {r['user_id']:json.loads(r['profile_json']) for r in conn.execute('SELECT user_id,profile_json FROM account_profiles')}
    recovery = {r[0] for r in conn.execute("SELECT name FROM account_recovery_requests WHERE status='requested'")}
    for row in rows:
        row['display_name'] = row.get('display_name') or row['name']
        row['login_status'] = login_state(row)
        row['last_login'] = recent.get(row['user_id'])
        row['confirmed_roster_groups'] = people.get(row.get('publisher_name') or row['name'],[])
        row['profile'] = profiles.get(row['user_id'],{})
        row['recovery_requested'] = row['name'] in recovery
    summary = {'total':len(rows),'enabled':sum(r['login_status']=='enabled' for r in rows),
               'unopened':sum(r['login_status']=='unopened' for r in rows),'disabled':sum(r['login_status']=='disabled' for r in rows),
               'recovery_requested':len(recovery)}
    search = str(query.get('q') or '').strip().casefold()
    role,status = query.get('role'),query.get('status')
    if role and role not in ACCOUNT_ROLES:
        raise ValueError('角色筛选无效')
    if status and status not in ('enabled','unopened','disabled','recovery'):
        raise ValueError('登录状态筛选无效')
    rows = [r for r in rows if (not search or search in (' '.join([r['name'],r.get('publisher_name') or '',r['display_name'],r['profile'].get('employee_no','')])).casefold())
            and (not role or r['role']==role) and (not status or (r['recovery_requested'] if status=='recovery' else r['login_status']==status))]
    order = ('superadmin','admin','operator','sales')
    rows.sort(key=lambda r:(order.index(r['role']),r['name']))
    limit,offset = _page(query)
    return ok({'rows':rows[offset:offset+limit],'total':len(rows),'summary':summary,'limit':limit,'offset':offset})


def events(conn, query):
    limit,offset = _page(query)
    where,args = (' WHERE user_id=?',[query['user_id']]) if query.get('user_id') else ('',[])
    total = conn.execute('SELECT COUNT(*) FROM account_events'+where,args).fetchone()[0]
    rows = [dict(row) for row in conn.execute('SELECT event_id,user_id,actor_id,actor_name,action,detail_json,created_at FROM account_events'+where+' ORDER BY created_at DESC,event_id DESC LIMIT ? OFFSET ?',[*args,limit,offset])]
    for row in rows:
        row['details'] = json.loads(row.pop('detail_json'))
    return ok({'rows':rows,'total':total,'limit':limit,'offset':offset})


def detail(conn, user_id, people):
    import manualdb
    account,profile,experience,required = _facts(conn,user_id)
    name = account['name']
    publisher_name = account.get('publisher_name') or name
    published = conn.execute('SELECT COUNT(*) FROM freight_items WHERE publisher_id=?',(account['publisher_id'],)).fetchone()[0]
    pending = conn.execute("SELECT review_status,COUNT(*) AS n FROM pending_records p WHERE p.publisher_id=? GROUP BY review_status",(account['publisher_id'],)).fetchall()
    route_rows = next((r for r in manualdb.list_operators(conn) if r['user_id']==user_id),{})
    sessions = [dict(r) for r in conn.execute('SELECT client_ip,created_at,expires_at FROM auth_sessions WHERE operator_name=? AND expires_at>? ORDER BY created_at DESC LIMIT ?',
                                             (name,int(time.time()),ADMIN_PAGE_LIMIT))]
    last = conn.execute("SELECT MAX(created_at) FROM account_events WHERE user_id=? AND action='login_success'",(user_id,)).fetchone()[0]
    since = (datetime.fromisoformat(manualdb.now_cn())-timedelta(seconds=ADMIN_LOGIN_FAILURE_WINDOW_SECONDS)).strftime('%Y-%m-%d %H:%M:%S')
    failures = conn.execute("SELECT COUNT(*) FROM account_events WHERE user_id=? AND action='login_failed' AND created_at>=?",(user_id,since)).fetchone()[0]
    state = {r['review_status']:r['n'] for r in pending}
    account.update({'profile':profile,'experience':experience,'must_change_password':required,
                    'signature':signature(account,profile,experience,required),'publisher_groups':route_rows.get('publisher_groups',[]),
                    'routes_list':route_rows.get('routes_list',[]),'last_login':last,'sessions':sessions,'recent_login_failures':failures,
                    'business':{'published':published,'awaiting':state.get('awaiting',0),'approved':state.get('approved',0),'rejected':state.get('rejected',0)},
                    'events':events(conn,{'user_id':user_id})['data'],
                    'last_admin':account['role'] in ADMIN_ROLES and account['login_status']=='enabled' and conn.execute("SELECT COUNT(*) FROM operators WHERE role IN ('superadmin','admin') AND active=1 AND password_hash IS NOT NULL").fetchone()[0]==1,
                    'ownership_basis':'账号资料与公告按稳定账号编号关联；新报价按钉钉人员 ID 自动关联本人报价身份，跨群发价无需重复关联。修改登录账号、密码或昵称不改变归属。'})
    return ok(account)


def records(conn, query):
    account = _account(conn,query.get('user_id'))
    source = query.get('source') or 'published'
    if source not in ('published','awaiting','rejected'):
        raise ValueError('记录分类无效')
    limit,offset = _page(query)
    publisher_name = account.get('publisher_name') or account['name']
    if source == 'published':
        total=conn.execute('SELECT COUNT(*) FROM freight_items WHERE publisher_id=?',(account['publisher_id'],)).fetchone()[0]
        rows=[dict(r) for r in conn.execute('SELECT entry_id,record_type AS kind,pol,pod,carrier,container_type,price_usd,valid_from,valid_to,status,message_time FROM freight_items WHERE publisher_id=? ORDER BY id DESC LIMIT ? OFFSET ?',
                                           (account['publisher_id'],limit,offset))]
    else:
        clause="p.publisher_id=? AND review_status=?"
        total=conn.execute('SELECT COUNT(*) FROM pending_records p WHERE '+clause,(account['publisher_id'],source)).fetchone()[0]
        rows=[]
        for r in conn.execute('SELECT entry_id,kind,payload,check_status FROM pending_records p WHERE '+clause+' ORDER BY id DESC LIMIT ? OFFSET ?', (account['publisher_id'],source,limit,offset)):
            payload=json.loads(r['payload'])
            rows.append({'entry_id':r['entry_id'],'kind':r['kind'],'pol':payload.get('起运港'),'pod':payload.get('目的港'),
                'carrier':payload.get('船司'),'container_type':payload.get('柜型'),
                'price_usd':payload.get('海运费USD',payload.get('价格USD')),'valid_from':payload.get('有效期起'),
                'valid_to':payload.get('有效期止'),'status':r['check_status'],'message_time':payload.get('消息时间')})
    return ok({'rows':rows,'total':total,'source':source,'limit':limit,'offset':offset})


def save_section(conn, body, stamp):
    if set(body)-({'user_id','section','fields','expected_signature'} | ADMIN_SERVER_FIELDS):
        raise ValueError('资料请求包含非当前区块字段')
    account,profile,experience,required = _checked(conn,body)
    if account['role'] == ROLE_SUPERADMIN and body.get('_role') != ROLE_SUPERADMIN:
        raise PermissionError('仅超级管理员可管理超级管理员账号')
    fields = profile_fields(body.get('section'),body.get('fields'),date.fromisoformat(stamp[:10]))
    before = dict(profile if body['section']=='profile' else experience)
    if 'display_name' in fields:
        before['display_name'] = account['display_name']
        conn.execute('UPDATE operators SET display_name=? WHERE user_id=?',(fields['display_name'],account['user_id']))
    target = profile if body['section']=='profile' else experience
    target.update({k:v for k,v in fields.items() if k!='display_name'})
    conn.execute('INSERT INTO account_profiles(user_id,profile_json,experience_json,updated_at,updated_by) VALUES(?,?,?,?,?) '
                 'ON CONFLICT(user_id) DO UPDATE SET profile_json=excluded.profile_json,experience_json=excluded.experience_json,updated_at=excluded.updated_at,updated_by=excluded.updated_by',
                 (account['user_id'],json.dumps(profile,ensure_ascii=False),json.dumps(experience,ensure_ascii=False),stamp,body['_actor']))
    changes = {key:{'before':before.get(key,''),'after':value} for key,value in fields.items() if before.get(key,'')!=value}
    if changes:
        record_event(conn,account['user_id'],'profile_updated' if body['section']=='profile' else 'experience_updated',body['_actor'],body['_actor_id'],changes,stamp)
    conn.commit()
    return ok({'user_id':account['user_id'],'changed':list(changes)})


def create_account(conn, body, stamp):
    import manualdb
    from services.account_forms import account_name
    if set(body)-({'name','display_name','role'} | ADMIN_SERVER_FIELDS):
        raise ValueError('新账号仅填写登录名、显示名和角色')
    name = account_name(body.get('name'))
    role = body.get('role') or 'sales'
    allowed_roles = ACCOUNT_ROLES if body.get('_role') == ROLE_SUPERADMIN else ACCOUNT_ROLES - {ROLE_SUPERADMIN}
    if role not in allowed_roles:
        raise ValueError('角色无效')
    fields = profile_fields('profile',{'display_name':body.get('display_name') or name},date.fromisoformat(stamp[:10]))
    _lock(conn)
    if conn.execute('SELECT name FROM operators WHERE name=? OR publisher_name=?',(name,name)).fetchone():
        raise ValueError('登录账号或报价身份已存在，请直接维护现有账号')
    result = manualdb.save_operator(conn,{'name':name,'role':role,'active':0,'_actor':body['_actor'],'_actor_id':body['_actor_id']},commit=False)
    conn.execute('UPDATE operators SET display_name=? WHERE user_id=?',(fields['display_name'],result['user_id']))
    conn.commit()
    return ok({'user_id':result['user_id']})


def security(conn, body, hash_password, stamp):
    import manualdb
    from ops.account_selfservice import resolve_requests
    action = security_request(body)
    account,profile,experience,required = _checked(conn,body)
    name,user_id = account['name'],account['user_id']
    if account['role'] == ROLE_SUPERADMIN and body.get('_role') != ROLE_SUPERADMIN:
        raise PermissionError('仅超级管理员可管理超级管理员账号')
    changes = {'name':name,'_actor':body['_actor'],'_actor_id':body['_actor_id']}
    if action == 'disable':changes['active']=0
    if action in ('activate','enable'):
        if action=='enable' and not account['has_password']:
            raise ValueError('账号尚未设置密码，请使用开通登录')
        changes['active']=1
    if action == 'role':
        if body['role'] == account['role']:
            raise ValueError('角色没有变化，无需保存')
        changes['role']=body['role']
    if action == 'rename_login':
        from services.account_forms import account_name
        new_name = account_name(body.get('new_name'))
        if new_name == name:
            raise ValueError('登录账号没有变化，无需保存')
        if conn.execute('SELECT 1 FROM operators WHERE (name=? OR publisher_name=?) AND user_id<>?',(new_name,new_name,user_id)).fetchone():
            raise ValueError('登录账号或报价身份已存在，请换一个账号')
        conn.execute('UPDATE operators SET name=? WHERE user_id=?',(new_name,user_id))
        conn.execute('DELETE FROM account_recovery_requests WHERE name IN (?,?)',(name,new_name))
    if action in ('activate','reset_password'):
        changes['password_hash']=hash_password(body['new_password'])
        required=True
        resolve_requests(conn,name,body['_actor'],stamp)
    if action == 'require_password_change':
        if not account['has_password']:raise ValueError('请先开通登录并设置密码')
        required=True
    if len(changes)>len(('name','_actor','_actor_id')):
        manualdb.save_operator(conn,changes,commit=False)
    conn.execute('INSERT INTO account_security(user_id,must_change_password,revision) VALUES(?,?,1) ON CONFLICT(user_id) DO UPDATE SET must_change_password=excluded.must_change_password,revision=account_security.revision+1',
                 (user_id,int(required)))
    revoked = conn.execute('DELETE FROM auth_sessions WHERE operator_name=?',(name,)).rowcount
    details = {'revoked_sessions':revoked,'must_change_password':required}
    if action == 'rename_login':
        details['name'] = {'before':name,'after':new_name}
    record_event(conn,user_id,'security_'+action,body['_actor'],body['_actor_id'],details,stamp)
    conn.commit()
    return ok({'user_id':user_id,'revoked_sessions':revoked,'reauth_required':user_id==body.get('_actor_id')})
