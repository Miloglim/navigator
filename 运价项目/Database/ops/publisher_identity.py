"""Publisher persistence, additive startup migration and audited administrator binding."""
import json
import uuid
from core.publisher_contracts import PUBLISHER_DDL, SOURCE_FIELDS
from services.publisher_identity import source_identity, person_keys, roster_persons


def create_publisher(conn, name, stamp, publisher_id=None):
    identity = publisher_id or 'pub_' + uuid.uuid4().hex
    conn.execute('INSERT INTO quote_publishers(publisher_id,publisher_name,created_at) VALUES(?,?,?) '
                 'ON CONFLICT(publisher_id) DO NOTHING', (identity, name or '待确认发布人', stamp))
    return identity


def ensure_schema(conn, stamp):
    """Called only inside startup migration, never by ordinary read connections."""
    for ddl in PUBLISHER_DDL.values():
        conn.execute(ddl)
    if 'merged_into' not in {r[1] for r in conn.execute('PRAGMA table_info(quote_publishers)')}:
        conn.execute('ALTER TABLE quote_publishers ADD COLUMN merged_into TEXT')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_publisher_sources_publisher_id ON publisher_sources(publisher_id)')
    for table, additions in (('operators', ('publisher_id',)),
                             ('freight_items', ('publisher_id', *SOURCE_FIELDS.values())),
                             ('pending_records', ('publisher_id',))):
        columns = {r[1] for r in conn.execute('PRAGMA table_info('+table+')')}
        for column in additions:
            if column not in columns:
                conn.execute('ALTER TABLE '+table+' ADD COLUMN '+column+' TEXT')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_'+table+'_publisher_id ON '+table+'(publisher_id)')
    # Freeze the old, unique relationship once. A later account rename cannot create a new alias.
    accounts = [dict(r) for r in conn.execute('SELECT user_id,publisher_name,publisher_id FROM operators')]
    names = {}
    for account in accounts:
        names.setdefault(account['publisher_name'], []).append(account)
    for account in accounts:
        if account['publisher_id']:
            continue
        identity = create_publisher(conn, account['publisher_name'], stamp)
        conn.execute('UPDATE operators SET publisher_id=? WHERE user_id=?', (identity, account['user_id']))
        if account['publisher_name'] and len(names[account['publisher_name']]) == 1:
            conn.execute('INSERT INTO publisher_legacy_names(sender_name,publisher_id) VALUES(?,?) '
                         'ON CONFLICT(sender_name) DO NOTHING', (account['publisher_name'], identity))
    # Upgrade only identity metadata. Historical quotes/verdicts are deliberately untouched.
    existing = {}
    for source in conn.execute("SELECT source_key,publisher_id,identity_kind FROM publisher_sources WHERE identity_kind IN ('sender_id','open_id')"):
        value = json.loads(source['source_key'])[-1]
        key = ('sender:' if source['identity_kind']=='sender_id' else 'open:')+value
        existing.setdefault(key,set()).add(source['publisher_id'])
    for key, publishers in existing.items():
        owners = {p for p in publishers if conn.execute('SELECT 1 FROM operators WHERE publisher_id=?',(p,)).fetchone()}
        if len(owners)==1:
            conn.execute('INSERT INTO publisher_dingtalk_ids(identity_key,publisher_id,created_at) VALUES(?,?,?) '
                         'ON CONFLICT(identity_key) DO NOTHING',(key,next(iter(owners)),stamp))


def _register_person(conn, publisher_id, names, stamp):
    if conn.execute('SELECT 1 FROM operators WHERE publisher_id=?',(publisher_id,)).fetchone():
        return False
    name = next(iter(sorted(names)), '') or '钉钉发布人'
    if conn.execute('SELECT 1 FROM operators WHERE name=?',(name,)).fetchone():
        name += ' · '+publisher_id
    user_id = 'usr_'+uuid.uuid4().hex
    conn.execute('INSERT INTO operators(user_id,name,publisher_name,publisher_id,role,active,created_at) VALUES(?,?,?,?,?,0,?)',
                 (user_id,name,next(iter(sorted(names)),name),publisher_id,'operator',stamp))
    from ops.admin_accounts import record_event
    record_event(conn,user_id,'publisher_registered','系统','system',{'publisher_id':publisher_id,'login_enabled':False},stamp)
    return True


