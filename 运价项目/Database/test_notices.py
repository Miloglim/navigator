"""Announcement regressions. HTTP/UI fixtures never use production business data."""
import base64
import json
import secrets
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

import manualdb
import manual_server as server
from core.notice_contracts import NOTICE_DDL
from services.notices import notice_expiry, notice_form, pin_transition
from ops import notifications
from transport.notification_routes import build_notification_routes
from core.notice_contracts import NOTICE_PIN_ROUTE
from transport.notice_routes import build_notice_routes

PNG = 'data:image/png;base64,' + base64.b64encode(bytes.fromhex(
    '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489'
    '0000000b49444154789c636000020000050001a5f645400000000049454e44ae426082')).decode()
GROUP = 'Fixture group'
GROUPS = (GROUP, GROUP+' 2', 'Fixture LATAM chat', 'Fixture Europe chat', 'Fixture Africa chat')
ROUTES = ('Fixture Europe', 'Fixture Americas', 'Fixture Africa', 'Fixture Intra-Asia')


@contextmanager
def fixture_http(connect, clock=manualdb.now_cn):
    connection = connect()
    password = secrets.token_urlsafe(18)
    try:
        connection.execute(NOTICE_DDL)
        connection.execute('DELETE FROM announcements')
        for role in ('admin', 'operator', 'sales'):
            manualdb.save_operator(connection, {'name':'notice-'+role, 'role':role,
                'password_hash':server.password_hash(password)})
        manualdb.save_operator(connection, {'name':'notice-other', 'role':'sales',
            'password_hash':server.password_hash(password)})
        for group in GROUPS:
            connection.execute('INSERT INTO chat_groups(name,active) VALUES(?,1) ON CONFLICT(name) DO NOTHING', (group,))
        for lane in ROUTES:
            connection.execute('INSERT INTO routes(name,active) VALUES(?,1) ON CONFLICT(name) DO NOTHING', (lane,))
        connection.commit()
    finally:
        connection.close()
    get, post = build_notice_routes(connect, clock, server.log, notifications.replace_notice_request,
                                    notifications.resolve_notice_request)
    def notification_action(target, body):
        return post[NOTICE_PIN_ROUTE]({'id':target['entity_id'], 'revision':target['payload']['revision'],
            'action':target['action'], '_actor':body['_actor'], '_actor_id':body['_actor_id'],
            '_actor_display':body['_actor_display'], '_role':body['_role']})
    notification_get, notification_post = build_notification_routes(connect, notifications, notification_action, clock)
    with patch.object(server, 'conn', side_effect=connect), patch.dict(server.GET_ROUTES, {**get,**notification_get}), patch.dict(server.POST_ROUTES, {**post,**notification_post}):
        http = server.FreightHTTPServer(('127.0.0.1',0), server.Handler)
        thread = threading.Thread(target=http.serve_forever)
        thread.start()
        base = 'http://127.0.0.1:%d' % http.server_port
        def call(path, body=None, cookie='', headers=None):
            request = urllib.request.Request(base+path, data=json.dumps(body).encode() if body is not None else None,
                headers={'Content-Type':'application/json', 'Cookie':cookie, **(headers or {})})
            try:
                response = urllib.request.urlopen(request, timeout=15)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                return response.status, json.load(response), response.headers
        def login(role):
            code, result, headers = call('/api/auth/login', {'name':'notice-'+role, 'password':password})
            if code != 200:
                raise AssertionError(result)
            return headers['Set-Cookie'].split(';',1)[0]
        try:
            yield base, call, login
        finally:
            http.shutdown(); thread.join(timeout=10); http.server_close()


