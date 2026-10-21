import base64
import hashlib
import json
import unittest
import uuid
from urllib.parse import urlencode, urlparse, parse_qs
from test_navigator_fixture import NavigatorFixture

class ProspectorAuthTests(unittest.TestCase):
    fixture_factory = NavigatorFixture
    def setUp(self):
        self.fx = self.fixture_factory().__enter__()
        self.cookie = self.fx.login()
        self.verifier = 'a' * 43
        self.params = {'response_type':'code','client_id':'prospector','redirect_uri':'http://127.0.0.1:45678/navigator/callback','state':'test-state','code_challenge':base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip('='),'code_challenge_method':'S256'}

    def tearDown(self): self.fx.__exit__()

    def code(self):
        status, result, _ = self.fx.request('/api/prospector/authorize', self.params, self.cookie, origin=self.fx.base_url)
        self.assertEqual(status, 200, result)
        url = result['data']['redirect_url']
        self.assertEqual(parse_qs(urlparse(url).query)['state'], ['test-state'])
        return parse_qs(urlparse(url).query)['code'][0]

    def tokens(self, code=None, verifier=None):
        return self.fx.request('/api/prospector/token', {'grant_type':'authorization_code','code':code or self.code(),'code_verifier':verifier or self.verifier,'client_id':'prospector','redirect_uri':self.params['redirect_uri']})

    def test_pkce_single_use_rotation_logout_and_readonly(self):
        code = self.code()
        self.assertEqual(self.tokens(code, 'b'*43)[0], 400)
        status, result, _ = self.tokens(code)
        self.assertEqual(status, 200, result)
        data = result['data']
        self.assertEqual(data['expires_in'], 900)
        self.assertEqual(data['user']['name'], 'fixture-admin')
        self.assertEqual(self.tokens(code)[0], 400)
        self.assertEqual(self.fx.request('/api/prospector/me', bearer=data['access_token'])[0], 200)
        self.assertEqual(self.fx.request('/api/rates', bearer=data['access_token'])[0], 200)
        for path in ('/api/operators','/api/export','/api/bootstrap'):
            self.assertEqual(self.fx.request(path, cookie=self.cookie, bearer=data['access_token'])[0], 403)
        self.assertEqual(self.fx.request('/api/operator/save', {'name':'evil'}, cookie=self.cookie, bearer=data['access_token'])[0], 403)
        refreshed = self.fx.request('/api/prospector/token', {'grant_type':'refresh_token','client_id':'prospector','refresh_token':data['refresh_token']})
        self.assertEqual(refreshed[0], 200, refreshed)
        rotated = refreshed[1]['data']
        self.assertEqual(self.fx.request('/api/prospector/me', bearer=data['access_token'])[0], 401)
        self.assertEqual(self.fx.request('/api/prospector/token', {'grant_type':'refresh_token','client_id':'prospector','refresh_token':data['refresh_token']})[0], 400)
        self.assertEqual(self.fx.request('/api/prospector/logout', {}, bearer=rotated['access_token'])[0], 200)
        self.assertEqual(self.fx.request('/api/prospector/me', bearer=rotated['access_token'])[0], 401)
        c = self.fx.connect()
        for table in ('prospector_codes','prospector_tokens'):
            rows = [dict(row) for row in c.execute('SELECT * FROM '+table)]
            self.assertNotIn(data['access_token'], json.dumps(rows))
            self.assertNotIn(data['refresh_token'], json.dumps(rows))
        c.close()

    def test_advanced_filters_apply_before_server_count_and_pagination(self):
        data = self.tokens()[1]['data']
        c = self.fx.connect()
        for pod, route, pol, price in (('Santos','Brazil','Shekou',100),('Brazil','Brazil','South China',200),('Posorja','ECSA','Ningbo',300)):
            c.execute("INSERT INTO freight_items(record_type,entry_id,content_key,pod,route,pol,price_usd,container_type,carrier,valid_to,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)", ('rates','test-'+pod,'key-'+pod,pod,route,pol,price,'40HC','ZIM','2099-12-31','current'))
        c.commit(); c.close()
        query = {'pod':'Santos','podExtra':json.dumps(['Brazil']),'pol':'Shekou','polExtra':json.dumps(['South China']),'terms':json.dumps(['Brazil']),'lanes':json.dumps(['Brazil']),'carrier':'zim','container':'40HQ','limit':'1','offset':'0'}
        status, response, _ = self.fx.request('/api/rates?' + urlencode(query), bearer=data['access_token'])
        self.assertEqual(status,200,response)
        self.assertEqual(response['total'],2)
        self.assertEqual(len(response['rows']),1)
        self.assertEqual(self.fx.request('/api/rates?terms=bad', bearer=data['access_token'])[0],400)

    def test_container_aliases_and_nor_combinations_use_canonical_matching(self):
        data=self.tokens()[1]['data']
        c=self.fx.connect()
        for index,ctype in enumerate(('40HC','40HQ/HC','40NOR','40NOR/HC','20GP')):
            c.execute('INSERT INTO freight_items(record_type,entry_id,content_key,container_type,pod) VALUES(?,?,?,?,?)',('rates','cab-'+str(index),'cab-'+str(index),ctype,'test'))
        c.commit();c.close()
        for ctype,total in (('40HQ',2),('40HC',2),('40NOR',2),('40NOR/40HQ',4)):
            status,result,_=self.fx.request('/api/rates?'+urlencode({'container':ctype}),bearer=data['access_token'])
            self.assertEqual(status,200,result)
            self.assertEqual(result['total'],total,ctype)

    def test_superseded_quotes_stay_excluded_even_when_expired_requested(self):
        data=self.tokens()[1]['data']
        c=self.fx.connect()
        for index,status in enumerate(('当前生效','已被覆盖','已过期')):
            c.execute('INSERT INTO freight_items(record_type,entry_id,content_key,status,valid_to) VALUES(?,?,?,?,?)',('rates','status-'+str(index),'status-'+str(index),status,'2099-12-31'))
        c.commit();c.close()
        for params in ({},{'valid_since':'2026-10-10'},{'status':'已被覆盖'}):
            status,result,_=self.fx.request('/api/rates?'+urlencode(params),bearer=data['access_token'])
            self.assertEqual(status,200,result)
            self.assertFalse(any(row['status']=='已被覆盖' for row in result['rows']))
        self.assertEqual(self.fx.request('/api/rates',cookie=self.cookie)[1]['total'],3)

    def test_expiry_password_reset_and_forced_change_reject_existing_grant(self):
        data = self.tokens()[1]['data']
        c = self.fx.connect()
        c.execute("UPDATE prospector_tokens SET expires_at=0 WHERE kind='access'"); c.commit(); c.close()
        self.assertEqual(self.fx.request('/api/prospector/me', bearer=data['access_token'])[0],401)
        next_data = self.fx.request('/api/prospector/token', {'grant_type':'refresh_token','client_id':'prospector','refresh_token':data['refresh_token']})[1]['data']
        c = self.fx.connect()
        c.execute("UPDATE operators SET password_hash='changed' WHERE user_id=?", (data['user']['user_id'],)); c.commit(); c.close()
        self.assertEqual(self.fx.request('/api/prospector/me', bearer=next_data['access_token'])[0],401)
        self.assertEqual(self.fx.request('/api/prospector/token', {'grant_type':'refresh_token','client_id':'prospector','refresh_token':next_data['refresh_token']})[0],401)

    def test_intentional_unfiltered_prospector_search_is_recorded(self):
        data = self.tokens()[1]['data']
        self.assertEqual(self.fx.request('/api/rates?' + urlencode({'activity':'intentional','query_id':str(uuid.uuid4())}), bearer=data['access_token'])[0],200)
        c = self.fx.connect()
        count = c.execute('SELECT COUNT(*) FROM user_activity_events').fetchone()[0]; c.close()
        self.assertEqual(count,1)

    def test_plain_http_requires_loopback_host(self):
        import urllib.request
        import urllib.error
        request = urllib.request.Request(self.fx.base_url + '/api/prospector/authorize?' + urlencode(self.params), headers={'Host':'navigator.example'})
        try:
            response = urllib.request.urlopen(request,timeout=10)
        except urllib.error.HTTPError as error:
            response=error
        with response: self.assertEqual(response.status,403)

    def test_security_revision_invalidates_grant_even_after_reenabled(self):
        data=self.tokens()[1]['data']
        c=self.fx.connect()
        c.execute("INSERT INTO account_security(user_id,must_change_password,revision) VALUES(?,0,1) ON CONFLICT(user_id) DO UPDATE SET revision=account_security.revision+1",(data['user']['user_id'],));c.commit();c.close()
        self.assertEqual(self.fx.request('/api/prospector/me',bearer=data['access_token'])[0],401)

    def test_device_revocation_is_authoritative_beyond_token_rows(self):
        data=self.tokens()[1]['data']
        self.assertEqual(self.fx.request('/api/prospector/logout',{},bearer=data['access_token'])[0],200)
        c=self.fx.connect()
        # Models a late token insertion: revocation of its device must still win.
        c.execute('UPDATE prospector_tokens SET revoked=0');c.commit();c.close()
        self.assertEqual(self.fx.request('/api/prospector/me',bearer=data['access_token'])[0],401)
        self.assertEqual(self.fx.request('/api/prospector/token',{'grant_type':'refresh_token','client_id':'prospector','refresh_token':data['refresh_token']})[0],400)

    def test_forced_password_change_and_current_role_are_live(self):
        data = self.tokens()[1]['data']
        c = self.fx.connect()
        c.execute("UPDATE operators SET role='sales' WHERE user_id=?",(data['user']['user_id'],)); c.commit(); c.close()
        status, result, _ = self.fx.request('/api/prospector/me',bearer=data['access_token'])
        self.assertEqual(status,200)
        self.assertEqual(result['data']['user']['role'],'sales')
        c = self.fx.connect()
        c.execute("INSERT INTO account_security(user_id,must_change_password) VALUES(?,1) ON CONFLICT(user_id) DO UPDATE SET must_change_password=1",(data['user']['user_id'],)); c.commit(); c.close()
        self.assertEqual(self.fx.request('/api/prospector/me',bearer=data['access_token'])[0],401)
        self.assertEqual(self.fx.request('/api/prospector/token',{'grant_type':'refresh_token','client_id':'prospector','refresh_token':data['refresh_token']})[0],401)

    def test_concurrent_code_and_refresh_exchange_have_single_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        code = self.code()
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses=list(executor.map(lambda _: self.tokens(code),range(2)))
        self.assertEqual(sorted(r[0] for r in responses),[200,400])
        data=next(r[1]['data'] for r in responses if r[0]==200)
        body={'grant_type':'refresh_token','client_id':'prospector','refresh_token':data['refresh_token']}
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses=list(executor.map(lambda _: self.fx.request('/api/prospector/token',body),range(2)))
        self.assertEqual(sorted(r[0] for r in responses),[200,400])
        current=next(r[1]['data'] for r in responses if r[0]==200)
        self.assertEqual(self.fx.request('/api/prospector/me',bearer=current['access_token'])[0],200)
        # Cookie identity cannot choose another device for logout.
        self.assertEqual(self.fx.request('/api/prospector/logout',{},cookie=self.cookie,bearer=current['access_token'])[0],200)
        self.assertEqual(self.fx.request('/api/prospector/me',bearer=current['access_token'])[0],401)
        self.assertEqual(self.fx.request('/api/rates',cookie=self.cookie)[0],200)

    def test_concurrent_refresh_and_accepted_logout_leave_device_revoked(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        for _ in range(3):
            data=self.tokens()[1]['data']
            barrier=Barrier(2)
            def refresh():
                barrier.wait()
                return self.fx.request('/api/prospector/token',{'grant_type':'refresh_token','client_id':'prospector','refresh_token':data['refresh_token']})
            def logout():
                barrier.wait()
                return self.fx.request('/api/prospector/logout',{},bearer=data['access_token'])
            with ThreadPoolExecutor(max_workers=2) as executor:
                refreshing=executor.submit(refresh)
                logging_out=executor.submit(logout)
                refreshed=refreshing.result()
                exited=logging_out.result()
            self.assertIn(refreshed[0],(200,400))
            self.assertIn(exited[0],(200,401))
            if exited[0]==200:
                if refreshed[0]==200:
                    self.assertEqual(self.fx.request('/api/prospector/me',bearer=refreshed[1]['data']['access_token'])[0],401)
                self.assertEqual(self.fx.request('/api/prospector/token',{'grant_type':'refresh_token','client_id':'prospector','refresh_token':data['refresh_token']})[0],400)

    def test_web_match_reuses_browser_exact_filters(self):
        data=self.tokens()[1]['data']
        c=self.fx.connect()
        import manualdb
        manualdb.master_save(c,'ports',{'name':'Santos','kind':'pod','active':1})
        manualdb.master_save(c,'ports',{'name':'Shekou','kind':'pol','active':1})
        for index,pod in enumerate(('Santos','Santos Extra','Posorja')):
            c.execute("INSERT INTO freight_items(record_type,entry_id,content_key,pod,route,pol,price_usd,container_type,carrier,valid_to,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)", ('rates','web-'+str(index),'web-key-'+str(index),pod,'Brazil','Shekou',100+index,'40HQ/HC','ZIM','2025-01-01','current'))
        c.commit();c.close()
        query={'pod':'Santos','pol':'Shekou','carrier':'ZIM','route':'Brazil','ctype':'40HQ/HC','sort':'synced_at','direction':'desc'}
        browser=self.fx.request('/api/rates?'+urlencode(query),cookie=self.cookie)
        desktop=self.fx.request('/api/rates?'+urlencode(dict(query,match='web')),bearer=data['access_token'])
        self.assertEqual(browser[0],200,browser)
        self.assertEqual(desktop[0],200,desktop)
        self.assertEqual(desktop[1]['total'],browser[1]['total'])
        self.assertEqual([r['entry_id'] for r in desktop[1]['rows']],[r['entry_id'] for r in browser[1]['rows']])
        self.assertEqual(desktop[1]['total'],1)
        options=self.fx.request('/api/prospector/options',bearer=data['access_token'])
        self.assertEqual(options[0],200,options)
        self.assertEqual(set(options[1]['data']),{'ports','portGroups','carriers','routes'})

    def test_image_bearer_path_is_exact_basename(self):
        data=self.tokens()[1]['data']
        for path in ('/images/sub%2Ffile.png','/images/sub%5Cfile.png'):
            self.assertEqual(self.fx.request(path,bearer=data['access_token'])[0],403)

    def test_snapshot_excludes_private_grants(self):
        self.tokens()
        c=self.fx.connect()
        snapshot=__import__('manualdb').export_all(c)
        self.assertNotIn('prospector_codes',snapshot)
        self.assertNotIn('prospector_tokens',snapshot)
        c.close()

    def test_authorize_requires_origin_session_and_exact_loopback(self):
        self.assertEqual(self.fx.request('/api/prospector/authorize', self.params, self.cookie)[0], 403)
        self.assertEqual(self.fx.request('/api/prospector/authorize', self.params, origin=self.fx.base_url)[0], 401)
        for redirect in ('https://127.0.0.1:123/navigator/callback','http://localhost:123/navigator/callback','http://127.0.0.1:123/navigator/callback?x=1','http://127.0.0.1:123/else'):
            self.assertEqual(self.fx.request('/api/prospector/authorize?' + urlencode({**self.params,'redirect_uri':redirect}))[0], 400)
        self.assertEqual(self.fx.request('/api/prospector/authorize?' + urlencode(self.params))[0], 200)

    def test_live_account_and_query_dedup_cannot_spoof_identity(self):
        data = self.tokens()[1]['data']
        query_id = str(uuid.uuid4())
        suffix = '?' + urlencode({'pod':'ECPOS','query_id':query_id,'activity':'intentional','_actor_id':'evil','_client_app':'web'})
        for path in ('/api/rates','/api/space','/api/rates_grouped'):
            self.assertEqual(self.fx.request(path+suffix, bearer=data['access_token'])[0], 200)
        self.assertEqual(self.fx.request('/api/rates?activity=intentional&query_id=bad', bearer=data['access_token'])[0], 400)
        c = self.fx.connect()
        rows = c.execute('SELECT user_id,properties_json FROM user_activity_events').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['user_id'], data['user']['user_id'])
        self.assertEqual(json.loads(rows[0]['properties_json'])['client_app'], 'prospector')
        c.execute('UPDATE operators SET active=0 WHERE user_id=?', (data['user']['user_id'],)); c.commit(); c.close()
        self.assertEqual(self.fx.request('/api/prospector/me', bearer=data['access_token'])[0], 401)

if __name__ == '__main__': unittest.main()



class ProspectorTransportTests(unittest.TestCase):
    def test_private_lan_requires_both_real_peer_and_literal_host(self):
        from manual_server import Handler
        handler = Handler.__new__(Handler)
        handler.connection = object()
        for peer, host, expected in (
            ('192.168.190.12', '192.168.190.244:8795', True),
            ('10.0.0.2', '10.0.0.3:8795', True),
            ('172.31.0.2', '172.16.0.3:8795', True),
            ('127.0.0.1', '127.0.0.1:8795', True),
            ('203.0.113.10', '192.168.190.244:8795', False),
            ('192.168.190.12', 'navigator.example:8795', False),
            ('192.168.190.12', '203.0.113.10:8795', False),
            ('172.32.0.2', '172.16.0.3:8795', False),
            ('192.168.190.12', 'bad-host:8795', False),
        ):
            with self.subTest(peer=peer, host=host):
                handler.client_address = (peer, 12345)
                handler.headers = {'Host': host, 'X-Forwarded-For': '192.168.190.12', 'X-Forwarded-Proto': 'https'}
                self.assertEqual(handler._prospector_transport_allowed(), expected)
