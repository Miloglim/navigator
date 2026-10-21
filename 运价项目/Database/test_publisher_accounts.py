"""Real HTTP publisher mutations target a temporary roster and SQLite database."""
import json
from pathlib import Path
import secrets
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import manualdb
import manual_server as server
from ops import publisher_accounts as adapter
from freight_identity import runtime
from test_port_routes import seed


class PublisherAccountHTTPTests(unittest.TestCase):
    def test_authorization_and_full_http_to_collector_sync(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);database=root/'fixture.db';work=root/'work';work.mkdir()
            roster=root/'roster.json';runtime.atomic_json(roster,{'version':'1.1.0','revision':0,'members':[],'audit':[]})
            groups=[{'cid':'fixture-group','slug':'fixture','name':'测试群'}]
            msg={'id':'fixture-message','senderId':'account-1','senderOpenDingTalkId':'open-1',
                 't':'2026-09-28T12:00:00+08:00','s':'测试航管','c':'报价更新','img':[]}
            runtime.atomic_json(work/'msgs_fixture/fixture.json',[msg])
            def connection():return manualdb.connect(str(database))
            conn=connection();manualdb.init_schema(conn);seed(conn)
            password=secrets.token_urlsafe(24)
            for role in ('admin','operator','sales'):
                manualdb.save_operator(conn,{'name':'fixture-'+role,'role':role,'password_hash':server.password_hash(password)})
            conn.close()
            def snap(*args,**kwargs):return runtime.snapshot(work,groups=groups,roster_path=roster,as_of='2026-09-28')
            def save(req,candidate,actor,actor_id):return runtime.save_decision(req,candidate,actor,actor_id,roster)
            def confirmed():
                return adapter.PublisherPeople(runtime.load_roster(roster)['members'])
            with patch.object(server,'conn',side_effect=connection),patch.object(server,'confirmed_people',side_effect=confirmed),\
                 patch.object(adapter,'snapshot',side_effect=snap),patch.object(adapter,'save_decision',side_effect=save):
                http=server.FreightHTTPServer(('127.0.0.1',0),server.Handler)
                thread=threading.Thread(target=http.serve_forever);thread.start()
                self.addCleanup(http.server_close)
                base='http://127.0.0.1:%d'%http.server_port
                def request(path,data=None,cookie=''):
                    req=urllib.request.Request(base+path,data=json.dumps(data).encode() if data is not None else None,
                                               headers={'Content-Type':'application/json','Cookie':cookie})
                    try:response=urllib.request.urlopen(req,timeout=10)
                    except urllib.error.HTTPError as error:response=error
                    with response:return response.status,json.load(response),response.headers
                try:
                    route='/api/publishers/accounts';decision=route
                    self.assertEqual(request(route)[0],401)
                    self.assertEqual(request('/api/publishers/decision',{},cookie='')[0],404)
                    cookies={}
                    for role in ('admin','operator','sales'):
                        status,data,headers=request('/api/auth/login',{'name':'fixture-'+role,'password':password})
                        self.assertEqual(status,200);cookies[role]=headers['Set-Cookie'].split(';',1)[0]
                    self.assertEqual(request(route,cookie=cookies['sales'])[0],403)
                    self.assertEqual(request(route,cookie=cookies['operator'])[0],403)
                    self.assertEqual(request(decision,{},cookies['operator'])[0],403)
                    binding_route='/api/admin/account/publisher'
                    self.assertEqual(request(binding_route)[0],401)
                    for role in ('operator','sales'):
                        self.assertEqual(request(binding_route,cookie=cookies[role])[0],403)
                        self.assertEqual(request(binding_route,{},cookies[role])[0],403)
                    data=request(route,cookie=cookies['admin'])[1]['data'];candidate=data['candidates'][0]
                    self.assertFalse(candidate['account_library']['registered'])
                    confirmed_result=request(decision,{'candidate_id':candidate['candidate_id'],'action':'confirm',
                        'expected_revision':0,'_actor':'spoofed'},cookies['admin'])[1]
                    self.assertTrue(confirmed_result['ok'],confirmed_result)
                    self.assertEqual(confirmed_result['data']['account_created'],1)
                    data=request(route,cookie=cookies['admin'])[1]['data']
                    self.assertEqual(data['roster_revision'],1)
                    self.assertEqual(data['candidates'][0]['assessment']['status'],'publisher')
                    self.assertTrue(data['candidates'][0]['account_library']['registered'])
                    self.assertFalse(data['candidates'][0]['account_library']['login_enabled'])
                    self.assertEqual(len(data['audit']),1)
                    self.assertEqual(data['audit'][0]['note'],'管理员直接确认')
                    repeated=request(decision,{'candidate_id':candidate['candidate_id'],'action':'confirm',
                        'expected_revision':1},cookies['admin'])[1]
                    self.assertTrue(repeated['ok'],repeated)
                    self.assertTrue(repeated['data']['unchanged'])
                    self.assertEqual(repeated['data']['account_created'],0)
                    repeated_data=request(route,cookie=cookies['admin'])[1]['data']
                    self.assertEqual(repeated_data['roster_revision'],1)
                    self.assertEqual(len(repeated_data['audit']),1)
                    self.assertEqual(request('/api/operator/delete',{'name':'测试航管'},cookies['admin'])[0],400)
                    for rev,action,status in [(1,'exclude','excluded'),(2,'reset','insufficient')]:
                        current_candidate_id = (data['exclusions'][0]['candidate_id']
                                                if action == 'reset' else candidate['candidate_id'])
                        payload={'candidate_id':current_candidate_id,'action':action,
                                 'expected_revision':rev,'_actor':'spoofed'}
                        if action != 'exclude':payload['note']='测试业务依据'
                        result=request(decision,payload,cookies['admin'])[1]
                        self.assertTrue(result['ok'],result)
                        data=request(route,cookie=cookies['admin'])[1]['data']
                        self.assertEqual(data['roster_revision'],rev+1)
                        self.assertEqual(data['candidates'][0]['assessment']['status'],status)
                        self.assertEqual(len(data['exclusions']),1 if action == 'exclude' else 0)
                        self.assertFalse(data['candidates'][0]['account_library']['registered'])
                        collector=runtime.decision_for(snap(),'fixture',msg)
                        self.assertEqual(collector['status'],status)
                        self.assertEqual(collector['action'],'exclude')
                        self.assertTrue(collector['suppressed'])
                        rows=request('/api/operators',cookie=cookies['admin'])[1]['rows']
                        self.assertEqual(sum(row['name']=='测试航管' for row in rows),0)
                        if action == 'exclude':self.assertEqual(result['data']['account_removed'],1)
                        check=connection()
                        try:self.assertEqual(manualdb.ensure_publisher_people(check,confirmed()),0)
                        finally:check.close()
                        self.assertEqual(request('/api/auth/login',{'name':'测试航管','password':password})[0],403)
                    created=request('/api/operator/save',{'name':'仅本地身份','role':'sales','active':1},cookies['admin'])[1]
                    self.assertTrue(created['ok'],created)
                    rows=request('/api/operators',cookie=cookies['admin'])[1]['rows']
                    person=next(row for row in rows if row['name']=='仅本地身份')
                    self.assertFalse(person['active'])
                    self.assertFalse(person['login_enabled'])
                    self.assertEqual(request('/api/auth/login',{'name':'仅本地身份','password':password})[0],403)
                    upgraded=request('/api/operator/save',{'name':'测试航管','password':password,'active':1},cookies['admin'])[1]
                    self.assertTrue(upgraded['ok'],upgraded)
                    self.assertEqual(request('/api/auth/login',{'name':'测试航管','password':password})[0],200)
                    person=next(row for row in request('/api/operators',cookie=cookies['admin'])[1]['rows'] if row['name']=='测试航管')
                    self.assertTrue(person['login_enabled'])
                    self.assertEqual(data['audit'][0]['actor'],'fixture-admin')
                    self.assertEqual(len(data['audit']),3)
                    stale=request(decision,{'candidate_id':candidate['candidate_id'],'action':'exclude',
                        'expected_revision':0,'note':'test'},cookies['admin'])[1]
                    self.assertEqual(stale['code'],'revision_conflict')
                finally:http.shutdown();thread.join()

if __name__=='__main__':unittest.main()

