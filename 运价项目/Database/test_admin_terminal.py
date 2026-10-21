"""End-to-end admin terminal and forced-password boundary on disposable stores."""
import json
import secrets
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
import manualdb
import manual_server as server
from core.admin_contracts import ADMIN_ACCOUNTS, ADMIN_ACCOUNT, ADMIN_PROFILE, ADMIN_SECURITY, ADMIN_EVENTS, ADMIN_RECORDS
from core.api_monitor_contracts import API_EVENTS_ROUTE
from core.activity_contracts import ACTIVITY_QUOTE_USAGE_ROUTE, ACTIVITY_TIMELINE_ROUTE
from services.admin_accounts import profile_fields
from datetime import date


def verify_terminal(case, connect):
    password = secrets.token_urlsafe(20)
    c=connect()
    try:
        for table in ('auth_sessions','operators','account_profiles','account_security','account_events','announcements'):
            c.execute('DELETE FROM '+table)
        c.commit()
        for role in ('admin','operator','sales'):
            manualdb.save_operator(c,{'name':'terminal-'+role,'role':role,'active':1,'password_hash':server.password_hash(password)})
    finally:c.close()
    with patch.object(server,'conn',side_effect=connect), patch.object(server,'LOGIN_LIMITER',server.LoginLimiter()):
        http=server.FreightHTTPServer(('127.0.0.1',0),server.Handler)
        worker=threading.Thread(target=http.serve_forever);worker.start()
        base='http://127.0.0.1:%d'%http.server_port
        def request(path, body=None, cookie=''):
            req=urllib.request.Request(base+path,data=json.dumps(body,ensure_ascii=False).encode('utf-8') if body is not None else None,
                headers={'Content-Type':'application/json','Cookie':cookie,'Origin':base})
            try:response=urllib.request.urlopen(req,timeout=10)
            except urllib.error.HTTPError as error:response=error
            with response:return response.status,json.load(response),response.headers
        try:
            cookies={}
            for role in ('admin','operator','sales'):
                status,result,headers=request('/api/auth/login',{'name':'terminal-'+role,'password':password})
                case.assertEqual(status,200,result);cookies[role]=headers['Set-Cookie'].split(';',1)[0]
            usage=request(ACTIVITY_QUOTE_USAGE_ROUTE,{'action':'copied','quote_kind':'rates','item_count':1,
                'field_type':'','_actor_id':'forged'},cookies['sales'])
            case.assertEqual(usage[0],200,usage)
            quote_rows=request(ACTIVITY_TIMELINE_ROUTE+'?days=1&category=quote',cookie=cookies['admin'])[1]['data']['rows']
            case.assertEqual(len(quote_rows),1)
            case.assertEqual(quote_rows[0]['display_name'],'terminal-sales')
            case.assertEqual(quote_rows[0]['details'],{'quote_kind':'rates','item_count':1,'field_type':''})
            for role in ('operator','sales'):
                for path in (ADMIN_ACCOUNTS,ADMIN_ACCOUNT,ADMIN_EVENTS,ADMIN_RECORDS):case.assertEqual(request(path,cookie=cookies[role])[0],403)
                for path in (ADMIN_ACCOUNTS,ADMIN_PROFILE,ADMIN_SECURITY):case.assertEqual(request(path,{},cookies[role])[0],403)
            admin=cookies['admin']
            api_log=request(API_EVENTS_ROUTE+'?days=1&limit=100',cookie=admin)[1]['data']['rows']
            case.assertTrue(any(r['path']=='/api/auth/login' and r['user_id'] for r in api_log),
                            'successful login must be attributed to the authenticated account')
            listing=request(ADMIN_ACCOUNTS+'?limit=1&role=sales',cookie=admin)[1]['data']
            case.assertEqual(listing['total'],1);case.assertEqual(listing['summary']['total'],3)
            created=request(ADMIN_ACCOUNTS,{'name':'terminal-new','display_name':'新人员','role':'operator'},admin)[1]
            case.assertTrue(created['ok'],created);uid=created['data']['user_id']
            def detail():return request(ADMIN_ACCOUNT+'?user_id='+uid,cookie=admin)[1]['data']
            original=detail();case.assertEqual(original['login_status'],'unopened')
            c=connect()
            try:
                c.execute('INSERT INTO freight_items(record_type,sender,pol,pod,content_key,entry_id) VALUES(?,?,?,?,?,?)',('rates','terminal-new','TEST-POL','TEST-POD','terminal-record','terminal-entry'))
                c.execute('INSERT INTO pending_records(entry_id,kind,payload,check_status,review_status) VALUES(?,?,?,?,?)',
                          ('terminal-pending','rates',json.dumps({'发送人':'terminal-new','起运港':'TEST-POL','目的港':'TEST-POD'},ensure_ascii=False),'passed','awaiting'))
                c.execute('INSERT INTO announcements(id,title,body_html,group_name,tags_json,author_id,author_name,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                          ('terminal-notice','测试公告','<p>test</p>','', '[]',uid,'terminal-new','2026-09-30 10:00:00','2026-09-30 10:00:00'))
                publisher=c.execute('SELECT publisher_id FROM operators WHERE user_id=?',(uid,)).fetchone()[0]
                c.execute('UPDATE freight_items SET publisher_id=? WHERE entry_id=?',(publisher,'terminal-entry'))
                c.execute('UPDATE pending_records SET publisher_id=? WHERE entry_id=?',(publisher,'terminal-pending'))
                c.commit()
            finally:c.close()
            record=request(ADMIN_RECORDS+'?user_id='+uid,cookie=admin)[1]['data']
            case.assertEqual(record['total'],1);case.assertEqual(record['rows'][0]['entry_id'],'terminal-entry')
            for source in ('awaiting','rejected'):
                case.assertEqual(request(ADMIN_RECORDS+'?user_id='+uid+'&source='+source,cookie=admin)[0],200)

            profile={'user_id':uid,'section':'profile','expected_signature':original['signature'],'fields':{'display_name':'人员新名','department':'航管部'}}
            case.assertTrue(request(ADMIN_PROFILE,profile,admin)[1]['ok'])
            case.assertEqual(request(ADMIN_PROFILE,profile,admin)[0],400,'stale writes must fail')
            changed=detail();case.assertEqual(changed['name'],'terminal-new');case.assertEqual(changed['profile']['department'],'航管部')
            invalid={**profile,'expected_signature':changed['signature'],'fields':{'role':'admin'}}
            case.assertEqual(request(ADMIN_PROFILE,invalid,admin)[0],400)
            temporary=secrets.token_urlsafe(20)
            action={'user_id':uid,'action':'activate','new_password':temporary,'confirm_password':temporary,'expected_signature':changed['signature']}
            activated=request(ADMIN_SECURITY,action,admin)[1];case.assertTrue(activated['ok'],activated)
            case.assertEqual(request(ADMIN_SECURITY,action,admin)[0],400)
            status,login,headers=request('/api/auth/login',{'name':'terminal-new','password':temporary})
            case.assertTrue(login['user']['must_change_password']);limited=headers['Set-Cookie'].split(';',1)[0]
            status,denied,_=request('/api/rates',cookie=limited);case.assertEqual(status,403);case.assertEqual(denied['code'],'password_change_required')
            case.assertEqual(request('/api/account/profile',{'display_name':'forged','preferences':{}},limited)[0],403)
            permanent=secrets.token_urlsafe(20)
            changed_password=request('/api/auth/change-password',{'current_password':temporary,'new_password':permanent,'confirm_password':permanent},limited)
            case.assertTrue(changed_password[1]['ok'],changed_password)
            status,login,headers=request('/api/auth/login',{'name':'terminal-new','password':permanent})
            case.assertFalse(login['user']['must_change_password']);regular=headers['Set-Cookie'].split(';',1)[0]
            case.assertEqual(request('/api/rates',cookie=regular)[0],200)
            before=detail();case.assertEqual(len(before['sessions']),1)
            revoked=request(ADMIN_SECURITY,{'user_id':uid,'action':'revoke_sessions','expected_signature':before['signature']},admin)[1]
            case.assertEqual(revoked['data']['revoked_sessions'],1)
            case.assertEqual(request('/api/rates',cookie=regular)[0],401)
            # Every visible security action is a real endpoint, not a UI-only toggle.
            for action,extra in [('disable',{}),('enable',{}),('role',{'role':'sales'}),('require_password_change',{})]:
                latest=detail()
                result=request(ADMIN_SECURITY,{'user_id':uid,'action':action,'expected_signature':latest['signature'],**extra},admin)
                case.assertEqual(result[0],200,result)
                after=detail()
                if action=='disable':case.assertEqual(after['login_status'],'disabled')
                if action=='enable':case.assertEqual(after['login_status'],'enabled')
                if action=='role':
                    case.assertEqual(after['role'],'sales')
                    status,login,headers=request('/api/auth/login',{'name':'terminal-new','password':permanent})
                    case.assertEqual(status,200);sales_cookie=headers['Set-Cookie'].split(';',1)[0]
                    before_noop=detail();case.assertEqual(len(before_noop['sessions']),1)
                    noop=request(ADMIN_SECURITY,{'user_id':uid,'action':'role','role':'sales','expected_signature':before_noop['signature']},admin)
                    case.assertEqual(noop[0],400,noop)
                    case.assertEqual(len(detail()['sessions']),1,'same-role no-op must not revoke login sessions')
                    case.assertEqual(request('/api/rates',cookie=sales_cookie)[0],200)
                if action=='require_password_change':case.assertTrue(after['must_change_password'])
            reset_password=secrets.token_urlsafe(20)
            latest=detail()
            reset=request(ADMIN_SECURITY,{'user_id':uid,'action':'reset_password','expected_signature':latest['signature'],'new_password':reset_password,'confirm_password':reset_password},admin)
            case.assertEqual(reset[0],200,reset)
            case.assertEqual(request('/api/auth/login',{'name':'terminal-new','password':permanent})[0],403)
            case.assertTrue(request('/api/auth/login',{'name':'terminal-new','password':reset_password})[1]['user']['must_change_password'])
            latest=detail()
            experience=request(ADMIN_PROFILE,{'user_id':uid,'section':'experience','expected_signature':latest['signature'],'fields':{'career_start_year':'2018','skills':'fixture experience'}},admin)
            case.assertEqual(experience[0],200,experience)
            case.assertEqual(detail()['experience']['skills'],'fixture experience')
            latest=detail()
            renamed=request(ADMIN_SECURITY,{'user_id':uid,'action':'rename_login','expected_signature':latest['signature'],'new_name':'easy-login'},admin)
            case.assertEqual(renamed[0],200,renamed)
            renamed_detail=detail()
            case.assertEqual(renamed_detail['name'],'easy-login')
            case.assertEqual(renamed_detail['publisher_name'],'terminal-new')
            case.assertEqual(renamed_detail['user_id'],uid)
            case.assertEqual(renamed_detail['business']['published'],1)
            case.assertEqual(renamed_detail['business']['awaiting'],1)
            case.assertEqual(request(ADMIN_RECORDS+'?user_id='+uid,cookie=admin)[1]['data']['total'],1)
            case.assertEqual(request('/api/auth/login',{'name':'terminal-new','password':reset_password})[0],403)
            case.assertEqual(request('/api/auth/login',{'name':'easy-login','password':reset_password})[0],200)
            c=connect()
            try:
                notice=c.execute('SELECT author_id,author_name FROM announcements WHERE id=?',('terminal-notice',)).fetchone()
                case.assertEqual(notice['author_id'],uid)
                case.assertEqual(notice['author_name'],'terminal-new','公告署名保留发布时快照')
                case.assertEqual(manualdb.ensure_publisher_people(c,['terminal-new']),0,'改登录账号后同步发布人不得创建重复人员')
                case.assertEqual(c.execute('SELECT COUNT(*) FROM operators WHERE publisher_name=?',('terminal-new',)).fetchone()[0],1)
            finally:c.close()
            all_rows=request(ADMIN_ACCOUNTS,cookie=admin)[1]['data']['rows']
            own=next(r for r in all_rows if r['role']=='admin')
            own_detail=request(ADMIN_ACCOUNT+'?user_id='+own['user_id'],cookie=admin)[1]['data']
            case.assertEqual(request(ADMIN_SECURITY,{'user_id':own['user_id'],'action':'disable','expected_signature':own_detail['signature']},admin)[0],403)
            history=request(ADMIN_EVENTS+'?user_id='+uid+'&limit=100',cookie=admin)[1]['data']['rows']
            case.assertTrue(any(r['action']=='profile_updated' for r in history))
            case.assertTrue(any(r['action']=='login_success' for r in history))
            case.assertTrue(any(r['action']=='security_rename_login' for r in history))
            safe=json.dumps([detail(),history,all_rows],ensure_ascii=False)
            for secret in (password,temporary,permanent,'password_hash','token_hash','new_password'):
                case.assertNotIn(secret,safe)
        finally:
            http.shutdown();worker.join(timeout=10);http.server_close();manualdb.invalidate_master()


class AdminTerminalTests(unittest.TestCase):
    def test_sqlite_http(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'terminal.db'
            verify_terminal(self,lambda:manualdb.connect(path))
    def test_profile_validation(self):
        today=date(2026,9,29)
        for section,fields in [('profile',{'role':'admin'}),('experience',{'career_start_year':'2099'}),('profile',{'work_email':'wrong'})]:
            with self.assertRaises(ValueError):profile_fields(section,fields,today)
        self.assertEqual(profile_fields('experience',{'career_start_year':'2018'},today),{'career_start_year':'2018'})

if __name__=='__main__':unittest.main()

