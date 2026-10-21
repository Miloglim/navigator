"""Personal profile and query-scope checks on disposable HTTP databases."""
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import patch
import manualdb
import manual_server as server
from core.contracts import ACCOUNT_AVATAR_IDS, ACCOUNT_AVATAR_ALL_IDS, account_avatar_groups


def verify_profile(case, connect):
    c=connect()
    try:
        for table in ('account_recovery_requests','auth_sessions','operators','freight_items','port_routes','routes','chat_groups'):
            c.execute('DELETE FROM '+table)
        c.commit()
        for name in ('Route A','Route B','Route C'):
            c.execute('INSERT INTO routes(name,active) VALUES(?,1)',(name,))
        for name in ('Group A','Group B'):
            c.execute('INSERT INTO chat_groups(name,active) VALUES(?,1)',(name,))
        c.commit()
        manualdb.save_operator(c,{'name':'profile-sales','role':'sales','password_hash':server.password_hash('fixture-pass')})
        manualdb.save_operator(c,{'name':'other-sales','role':'sales','password_hash':server.password_hash('fixture-pass')})
        for index,(route,group) in enumerate([('Route A','Group A'),('Route B','Group A'),('Route C','Group B')]):
            c.execute('INSERT INTO freight_items(record_type,route,source_group,container_type,status,content_key,entry_id) VALUES(?,?,?,?,?,?,?)',('rates',route,group,'20GP','当前有效','fixture-sign-'+str(index),'fixture-entry-'+str(index)))
        c.commit()
    finally:c.close()
    with patch.object(server,'conn',side_effect=connect):
        http=server.FreightHTTPServer(('127.0.0.1',0),server.Handler);thread=threading.Thread(target=http.serve_forever);thread.start()
        base='http://127.0.0.1:%d'%http.server_port
        def call(path,body=None,cookie='',headers=None):
            request=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json','Cookie':cookie,**(headers or {})})
            try:res=urllib.request.urlopen(request,timeout=15)
            except urllib.error.HTTPError as error:res=error
            with res:return res.status,json.load(res),res.headers
        try:
            case.assertEqual(call('/api/account/profile')[0],401)
            cookie=call('/api/auth/login',{'name':'profile-sales','password':'fixture-pass'})[2]['Set-Cookie'].split(';')[0]
            profile=call('/api/account/profile',cookie=cookie)[1]['data']
            case.assertEqual(profile['name'],'profile-sales');case.assertEqual(profile['current_ip'],'127.0.0.1');case.assertTrue(profile['login_at']);case.assertTrue(profile['expires_at'])
            case.assertIs(profile['preferences']['growth_liquid_enabled'],True)
            case.assertIsNone(profile['preferences']['avatar_id'])
            case.assertEqual(profile['options']['avatar_ids'],list(ACCOUNT_AVATAR_ALL_IDS))
            avatar_groups=profile['options']['avatar_groups']
            case.assertEqual([group['label'] for group in avatar_groups],['女','男'])
            case.assertEqual([len(group['people']) for group in avatar_groups],[5,5])
            case.assertEqual([len(person['looks']) for group in avatar_groups for person in group['people']],[3]*10)
            case.assertEqual([look['id'] for group in avatar_groups for person in group['people'] for look in person['looks']],list(ACCOUNT_AVATAR_IDS))
            case.assertNotIn('password_hash',profile)
            body={'display_name':'My sales name','preferences':{'routes':['Route A','Route B'],'groups':['Group A'],'ctypes':['20GP'],'start_tab':'rates'}}
            for injected in ({'name':'other-sales'},{'role':'admin'},{'user_id':'other-id'},{'groups':['Group B']}):
                case.assertEqual(call('/api/account/profile',{**body,**injected},cookie)[0],400)
            case.assertEqual(call('/api/account/profile',body,cookie,{'Origin':'https://external.invalid'})[0],403)
            case.assertEqual(call('/api/account/profile',{**body,'display_name':''},cookie)[0],400)
            case.assertEqual(call('/api/account/profile',{**body,'preferences':{**body['preferences'],'routes':['unknown']}},cookie)[0],400)
            case.assertEqual(call('/api/account/profile',body,cookie)[0],200)
            case.assertEqual(call('/api/account/profile',cookie=cookie)[1]['data']['preferences'],{**body['preferences'],'ctypes':[],'growth_liquid_enabled':True,'avatar_id':None,'company_rates_enabled':False,'carrier_realtime_enabled':False})
            source_body={**body,'preferences':{**body['preferences'],'company_rates_enabled':True,'carrier_realtime_enabled':True}}
            source_preferences=call('/api/account/profile',source_body,cookie)[1]['data']['preferences']
            case.assertTrue(source_preferences['company_rates_enabled']);case.assertTrue(source_preferences['carrier_realtime_enabled'])
            # Older clients omit these optional fields; unrelated saves preserve both choices.
            preserved=call('/api/account/profile',body,cookie)[1]['data']['preferences']
            case.assertTrue(preserved['company_rates_enabled']);case.assertTrue(preserved['carrier_realtime_enabled'])
            for invalid in ('av-00','av-31','av2-w06-1','av2-m01-4','../av-01',3,False,[],{}):
                case.assertEqual(call('/api/account/profile',{**body,'preferences':{**body['preferences'],'avatar_id':invalid}},cookie)[0],400)
            avatar_body={**body,'preferences':{**body['preferences'],'avatar_id':'av-17'}}
            case.assertEqual(call('/api/account/profile',avatar_body,cookie)[1]['data']['preferences']['avatar_id'],'av-17')
            # A client without the new field must not reset a previously chosen avatar.
            case.assertEqual(call('/api/account/profile',body,cookie)[1]['data']['preferences']['avatar_id'],'av-17')
            case.assertEqual(call('/api/account/profile',cookie=cookie)[1]['data']['preferences']['avatar_id'],'av-17')
            case.assertEqual(call('/api/account/profile',{**body,'preferences':{**body['preferences'],'avatar_id':None}},cookie)[1]['data']['preferences']['avatar_id'],None)
            case.assertEqual(call('/api/account/profile',avatar_body,cookie)[0],200)
            with urllib.request.urlopen(base+'/assets/avatars/av-17.png',timeout=15) as asset:
                case.assertEqual(asset.status,200)
                case.assertEqual(asset.headers['Content-Type'],'image/png')
                case.assertEqual(asset.read(8),b'\x89PNG\r\n\x1a\n')
            new_avatar_body={**body,'preferences':{**body['preferences'],'avatar_id':'av2-m03-2'}}
            case.assertEqual(call('/api/account/profile',new_avatar_body,cookie)[1]['data']['preferences']['avatar_id'],'av2-m03-2')
            with urllib.request.urlopen(base+'/assets/avatars/av2-m03-2.png',timeout=15) as asset:
                case.assertEqual(asset.status,200)
                case.assertEqual(asset.read(8),b'\x89PNG\r\n\x1a\n')
            case.assertEqual(call('/api/account/profile',body,cookie)[1]['data']['preferences']['avatar_id'],'av2-m03-2')
            case.assertEqual(call('/assets/avatars/../av-17.png',cookie=cookie)[0],404)
            case.assertEqual(call('/assets/avatars/av-31.png',cookie=cookie)[0],404)
            case.assertEqual(call('/assets/avatars/av2-w06-1.png',cookie=cookie)[0],404)
            enabled_body={**body,'preferences':{**body['preferences'],'growth_liquid_enabled':True}}
            case.assertEqual(call('/api/account/profile',enabled_body,cookie)[1]['data']['preferences']['growth_liquid_enabled'],True)
            case.assertEqual(call('/api/account/profile',cookie=cookie)[1]['data']['preferences']['growth_liquid_enabled'],True)
            case.assertEqual(call('/api/account/profile',{**body,'preferences':{**body['preferences'],'growth_liquid_enabled':'true'}},cookie)[0],400)
            # An old browser omits the optional setting; saving unrelated preferences must preserve it.
            case.assertEqual(call('/api/account/profile',body,cookie)[1]['data']['preferences']['growth_liquid_enabled'],True)
            disabled_body={**body,'preferences':{**body['preferences'],'growth_liquid_enabled':False}}
            case.assertEqual(call('/api/account/profile',disabled_body,cookie)[1]['data']['preferences']['growth_liquid_enabled'],True)
            for start_tab in ('dyn','group'):
                case.assertEqual(call('/api/account/profile',{**body,'preferences':{**body['preferences'],'start_tab':start_tab}},cookie)[0],200)
                case.assertEqual(call('/api/account/profile',cookie=cookie)[1]['data']['preferences']['start_tab'],start_tab)
            case.assertEqual(call('/api/account/profile',cookie=cookie)[1]['data']['options']['route_sections'],[{'name':'未分区','routes':['Route A','Route B','Route C']}])
            status=call('/api/auth/status',cookie=cookie)[1];case.assertEqual(status['user']['display_name'],'My sales name');case.assertEqual(status['user']['name'],'profile-sales');case.assertIn('account',status['user']['views'])
            case.assertEqual(call('/api/operators',cookie=cookie)[0],403)
            other_cookie=call('/api/auth/login',{'name':'other-sales','password':'fixture-pass'})[2]['Set-Cookie'].split(';')[0]
            case.assertIs(call('/api/account/profile',cookie=other_cookie)[1]['data']['preferences']['growth_liquid_enabled'],True)
            case.assertIsNone(call('/api/account/profile',cookie=other_cookie)[1]['data']['preferences']['avatar_id'])
            case.assertEqual(call('/api/publishers/accounts',cookie=cookie)[0],403)
            summary=call('/api/sales/groups',cookie=cookie)
            case.assertEqual(summary[0],200,summary[1])
            case.assertEqual({row['name']:row['rates'] for row in summary[1]['data']['rows']},{'Group A':2,'Group B':1})
            preferred='?display_mode=preferred&display_routes='+urllib.parse.quote(json.dumps(['Route A']))
            summary=call('/api/sales/groups'+preferred,cookie=cookie)
            case.assertEqual(summary[0],200,summary[1])
            case.assertEqual({row['name']:row['rates'] for row in summary[1]['data']['rows']},{'Group A':1})
            query=call('/api/rates?route=Route%20C&group=Group%20B',cookie=cookie)
            case.assertEqual(query[0],200,query[1]);case.assertEqual(query[1]['total'],1)
            c=connect()
            try:
                other=c.execute('SELECT display_name,preferences_json FROM operators WHERE name=?',('other-sales',)).fetchone();case.assertFalse(other['display_name']);case.assertFalse(other['preferences_json'])
                master=manualdb.Master(c)
                query={'default_routes':json.dumps(['Route A','Route B']),'default_groups':json.dumps(['Group A']),'viewer':'profile-sales'}
                rows=manualdb.query(c,master,'rates',query)['rows'];case.assertEqual({r['route'] for r in rows},{'Route A','Route B'})
                rows=manualdb.query(c,master,'rates',{**query,'route':'Route C','group':'Group B'})['rows'];case.assertEqual(len(rows),1)
                c.execute('UPDATE operators SET routes=?,match_mode=? WHERE name=?',('Route A','all','profile-sales'));c.commit()
                rows=manualdb.query(c,master,'rates',query)['rows'];case.assertEqual({r['route'] for r in rows},{'Route A','Route B'})
                rows=manualdb.query(c,master,'rates',{**query,'route':'Route C','group':'Group B'})['rows'];case.assertEqual(len(rows),1)
            finally:c.close()
            profile=call('/api/account/profile',cookie=cookie)[1]['data'];case.assertEqual(profile['options']['routes'],['Route A','Route B','Route C']);case.assertEqual(profile['unavailable']['routes'],[])
            case.assertEqual(call('/api/account/profile',body,cookie)[0],200)
            # Name remains the login key; profile edits neither revoke sessions nor change the password.
            case.assertEqual(call('/api/auth/login',{'name':'profile-sales','password':'fixture-pass'})[0],200)
        finally:http.shutdown();thread.join(timeout=10);http.server_close()


class ProfileTests(unittest.TestCase):
    def test_grouped_avatar_files_exist(self):
        avatar_dir=Path(__file__).parent/'web'/'avatars'
        self.assertEqual(len(ACCOUNT_AVATAR_IDS),30)
        self.assertEqual(account_avatar_groups()[0]['people'][0]['looks'][0]['id'],'av2-w01-1')
        for avatar_id in ACCOUNT_AVATAR_ALL_IDS:
            self.assertTrue((avatar_dir/(avatar_id+'.png')).is_file(),avatar_id)

    def test_regions_follow_current_master_and_visible_names(self):
        from services.account_forms import account_route_sections
        rows=[{'name':'Unmapped','region':None},{'name':'West','region':'美加线'},{'name':'Europe','region':'欧地线'},{'name':'Hidden','region':'欧地线'},{'name':'East','region':'美加线'}]
        self.assertEqual(account_route_sections(rows,['Unmapped','West','Europe','East']),[
            {'name':'欧地线','routes':['Europe']},{'name':'美加线','routes':['West','East']},{'name':'未分区','routes':['Unmapped']}])

    def test_sqlite_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            verify_profile(self,lambda:manualdb.connect(Path(directory)/'profile.db'))

if __name__=='__main__':unittest.main()