def verify_notice_scopes(case, connect):
    with fixture_http(connect) as (_, call, login):
        cookie = login('sales')
        baseline = {'title':'Scoped announcement', 'body_html':'<p>Scope test</p>',
                    'target_groups':[GROUP, GROUP+' 2'], 'target_routes':['Fixture Europe']}
        code, reply, _ = call('/api/notices/save', baseline, cookie)
        case.assertEqual(code, 200, reply)
        nid = reply['data']['id']
        for query in ({'groups':[GROUP+' 2']}, {'routes':['Fixture Europe']},
                      {'groups':['missing'], 'routes':['Fixture Europe']}):
            url = '/api/notices?' + urllib.parse.urlencode({key:json.dumps(value) for key,value in query.items()})
            code, reply, _ = call(url, cookie=cookie)
            case.assertEqual(code, 200, reply)
            case.assertEqual(reply['data']['total'], 1)
            row = reply['data']['rows'][0]
            case.assertEqual(row['target_groups'], baseline['target_groups'])
            case.assertEqual(row['target_routes'], baseline['target_routes'])
        code, reply, _ = call('/api/notices?routes='+urllib.parse.quote(json.dumps(['Fixture Americas'])), cookie=cookie)
        case.assertEqual(reply['data']['total'], 0)
        for values in ({'target_groups':['missing']}, {'target_routes':['missing']},
                       {'target_groups':'not an array'}, {'target_routes':[123]}):
            case.assertEqual(call('/api/notices/save', {**baseline, **values}, cookie)[0], 400)
        # Legacy editing must preserve multi-target ranges it does not understand.
        code, reply, _ = call('/api/notices/save', {'id':nid, 'revision':1,
                              'title':'Edited', 'body_html':'<p>New</p>'}, cookie)
        case.assertEqual(code, 200, reply)
        row = call('/api/notices', cookie=cookie)[1]['data']['rows'][0]
        case.assertEqual(row['target_groups'], baseline['target_groups'])
        case.assertEqual(row['target_routes'], baseline['target_routes'])
        # Clearing both lists explicitly makes a global notice, visible in any filter.
        case.assertEqual(call('/api/notices/save', {**baseline, 'id':nid, 'revision':2,
                         'target_groups':[], 'target_routes':[]}, cookie)[0], 200)
        reply = call('/api/notices?routes='+urllib.parse.quote(json.dumps(['Fixture Americas'])), cookie=cookie)[1]
        case.assertEqual(reply['data']['total'], 1)