def ensure_person(conn, keys, names, stamp, match_existing=False):
    """Persist person-level ID aliases. Only an unambiguous confirmed roster may bootstrap a legacy account."""
    from ops.admin_accounts import _lock
    _lock(conn)
    placeholders=','.join('?' for _ in keys)
    rows=conn.execute('SELECT identity_key,publisher_id FROM publisher_dingtalk_ids WHERE identity_key IN ('+placeholders+')',keys).fetchall()
    if len(rows)==len(keys) and len({r['publisher_id'] for r in rows})==1:
        identity=canonical(conn,rows[0]['publisher_id'])
        if conn.execute('SELECT 1 FROM operators WHERE publisher_id=?',(identity,)).fetchone():
            return identity,False
    found={canonical(conn,r[0]) for r in conn.execute('SELECT publisher_id FROM publisher_dingtalk_ids WHERE identity_key IN ('+placeholders+')',keys)}
    if len(found)>1:
        raise ValueError('同一消息的钉钉身份关联冲突，拒绝合并不同账号')
    identity=next(iter(found),None)
    if not identity and match_existing and names:
        marks=','.join('?' for _ in names)
        candidates=[r[0] for r in conn.execute("SELECT publisher_id FROM operators WHERE role='operator' AND publisher_name IN ("+marks+") AND publisher_id IS NOT NULL",sorted(names))]
        candidates=[p for p in candidates if not conn.execute('SELECT 1 FROM publisher_dingtalk_ids WHERE publisher_id=?',(p,)).fetchone()]
        if len(candidates)==1:
            identity=candidates[0]
    identity=create_publisher(conn,next(iter(sorted(names)),''),stamp,identity)
    for key in keys:
        conn.execute('INSERT INTO publisher_dingtalk_ids(identity_key,publisher_id,created_at) VALUES(?,?,?) '
                     'ON CONFLICT(identity_key) DO NOTHING',(key,identity,stamp))
    created=_register_person(conn,identity,names,stamp)
    return identity,created


def sync_confirmed(conn, members, stamp):
    people=roster_persons(members)
    counts={name:sum(name in p['names'] for p in people) for p in people for name in p['names']}
    created=0
    for person in people:
        unique=bool(person['names']) and all(counts[name]==1 for name in person['names'])
        _,added=ensure_person(conn,sorted(person['keys']),person['names'],stamp,match_existing=unique)
        created+=added
    return created


def remove_unopened_placeholder(conn, identity_keys, actor, actor_id, stamp):
    """Remove only an untouched login placeholder created by publisher confirmation."""
    keys = sorted({str(key).strip() for key in identity_keys if str(key).strip()})
    if not keys:
        return 0
    placeholders = ','.join('?' for _ in keys)
    publishers = {row['publisher_id'] for row in conn.execute(
        'SELECT publisher_id FROM publisher_dingtalk_ids WHERE identity_key IN (' + placeholders + ')', keys)}
    if len(publishers) != 1:
        return 0
    publisher_id = next(iter(publishers))
    account = conn.execute(
        "SELECT user_id,name,active,password_hash FROM operators WHERE publisher_id=? AND role='operator'",
        (publisher_id,)).fetchone()
    return _remove_unopened_placeholder_account(conn, account, actor, actor_id, stamp, publisher_id)


def _remove_unopened_placeholder_account(conn, account, actor, actor_id, stamp, publisher_id):
    if not account or account['active'] or account['password_hash']:
        return 0
    events = {row['action'] for row in conn.execute(
        'SELECT action FROM account_events WHERE user_id=?', (account['user_id'],))}
    if events != {'publisher_registered'}:
        return 0
    if conn.execute('SELECT 1 FROM auth_sessions WHERE operator_name=?', (account['name'],)).fetchone():
        return 0
    from ops.admin_accounts import record_event
    record_event(conn, account['user_id'], 'publisher_excluded_placeholder_removed', actor, actor_id,
                 {'publisher_id': publisher_id, 'login_enabled': False}, stamp)
    removed = conn.execute('DELETE FROM operators WHERE user_id=?', (account['user_id'],)).rowcount
    conn.commit()
    return removed


def remove_unopened_placeholder_by_user_id(conn, user_id, actor, actor_id, stamp):
    """Remove one uniquely resolved automatic placeholder without trusting a display name."""
    if not str(user_id or '').strip():
        return 0
    account = conn.execute(
        "SELECT user_id,name,active,password_hash,publisher_id FROM operators "
        "WHERE user_id=? AND role='operator'", (user_id,)).fetchone()
    return _remove_unopened_placeholder_account(
        conn, account, actor, actor_id, stamp, account['publisher_id'] if account else '')


def remove_excluded_placeholders(conn, members, stamp, actor='系统', actor_id='system'):
    from services.publisher_identity import person_keys
    removed = 0
    for member in members:
        if member.get('decision', 'publisher') == 'excluded':
            removed += remove_unopened_placeholder(conn, person_keys(member), actor, actor_id, stamp)
    return removed


