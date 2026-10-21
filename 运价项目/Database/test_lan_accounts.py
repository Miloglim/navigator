"""Isolated LAN scope, image, write and HTTPS regressions."""
import json, secrets, tempfile, threading, unittest, urllib.error, urllib.request, ssl, subprocess
from pathlib import Path
from unittest.mock import patch
import manualdb
import manual_server as server
from ops.account_security import LoginLimiter, LoginThrottled
from services.access_policy import record_in_scope
from test_port_routes import seed, quote, LANE_A, LANE_B


def verify_lan_accounts(case, connect):
    password=secrets.token_urlsafe(24);connection=connect()
    try:
        for table in ('auth_sessions','operators','freight_items','pending_records'):
            connection.execute('DELETE FROM '+table)
        connection.commit();seed(connection)
        for name,role in [('admin','admin'),('ops','operator'),('sales','sales')]:
            manualdb.save_operator(connection,{'name':'local-'+name,'role':role,'groups':'Group A' if role!='admin' else '',
                'password_hash':server.password_hash(password)})
        pending={}
        for group,price in [('Group A',111),('Group B',222)]:
            record={**quote(LANE_A),'来源群':group,'发送人':'local-ops' if group=='Group A' else 'other-publisher','消息ID':'pending-'+group,'海运费USD':str(price),'图片名':'pending-'+group[-1]+'.png'}
            pending[group]=manualdb.stage_pending(connection,manualdb.get_master(connection),'rates',[record],auto_suggest=False,preserve_other_sources=True)[0]
        for group,price in [('Group A',333),('Group B',444)]:
            record={**quote(LANE_A),'来源群':group,'发送人':'local-ops' if group=='Group A' else 'other-publisher','消息ID':'released-'+group,'海运费USD':str(price),'图片名':'released-'+group[-1]+'.png'}
            pid=manualdb.stage_pending(connection,manualdb.get_master(connection),'rates',[record],auto_suggest=False,preserve_other_sources=True)[0]
            case.assertTrue(manualdb.approve_pending(connection,manualdb.get_master(connection),pid,'fixture')['ok'])
        # The administrator fixture may supersede offers globally; reset status to
        # verify the restricted operator cannot supersede another group's offer.
        connection.execute("UPDATE freight_items SET status=?", (manualdb.DEFAULT_STATUS['rates'],))
        connection.commit()
        from test_publisher_identity import binding
        publisher=connection.execute('SELECT publisher_id FROM pending_records WHERE id=?',(pending['Group A'],)).fetchone()[0]
        binding(connection,'local-ops',publisher)
    finally:connection.close()
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory);(root/'images').mkdir()
        for name in ('pending-A.png','pending-B.png','released-A.png','released-B.png','unreferenced.png'):
            (root/'images'/name).write_bytes(b'fixture-image')
        with patch.object(server,'conn',side_effect=connect),patch.object(server,'HERE',root),patch.object(server,'LOGIN_LIMITER',LoginLimiter()):
            server._IMG_INDEX.update(key=None,by_stem={},memo={})
            http=server.FreightHTTPServer(('127.0.0.1',0),server.Handler)
            thread=threading.Thread(target=http.serve_forever);thread.start();base='http://127.0.0.1:%d'%http.server_port
            def call(path,payload=None,cookie='',extra=None):
                req=urllib.request.Request(base+path,data=json.dumps(payload).encode() if payload is not None else None,
                    headers={'Content-Type':'application/json','Cookie':cookie,**(extra or {})})
                try:response=urllib.request.urlopen(req,timeout=10)
                except urllib.error.HTTPError as error:response=error
                with response:
                    text=response.read();data=json.loads(text) if response.headers.get_content_type()=='application/json' else text
                    return response.status,data,response.headers
            try:
                cookies={}
                for role in ('admin','ops','sales'):
                    status,data,headers=call('/api/auth/login',{'name':'local-'+role,'password':password})
                    case.assertEqual(status,200,data);cookies[role]=headers['Set-Cookie'].split(';',1)[0]
                for view in ('','&view=source','&view=grouped'):
                    status,data,_=call('/api/pending?status=awaiting'+view+'&viewer=local-admin',cookie=cookies['ops'])
                    case.assertEqual(status,200,data);case.assertEqual(data['total'],1,data)
                    case.assertIn('Group A',json.dumps(data));case.assertNotIn('Group B',json.dumps(data))
                status,stats,_=call('/api/group-stats?viewer=local-admin',cookie=cookies['ops'])
                case.assertEqual(status,200,stats);case.assertEqual([row['group'] for row in stats['groups']],['Group A'])
                status,counts,_=call('/api/queue-stats?viewer=local-admin',cookie=cookies['ops'])
                case.assertEqual(status,200,counts);case.assertEqual(counts['pending_awaiting'],1)
                status,audit,_=call('/api/audit?viewer=local-admin',cookie=cookies['ops'])
                case.assertEqual(status,200,audit);case.assertNotIn('Group B',json.dumps(audit,ensure_ascii=False))
                status,hints,_=call('/api/audit-hints?groups=Group%20A,Group%20B&viewer=local-admin',cookie=cookies['ops'])
                case.assertEqual(status,200,hints);case.assertEqual(hints['hints']['Group B'],{'field_stats':{},'defaults':[]})
                case.assertEqual(call('/api/pending/update',{'id':pending['Group B'],'fields':{'备注':'blocked-sales'}},cookies['sales'])[0],403)
                status,edited,_=call('/api/pending/update',{'id':pending['Group B'],'fields':{'备注':'role-authorized edit'}},cookies['ops'])
                case.assertEqual(status,200);case.assertTrue(edited['ok'])
                status,all_pending,_=call('/api/pending?ownership=all',cookie=cookies['ops'])
                case.assertEqual(all_pending['total'],2)
                for cookie in (cookies['sales'],cookies['ops']):
                    case.assertEqual(call('/images/released-A.png',cookie=cookie)[0],200)
                    case.assertEqual(call('/images/released-B.png',cookie=cookie)[0],200)
                    case.assertEqual(call('/images/unreferenced.png',cookie=cookie)[0],404)
                case.assertEqual(call('/images/pending-A.png',cookie=cookies['sales'])[0],404)
                case.assertEqual(call('/images/pending-A.png',cookie=cookies['ops'])[0],200)
                case.assertEqual(call('/images/pending-B.png',cookie=cookies['ops'])[0],200)
                case.assertEqual(call('/images/pending-B.png',cookie=cookies['admin'])[0],200)
                case.assertEqual(call('/api/pending/reject',{'id':pending['Group A']},cookies['ops'],{'Origin':'https://external.invalid'})[0],403)
                status,data,_=call('/api/pending/approve_all',{'_scope_viewer':'local-admin'},cookies['ops'])
                case.assertEqual(status,200,data);case.assertEqual(data['approved'],1,data)
                connection=connect()
                try:
                    case.assertEqual(connection.execute('SELECT review_status FROM pending_records WHERE id=?',(pending['Group B'],)).fetchone()[0],'awaiting')
                    case.assertEqual(connection.execute('SELECT status FROM freight_items WHERE source_group=?',('Group B',)).fetchone()[0],manualdb.DEFAULT_STATUS['rates'])
                finally:connection.close()
                status,data,_=call('/api/pending',{'kind':'rates','records':[{**quote(LANE_A),'来源群':'Group A','消息ID':'another-own'}]},cookies['ops'])
                case.assertEqual(status,200,data)
                connection=connect()
                try:case.assertIsNotNone(connection.execute('SELECT id FROM pending_records WHERE id=?',(pending['Group B'],)).fetchone())
                finally:connection.close()
                for _ in range(8):case.assertEqual(call('/api/auth/login',{'name':'guess','password':'wrong'})[0],403)
                status,data,headers=call('/api/auth/login',{'name':'guess','password':'wrong'})
                case.assertEqual(status,429);case.assertEqual(data['code'],'login_throttled');case.assertIn('Retry-After',headers)
            finally:
                http.shutdown();thread.join(timeout=10);http.server_close()
                server._IMG_INDEX.update(key=None,by_stem={},memo={});manualdb.invalidate_master()