def verify_notices(case, connect):
    with fixture_http(connect) as (_, call, login):
        admin, sales, other, operator = [login(role) for role in ('admin','sales','other','operator')]
        draft = {'title':'Important sailing notice', 'body_html':'<p><b>Changed</b> today</p><img src="'+PNG+'">',
            'group_name':GROUP, 'tags':['sailing','important'], 'pin':True,
            '_role':'admin', '_actor_id':'forged', '_actor_display':'forged'}
        case.assertEqual(call('/api/notices')[0],401)
        case.assertEqual(call('/api/notifications')[0],401)
        case.assertEqual(call('/api/notices/save',draft)[0],401)
        with patch.dict(server.CFG, {'token':'fixture-agent-only'}):
            case.assertEqual(call('/api/notices/save',draft,headers={'x-push-token':'fixture-agent-only'})[0],403)
        case.assertEqual(call('/api/notices/save',draft,sales,{'Origin':'https://outside.invalid'})[0],403)
        code, reply, _ = call('/api/notices/save',draft,sales)
        case.assertEqual(code,200,reply)
        nid = reply['data']['id']
        case.assertEqual(reply['data']['pin_state'],'pending')
        case.assertEqual(call('/api/notifications/unread-count',cookie=admin)[1]['data']['count'],1)
        case.assertEqual(call('/api/notifications/unread-count',cookie=sales)[1]['data']['count'],0)
        def listing(cookie=admin, query=''):
            code, data, _ = call('/api/notices'+query,cookie=cookie)
            case.assertEqual(code,200,data)
            return data['data']
        row = listing()['rows'][0]
        case.assertEqual(row['author_name'],'notice-sales')
        case.assertNotEqual(row['author_id'],'forged')
        case.assertIsNone(row['avatar_id'])
        case.assertEqual(row['tags'],draft['tags'])
        connection = connect()
        try:
            connection.execute("UPDATE operators SET preferences_json=? WHERE user_id=?", (json.dumps({'avatar_id':'av-17'}), row['author_id']))
            connection.commit()
        finally:
            connection.close()
        case.assertEqual(listing()['rows'][0]['avatar_id'],'av-17')
        connection = connect()
        try:
            connection.execute("UPDATE operators SET preferences_json=? WHERE user_id=?", (json.dumps({'avatar_id':'av2-w01-2'}), row['author_id']))
            connection.commit()
        finally:
            connection.close()
        case.assertEqual(listing()['rows'][0]['avatar_id'],'av2-w01-2')
        case.assertEqual(listing(query='?pending=1')['total'],1)
        case.assertEqual(call('/api/notices?pending=1',cookie=sales)[0],403)
        update = {'id':nid, 'revision':1}
        for cookie in (other, operator):
            case.assertEqual(call('/api/notices/save',{**draft, **update},cookie)[0],403)
            case.assertEqual(call('/api/notices/delete',update,cookie)[0],403)
        case.assertEqual(call('/api/notices/pin',{**update,'action':'approve'},sales)[0],403)
        admin_messages=call('/api/notifications',cookie=admin)[1]['data']['rows']
        case.assertEqual(len(admin_messages),1)
        request_id=admin_messages[0]['notification_id']
        case.assertTrue(admin_messages[0]['actionable'])
        case.assertEqual(call('/api/notifications/action',{'id':request_id,'action':'approve'},other)[0],400)
        case.assertEqual(call('/api/notifications/action',{'id':request_id,'action':'approve'},operator)[0],400)
        case.assertEqual(call('/api/notifications/action',{'id':request_id,'action':'approve'},admin)[0],200)
        case.assertEqual(call('/api/notifications/unread-count',cookie=admin)[1]['data']['count'],0)
        case.assertEqual(call('/api/notifications/unread-count',cookie=sales)[1]['data']['count'],1)
        sales_messages=call('/api/notifications?unread=1',cookie=sales)[1]['data']['rows']
        case.assertEqual(sales_messages[0]['kind'],'notice_pin_result')
        case.assertEqual(call('/api/notifications/action',{'id':sales_messages[0]['notification_id'],'action':'approve'},sales)[0],403)
        case.assertEqual(call('/api/notifications/read',{'id':sales_messages[0]['notification_id']},sales)[0],200)
        case.assertEqual(call('/api/notifications/unread-count',cookie=sales)[1]['data']['count'],0)
        case.assertEqual(listing()['rows'][0]['pin_state'],'approved')
        case.assertEqual(call('/api/notices/save',{**draft,**update},sales)[0],400)
        code, result, _ = call('/api/notices/save',{**draft,'id':nid,'revision':2},sales)
        case.assertEqual(code,200,result)
        case.assertEqual(listing()['rows'][0]['pin_state'],'pending')
        case.assertEqual(call('/api/notices/pin',{'id':nid,'revision':3,'action':'reject'},admin)[0],200)
        case.assertEqual(listing()['rows'][0]['pin_state'],'rejected')
        case.assertEqual(call('/api/notices/pin',{'id':nid,'revision':4,'action':'request'},sales)[0],200)
        case.assertEqual(call('/api/notices/pin',{'id':nid,'revision':5,'action':'approve'},admin)[0],200)
        for title in ('Other update','Next update'):
            case.assertEqual(call('/api/notices/save',{**draft,'title':title,'pin':False,'tags':['sale_%']},other)[0],200)
        case.assertEqual(listing(query='?tag=sailing')['total'],1)
        case.assertEqual(listing(query='?tag=sail')['total'],0)
        case.assertEqual(listing(query='?tag=sale_%25')['total'],2)
        case.assertEqual(listing(query='?group=Fixture%20group')['total'],3)
        case.assertEqual(listing(query='?group=Unknown')['total'],0)
        case.assertEqual(listing(query='?q=notice-other')['total'],2)
        case.assertEqual(listing(query='?q=Changed')['total'],3)
        case.assertEqual(listing(query='?q=Fixture%20group')['total'],3)
        case.assertEqual(listing(query='?q=important')['total'],1)
        case.assertEqual(listing(query='?q=%E7%BD%AE%E9%A1%B6')['total'],1)
        page = listing(query='?limit=1')
        case.assertEqual(page['total'],3); case.assertEqual(len(page['rows']),1)
        case.assertEqual(page['rows'][0]['id'],nid)
        case.assertNotEqual(listing(query='?limit=1&offset=1')['rows'][0]['id'],nid)
        case.assertEqual(call('/api/notices?limit=wrong',cookie=admin)[0],400)
        case.assertEqual(call('/api/notices/save',{**draft,'group_name':'Unknown'},sales)[0],400)
        case.assertEqual(call('/api/notices/delete',{'id':nid,'revision':6},sales)[0],200)
        case.assertEqual(listing()['total'],2)
        case.assertEqual(call('/api/notices/delete',{'id':nid,'revision':7},admin)[0],400)