def resolve(conn, record, stamp, legacy=False):
    """Never trust a caller-supplied publisher_id; only source facts choose an identity."""
    key, kind, name, group = source_identity(record)
    keys=person_keys(record)
    if keys:
        # The same DWS account owns its quotes in every group. No per-group claim step.
        identity,_=ensure_person(conn,keys,{name} if name else set(),stamp)
        conn.execute('INSERT INTO publisher_sources(source_key,publisher_id,source_group,sender_name,identity_kind,created_at) '
                     'VALUES(?,?,?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET publisher_id=excluded.publisher_id',
                     (key,identity,group,name,kind,stamp))
        return identity
    lock = ' FOR UPDATE' if getattr(conn, 'engine', '') == 'postgres' else ''
    row = conn.execute('SELECT publisher_id FROM publisher_sources WHERE source_key=?'+lock, (key,)).fetchone()
    if row:
        return row['publisher_id']
    legacy = conn.execute('SELECT publisher_id FROM publisher_legacy_names WHERE sender_name=?', (name,)).fetchone() if legacy and kind == 'name_only' else None
    # Deterministic opaque ID avoids orphan publishers when collectors race on one source.
    identity = legacy['publisher_id'] if legacy else 'pub_' + uuid.uuid5(uuid.NAMESPACE_URL, 'freight-source:'+key).hex
    create_publisher(conn, name, stamp, identity)
    conn.execute('INSERT INTO publisher_sources(source_key,publisher_id,source_group,sender_name,identity_kind,created_at) '
                 'VALUES(?,?,?,?,?,?) ON CONFLICT(source_key) DO NOTHING', (key, identity, group, name, kind, stamp))
    return conn.execute('SELECT publisher_id FROM publisher_sources WHERE source_key=?'+lock, (key,)).fetchone()['publisher_id']


def choices(conn, user_id):
    target = conn.execute('SELECT publisher_id FROM operators WHERE user_id=?', (user_id,)).fetchone()
    if not target:
        raise ValueError('账号不存在')
    sources = [dict(r) for r in conn.execute('SELECT s.*,p.publisher_name FROM publisher_sources s JOIN quote_publishers p ON p.publisher_id=s.publisher_id '
        'WHERE s.publisher_id=? ORDER BY s.source_group,s.sender_name', (target['publisher_id'],))]
    for row in sources:
        row['bound'] = row['publisher_id'] == target['publisher_id']
    identity_keys=[r[0] for r in conn.execute('SELECT identity_key FROM publisher_dingtalk_ids WHERE publisher_id=? ORDER BY identity_key',(target['publisher_id'],))]
    return {'publisher_id': target['publisher_id'], 'sources': sources,'identity_keys':identity_keys,'automatic':True}


def annotate(conn, rows):
    """Derive binding status on reads, without storing duplicate UI state."""
    bound = {r[0] for r in conn.execute("SELECT publisher_id FROM operators WHERE publisher_id IS NOT NULL")}
    for row in rows:
        row['publisher_bound'] = row.get('publisher_id') in bound
    return rows


def canonical(conn, publisher_id):
    """Serialize approved/revised writes with binding so an in-flight row cannot regain the old ID."""
    seen = set()
    lock = ' FOR UPDATE' if getattr(conn,'engine','') == 'postgres' else ''
    while publisher_id not in seen:
        seen.add(publisher_id)
        row = conn.execute('SELECT merged_into FROM quote_publishers WHERE publisher_id=?'+lock,(publisher_id,)).fetchone()
        if not row:
            raise ValueError('报价身份不存在')
        if not row['merged_into']:
            return publisher_id
        publisher_id = row['merged_into']
    raise ValueError('报价身份关联出现循环')


def bind(conn, body, stamp):
    from ops.admin_accounts import _checked, record_event
    from core.contracts import ok
    account, _, _, _ = _checked(conn, body)
    if account['role'] != 'operator':
        raise ValueError('仅航管账号可绑定报价来源')
    old, target = str(body.get('publisher_id') or ''), account['publisher_id']
    if not old or old == target:
        raise ValueError('请选择尚未绑定的报价来源')
    if not conn.execute('SELECT 1 FROM publisher_sources WHERE publisher_id=?', (old,)).fetchone():
        raise ValueError('报价来源不存在，请刷新')
    if conn.execute('SELECT 1 FROM operators WHERE publisher_id=?', (old,)).fetchone():
        raise ValueError('该报价身份已绑定其他账号，不能直接转交历史报价')
    lock = ' FOR UPDATE' if getattr(conn, 'engine', '') == 'postgres' else ''
    conn.execute('SELECT publisher_id FROM quote_publishers WHERE publisher_id IN (?,?) ORDER BY publisher_id'+lock, (old,target)).fetchall()
    conn.execute('UPDATE quote_publishers SET merged_into=? WHERE publisher_id=?',(target,old))
    # Sources and their historical records move atomically; no quote content or verdict is changed.
    counts = {}
    for table in ('publisher_sources','publisher_legacy_names','publisher_dingtalk_ids','freight_items','pending_records'):
        counts[table] = conn.execute('UPDATE '+table+' SET publisher_id=? WHERE publisher_id=?', (target,old)).rowcount
    record_event(conn, account['user_id'], 'publisher_bound', body['_actor'], body['_actor_id'],
                 {'source_publisher_id':old, 'publisher_id':target, 'records':counts}, stamp)
    conn.commit()
    return ok({'user_id':account['user_id'], 'publisher_id':target, 'counts':counts})
