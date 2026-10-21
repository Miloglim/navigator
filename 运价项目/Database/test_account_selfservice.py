"""Account life-cycle regressions; all credentials/data are disposable fixtures."""
import json
import secrets
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
import manualdb
import manual_server as server
from ops import notifications
from ops import account_selfservice
from ops.account_security import LoginLimiter
from transport.notification_routes import build_notification_routes


def verify_selfservice(case, connect):
    original=secrets.token_urlsafe(18); updated=secrets.token_urlsafe(18)
    connection=connect()
    try:
        for table in ('account_notifications','account_recovery_requests','auth_sessions','operators'):
            connection.execute('DELETE FROM '+table)
        connection.commit()
        for name,role in [('fixture-admin','admin'),('fixture-ops','operator')]:
            manualdb.save_operator(connection,{'name':name,'role':role,'password_hash':server.password_hash(original),'groups':'Assigned group' if role=='operator' else ''})
        manualdb.ensure_publisher_people(connection,['reserved-publisher'])
    finally:connection.close()
    notification_get,notification_post=build_notification_routes(
        connect,notifications,lambda target,body: (_ for _ in ()).throw(PermissionError('这条消息不能在此处理')),
        manualdb.now_cn)
    with patch.object(server,'conn',side_effect=connect),patch.object(server,'LOGIN_LIMITER',LoginLimiter()),patch.object(server,'RECOVERY_LIMITER',LoginLimiter()),patch.dict(server.GET_ROUTES,notification_get),patch.dict(server.POST_ROUTES,notification_post):
        http=server.FreightHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=http.serve_forever);thread.start()
        base='http://127.0.0.1:%d'%http.server_port
        def call(path,body=None,cookie='',headers=None):
            request=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,
                headers={'Content-Type':'application/json','Cookie':cookie,**(headers or {})})
            try:response=urllib.request.urlopen(request,timeout=15)
            except urllib.error.HTTPError as error:response=error
            with response:return response.status,json.load(response),response.headers
        def login(name,password=original):
            code,result,headers=call('/api/auth/login',{'name':name,'password':password})
            case.assertEqual(code,200,result)
            return headers['Set-Cookie'].split(';',1)[0]
        def signup(name,**extra):return {'name':name,'password':original,'confirm_password':original,**extra}
        try:
            admin=login('fixture-admin');ops=login('fixture-ops')
            for role in ('operator','admin'):
                case.assertEqual(call('/api/auth/register',signup('forged-'+role,role=role))[0],400)
            case.assertEqual(call('/api/auth/register',signup('reserved-publisher'))[0],400)
            case.assertEqual(call('/api/auth/register',signup('weak',password='123',confirm_password='123'))[0],400)
            case.assertEqual(call('/api/auth/register',signup('mismatch',confirm_password=updated))[0],400)
            case.assertEqual(call('/api/auth/register',signup('outside'),headers={'Origin':'https://other.invalid'})[0],403)
            def concurrent(_):return call('/api/auth/register',signup('new-sales'))[0]
            with ThreadPoolExecutor(max_workers=2) as workers:
                case.assertEqual(sorted(workers.map(concurrent,range(2))),[200,400])
            first=login('new-sales');second=login('new-sales')
            connection=connect()
            try:
                row=connection.execute('SELECT * FROM operators WHERE name=?',('new-sales',)).fetchone()
                case.assertEqual(row['role'],'sales');case.assertEqual(row['active'],1);case.assertTrue(row['user_id'])
                case.assertNotEqual(row['password_hash'],original)
            finally:connection.close()
            for cookie in (first,ops):
                case.assertEqual(call('/api/operator/save',{'name':'claim','role':'operator'},cookie)[0],403)
                case.assertEqual(call('/api/account/recovery-requests',cookie=cookie)[0],403)
            change={'current_password':original,'new_password':updated,'confirm_password':updated}
            case.assertEqual(call('/api/auth/change-password',change)[0],401)
            case.assertEqual(call('/api/auth/change-password',{**change,'current_password':'wrong'},ops)[0],403)
            # Never accept a client account name for changing somebody else's password.
            case.assertEqual(call('/api/auth/change-password',{**change,'name':'fixture-admin'},ops)[0],400)
            status,data,headers=call('/api/auth/change-password',change,first)
            case.assertEqual(status,200,data);case.assertIn('Max-Age=0',headers['Set-Cookie'])
            for cookie in (first,second):case.assertFalse(call('/api/auth/status',cookie=cookie)[1]['authenticated'])
            case.assertEqual(call('/api/auth/login',{'name':'new-sales','password':original})[0],403)
            login('new-sales',updated)
            status,known,_=call('/api/auth/forgot-password',{'name':'fixture-ops'})
            case.assertEqual(status,200);case.assertEqual(call('/api/auth/forgot-password',{'name':'nonexistent'})[1],known)
            case.assertEqual(call('/api/auth/forgot-password',{'name':'fixture-ops'})[0],200)
            case.assertEqual(call('/api/notifications/unread-count',cookie=admin)[1]['data']['count'],1)
            messages=call('/api/notifications',cookie=admin)[1]['data']['rows']
            case.assertEqual({row['kind'] for row in messages},{'password_reset_request'})
            case.assertTrue(all(not row['actionable'] for row in messages))
            case.assertEqual(call('/api/notifications/unread-count',cookie=ops)[1]['data']['count'],0)
            requests=call('/api/account/recovery-requests',cookie=admin)[1]['data']['rows']
            case.assertEqual([row['name'] for row in requests],['fixture-ops'])
            case.assertEqual(call('/api/operator/save',{'name':'fixture-ops','password':updated},admin)[0],200)
            case.assertEqual(call('/api/account/recovery-requests',cookie=admin)[1]['data']['rows'],[])
            case.assertEqual(call('/api/notifications/unread-count',cookie=admin)[1]['data']['count'],0)
            case.assertTrue(all(row['resolved_action']=='resolved' for row in call('/api/notifications',cookie=admin)[1]['data']['rows']))
            case.assertFalse(call('/api/auth/status',cookie=ops)[1]['authenticated'])
            new_ops=login('fixture-ops',updated)
            connection=connect()
            try:
                row=connection.execute('SELECT role,groups FROM operators WHERE name=?',('fixture-ops',)).fetchone()
                case.assertEqual((row['role'],row['groups']),('operator',''))
            finally:connection.close()
            case.assertEqual(call('/api/operator/save',{'name':'admin-created-ops','role':'operator','groups':'New assigned group','password':updated},admin)[0],200)
            case.assertEqual(call('/api/auth/login',{'name':'admin-created-ops','password':updated})[1]['user']['role'],'operator')
        finally:http.shutdown();thread.join(timeout=10);http.server_close()