class NoticeTests(unittest.TestCase):
    def test_multi_target_scopes_http(self):
        with tempfile.TemporaryDirectory() as directory:
            verify_notice_scopes(self, lambda:manualdb.connect(Path(directory)/'scopes.db'))

    def test_http_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            verify_notices(self, lambda:manualdb.connect(Path(directory)/'notices.db'))

    def test_ten_notice_board_order_filter_and_pages(self):
        current = ['2026-09-30 18:00:00']
        with tempfile.TemporaryDirectory() as directory:
            connect = lambda:manualdb.connect(Path(directory)/'ten.db')
            with fixture_http(connect, lambda:current[0]) as (_, call, login):
                admin, sales = login('admin'), login('sales')
                for index in range(10):
                    current[0] = f'2026-09-30 18:{index:02d}:00'
                    body = {'title':f'Board sample {index}', 'body_html':f'<p>Content {index}</p>',
                        'group_name':GROUP if index % 2 else '', 'tags':['grouped' if index % 2 else 'general'],
                        'expires_on':'2026-09-30' if index == 0 else None, 'pin':index == 2}
                    code, result, _ = call('/api/notices/save', body, admin if index == 2 else sales)
                    self.assertEqual(code, 200, result)

                def listing(query=''):
                    code, result, _ = call('/api/notices'+query, cookie=admin)
                    self.assertEqual(code, 200, result)
                    return result['data']

                page = listing('?limit=4')
                self.assertEqual(page['total'], 10)
                self.assertEqual([row['title'] for row in page['rows']],
                    ['Board sample 2','Board sample 9','Board sample 8','Board sample 7'])
                self.assertEqual([len(listing(f'?limit=4&offset={offset}')['rows']) for offset in (0,4,8)], [4,4,2])
                self.assertEqual(listing('?group=Fixture%20group')['total'], 5)
                self.assertEqual(listing('?tag=general')['total'], 5)
                current[0] = '2026-10-01 00:00:00'
                self.assertEqual(listing()['total'], 9)
                self.assertEqual(listing('?tag=general')['total'], 4)
                self.assertNotIn('Board sample 0', [row['title'] for row in listing()['rows']])

    def test_expiry_lifecycle(self):
        current = ['2026-09-30 12:00:00']
        with tempfile.TemporaryDirectory() as directory:
            connect = lambda:manualdb.connect(Path(directory)/'notices.db')
            with fixture_http(connect, lambda:current[0]) as (_, call, login):
                sales, admin = login('sales'), login('admin')
                body = {'title':'Timed notice','body_html':'<p>Visible</p>','tags':['timed']}
                for invalid in ('2026-09-29','2026-02-30','2026/10/02','',True,[]):
                    self.assertEqual(call('/api/notices/save',{**body,'expires_on':invalid},sales)[0],400)
                self.assertEqual(call('/api/notices/save',{**body,'title':'Permanent','tags':['permanent']},sales)[0],200)
                code, result, _ = call('/api/notices/save',{**body,'expires_on':'2026-10-02','pin':True},sales)
                self.assertEqual(code,200,result)
                notice_id = result['data']['id']
                def listing(path='/api/notices'):
                    status, response, _ = call(path,cookie=admin)
                    self.assertEqual(status,200,response)
                    return response['data']
                self.assertEqual(listing()['total'],2)
                self.assertEqual(next(row for row in listing()['rows'] if row['id']==notice_id)['expires_on'],'2026-10-02')
                self.assertEqual(listing('/api/notices?pending=1')['total'],1)
                code, result, _ = call('/api/notices/save',{**body,'id':notice_id,'revision':1},sales)
                self.assertEqual(code,200,result)
                self.assertEqual(next(row for row in listing()['rows'] if row['id']==notice_id)['expires_on'],'2026-10-02')
                current[0] = '2026-10-02 23:59:59'
                self.assertEqual(listing()['total'],2)
                current[0] = '2026-10-03 00:00:00'
                self.assertEqual([row['title'] for row in listing()['rows']],['Permanent'])
                self.assertEqual(listing('/api/notices?pending=1')['total'],0)
                self.assertEqual(listing()['tags'],['permanent'])
                code, result, _ = call('/api/notices/save',{**body,'id':notice_id,'revision':2,'expires_on':'2026-10-05'},sales)
                self.assertEqual(code,200,result)
                self.assertEqual(listing()['total'],2)
                code, result, _ = call('/api/notices/save',{**body,'id':notice_id,'revision':3,'expires_on':None},sales)
                self.assertEqual(code,200,result)
                self.assertIsNone(next(row for row in listing()['rows'] if row['id']==notice_id)['expires_on'])
                current[0] = '2026-10-10 12:00:00'
                self.assertEqual(listing()['total'],2)

    def test_legacy_sqlite_notice_column(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'legacy.db'
            with closing(sqlite3.connect(path)) as conn:
                conn.execute(NOTICE_DDL.replace(', expires_on TEXT',''))
                conn.execute("INSERT INTO announcements(id,title,body_html,tags_json,author_id,author_name,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    ('legacy','Old notice','<p>Kept</p>','[]','owner','Owner','2026-09-29','2026-09-29'))
                conn.commit()
            connection = manualdb.connect(path)
            try:
                self.assertIn('expires_on',{row[1] for row in connection.execute('PRAGMA table_info(announcements)')})
                row = connection.execute('SELECT title,expires_on FROM announcements WHERE id=?',('legacy',)).fetchone()
                self.assertEqual((row['title'],row['expires_on']),('Old notice',None))
            finally:
                connection.close()

    def test_preferred_browse_keeps_global_and_explicit_search_reaches_other_group(self):
        with tempfile.TemporaryDirectory() as directory:
            connect=lambda:manualdb.connect(Path(directory)/'notices.db')
            with fixture_http(connect) as (_, call, login):
                cookie=login('sales')
                conn=connect()
                try:
                    conn.execute('INSERT INTO chat_groups(name,active) VALUES(?,1)',('Other fixture group',))
                    conn.commit()
                finally:
                    conn.close()
                for name, group in [('global',''),('preferred',GROUP),('outside','Other fixture group')]:
                    code, result, _=call('/api/notices/save',{'title':name,'body_html':'<p>test</p>','group_name':group,'tags':[]},cookie)
                    self.assertEqual(code,200,result)
                params='?display_mode=preferred&display_groups='+urllib.parse.quote(json.dumps([GROUP]))
                code, result, _=call('/api/notices'+params,cookie=cookie)
                self.assertEqual(code,200,result)
                self.assertEqual({row['title'] for row in result['data']['rows']},{'global','preferred'})
                self.assertEqual(result['data']['total'],2)
                code, result, _=call('/api/notices?group=Other%20fixture%20group',cookie=cookie)
                self.assertEqual(code,200,result)
                self.assertEqual([row['title'] for row in result['data']['rows']],['outside'])
                code, result, _=call('/api/notices?display_mode=preferred&display_groups=%22bad%22',cookie=cookie)
                self.assertEqual(code,400,result)

    def test_rich_text_security_and_format(self):
        raw = '<h2 onclick="bad()">Title</h2><script>alert(1)</script><svg><script>bad</script></svg>'
        raw += '<p style="color:#ff0000;background-image:url(evil);font-size:24px">Text</p>'
        raw += '<a href="javascript:bad()">bad link</a><a href="https://example.com">good link</a>'
        raw += '<font color="#123456" size="5">large</font><img src="'+PNG+'" onerror="bad()">'
        result = notice_form({'title':'Title','body_html':raw,'tags':['one','one',' two ']},[]) 
        html = result['body_html']
        for bad in ('onclick','onerror','javascript','<svg','<script','alert','background-image'):
            self.assertNotIn(bad,html)
        for good in ('<h2>','color:#ff0000','font-size:24px','font-size:24px','rel="noopener noreferrer"',PNG):
            self.assertIn(good,html)
        self.assertEqual(result['tags'],['one','two'])

    def test_validation(self):
        baseline = {'title':'Title','body_html':'<p>Body</p>'}
        for delta in ({'title':''},{'body_html':'<script>bad</script>'},{'pin':'true'},
            {'tags':['x']*9+['new'+str(i) for i in range(9)]},{'tags':{'bad':1}},
            {'body_html':'<img src="https://example.com/tracker.png">'},
            {'body_html':'<img src="data:image/png;base64,YmFk">'},
            {'body_html':('<img src="'+PNG+'">')*9}):
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                notice_form({**baseline, **delta},[])
        self.assertEqual(pin_transition('none','request','sales',True),'pending')
        self.assertEqual(pin_transition('none','request','operator',True),'approved')
        with self.assertRaises(PermissionError): pin_transition('pending','approve','sales',True)
        with self.assertRaises(PermissionError): pin_transition('none','request','sales',False)
        self.assertIsNone(notice_expiry(None,'2026-09-30'))
        self.assertEqual(notice_expiry('2026-09-30','2026-09-30'),'2026-09-30')

    def test_snapshot_compatibility(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = manualdb.connect(Path(directory)/'snapshot.db')
            try:
                get,post = build_notice_routes(lambda:manualdb.connect(Path(directory)/'snapshot.db'), manualdb.now_cn, lambda *args:None)
                post['/api/notices/save']({'title':'Archive','body_html':'<p>Kept</p>',
                    '_actor':'fixture','_actor_id':'fixture','_actor_display':'Fixture','_role':'sales'})
                snapshot = manualdb.export_all(connection)
                self.assertIn('announcements',snapshot)
                self.assertEqual(len(snapshot['announcements']),1)
                snapshot['announcements'][0]['body_html'] += '<script>unsafe()</script>'
                manualdb.load_snapshot(connection,snapshot)
                self.assertNotIn('<script>',connection.execute('SELECT body_html FROM announcements').fetchone()[0])
                snapshot.pop('announcements')
                manualdb.load_snapshot(connection,snapshot)
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM announcements').fetchone()[0],1)
            finally:
                connection.close()


if __name__ == '__main__':
    import sys
    if '--serve' in sys.argv:
        with tempfile.TemporaryDirectory() as directory:
            with fixture_http(lambda:manualdb.connect(Path(directory)/'preview.db')) as (base, call, login):
                print(json.dumps({'url':base, 'cookies':{role:login(role) for role in ('admin','sales','operator')} }),flush=True)
                threading.Event().wait()
    else:
        unittest.main()