class LANAccountTests(unittest.TestCase):
    def test_sqlite_scope_images_writes_and_throttle(self):
        with tempfile.TemporaryDirectory() as directory:
            database=Path(directory)/'isolated.db';verify_lan_accounts(self,lambda:manualdb.connect(database))

    def test_any_all_scope(self):
        account={'active':1,'groups':'A','routes':LANE_A,'match_mode':'all'}
        self.assertTrue(record_in_scope(account,{'来源群':'A','航线':LANE_A}))
        self.assertTrue(record_in_scope(account,{'来源群':'A','航线':LANE_B}))
        self.assertTrue(record_in_scope({**account,'match_mode':'any'},{'来源群':'A','航线':LANE_B}))
        self.assertFalse(record_in_scope(None,{}))

    def test_limiter_recovery_and_parallel_cap(self):
        from core.contracts import LOGIN_MAX_INFLIGHT
        stamp=[0];limiter=LoginLimiter(clock=lambda:stamp[0])
        for _ in range(8):
            with limiter.admit('same','local'):pass
        with self.assertRaises(LoginThrottled):
            with limiter.admit('same','local'):pass
        stamp[0]=301
        with limiter.admit('same','local'):pass
        contexts=[limiter.admit('person-'+str(i),'local') for i in range(LOGIN_MAX_INFLIGHT)]
        for context in contexts:context.__enter__()
        try:
            with self.assertRaises(LoginThrottled):
                with limiter.admit('extra','local'):pass
        finally:
            for context in contexts:context.__exit__(None,None,None)

    def test_native_https_secure_cookie(self):
        openssl=Path(r'C:\Program Files\Git\usr\bin\openssl.exe')
        if not openssl.is_file():self.skipTest('existing openssl unavailable')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cert=root/'cert.pem';key=root/'key.pem';database=root/'tls.db'
            subprocess.run([str(openssl),'req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),
                '-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            connection=manualdb.connect(database);password=secrets.token_urlsafe(24)
            manualdb.save_operator(connection,{'name':'tls-user','role':'sales','password_hash':server.password_hash(password)});connection.close()
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2;context.load_cert_chain(cert,key)
            with patch.object(server,'conn',side_effect=lambda:manualdb.connect(database)),patch.object(server,'LOGIN_LIMITER',LoginLimiter()):
                http=server.FreightHTTPServer(('127.0.0.1',0),server.Handler)
                http.socket=context.wrap_socket(http.socket,server_side=True,do_handshake_on_connect=False)
                thread=threading.Thread(target=http.serve_forever);thread.start()
                try:
                    url='https://localhost:%d'%http.server_port
                    req=urllib.request.Request(url+'/api/auth/login',data=json.dumps({'name':'tls-user','password':password}).encode(),headers={'Origin':url,'Content-Type':'application/json'})
                    with urllib.request.urlopen(req,context=ssl.create_default_context(cafile=str(cert)),timeout=10) as response:
                        self.assertEqual(response.status,200);self.assertIn('; Secure',response.headers['Set-Cookie']);self.assertTrue(json.load(response)['ok'])
                finally:http.shutdown();thread.join(timeout=10);http.server_close()

if __name__=='__main__':unittest.main()