class SelfServiceTests(unittest.TestCase):
    def test_center_shared_profile_overrides_local_mirror_and_saves_center_first(self):
        with tempfile.TemporaryDirectory() as directory:
            connect=lambda:manualdb.connect(Path(directory)/'profile.db')
            connection=connect()
            try:
                connection.execute("INSERT INTO operators(user_id,name,display_name,role,active,preferences_json,match_mode) VALUES('usr_profile','profile@example.invalid','local','sales',1,'{}','any')")
                connection.commit()
            finally:connection.close()
            session={'user_id':'usr_profile','name':'profile@example.invalid','role':'sales','display_name':'profile@example.invalid'}
            shared={'userId':'center-profile','displayName':'Zayne','avatarId':'av-04','growth':{'level':28}}
            result=account_selfservice.profile(connect,session,'127.0.0.1',shared)['data']
            self.assertEqual(result['display_name'],'Zayne')
            self.assertEqual(result['preferences']['avatar_id'],'av-04')
            self.assertEqual(result['shared_growth'],{'level':28})
            calls=[]
            body={'display_name':'Zayne Updated','preferences':{'routes':[],'groups':[],'ctypes':[],'start_tab':'rates','avatar_id':'av-04'}}
            saved=account_selfservice.save_profile(connect,session,'127.0.0.1',body,
                lambda display,avatar:(calls.append((display,avatar)) or {**shared,'displayName':display,'avatarId':avatar}))['data']
            self.assertEqual(calls,[('Zayne Updated','av-04')])
            self.assertEqual(saved['display_name'],'Zayne Updated')
            self.assertEqual(saved['preferences']['avatar_id'],'av-04')

    def test_sqlite_http_account_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/'fixture.db';verify_selfservice(self,lambda:manualdb.connect(db))

if __name__=='__main__':unittest.main()
