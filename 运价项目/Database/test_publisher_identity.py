"""Stable source ownership across rename, same-name senders, approval and snapshots."""
import json
import os
import sqlite3
import unittest
import uuid
import manualdb as db
from ops import publisher_identity as identities
from ops.admin_accounts import detail, records, security
from test_port_routes import seed, quote, LANE_A, PORT_B


def binding(conn, name, publisher):
    uid=conn.execute('SELECT user_id FROM operators WHERE name=?',(name,)).fetchone()[0]
    return identities.bind(conn,{'user_id':uid,'publisher_id':publisher,
        'expected_signature':detail(conn,uid,{})['data']['signature'],
        '_actor':'fixture-admin','_actor_id':'fixture-admin'},db.now_cn())


def verify_identity(case, conn):
    seed(conn);db.invalidate_master()
    db.save_operator(conn,{'name':'login-a','role':'operator'})
    db.save_operator(conn,{'name':'login-b','role':'operator'})
    identities.sync_confirmed(conn,[{'name':'login-a','sender_id':'dws-a','sender_open_id':'open-a'},
                                    {'name':'login-b','sender_id':'dws-b','sender_open_id':'open-b'}],db.now_cn())
    conn.commit()
    master=db.get_master(conn)
    def offer(sender_id, mid, name='同名航管'):
        record=dict(quote(LANE_A),**{'来源群':'身份测试群','来源群ID':'group-1','发送人':name,
                   '发送人ID':sender_id,'消息ID':mid})
        if sender_id=='dws-b':record['目的港']=PORT_B
        return record
    ids=db.stage_pending(conn,master,'rates',[offer('dws-a','a'),offer('dws-b','b')],preserve_other_sources=True)
    before=[dict(r) for r in conn.execute('SELECT * FROM pending_records ORDER BY id')]
    publishers=[conn.execute('SELECT publisher_id FROM pending_records WHERE id=?',(pid,)).fetchone()[0] for pid in ids]
    case.assertNotEqual(*publishers)
    case.assertEqual(db.list_pending(conn,viewer='login-a')['total'],1)
    bound=publishers[0]
    case.assertEqual(identities.canonical(conn,publishers[0]),bound)
    case.assertEqual(db.list_pending(conn,viewer='login-a')['total'],1)
    case.assertEqual(db.list_pending(conn,viewer='login-b')['total'],1)
    case.assertEqual(next(r for r in db.list_operators(conn) if r['name']=='login-a')['publisher_groups'],['身份测试群'])
    case.assertTrue(db.list_pending(conn,viewer='login-a')['rows'][0]['publisher_bound'])
    for old,new in zip(before,[dict(r) for r in conn.execute('SELECT * FROM pending_records ORDER BY id')]):
        case.assertEqual({k:v for k,v in old.items() if k!='publisher_id'}, {k:v for k,v in new.items() if k!='publisher_id'})
    with case.assertRaises(ValueError):binding(conn,'login-b',bound)
    uid=conn.execute("SELECT user_id FROM operators WHERE name='login-a'").fetchone()[0]
    account=detail(conn,uid,{})['data']
    security(conn,{'user_id':uid,'action':'rename_login','new_name':'favorite-login',
        'expected_signature':account['signature'],'_actor':'fixture-admin','_actor_id':'fixture-admin'},lambda p:p,db.now_cn())
    case.assertEqual(db.list_pending(conn,viewer='favorite-login')['total'],1)
    db.save_operator(conn,{'name':'favorite-login','password_hash':'fixture-not-a-real-password'})
    case.assertEqual(db.list_pending(conn,viewer='favorite-login')['total'],1)
    # DWS display-name changes and forged publisher IDs cannot change the binding.
    new_group=dict(offer('dws-a','a2','新的钉钉昵称'),publisher_id=publishers[1])
    new_group.update({'来源群':'另一报价群','来源群ID':'group-2'})
    more=db.stage_pending(conn,master,'rates',[new_group],preserve_other_sources=True)
    case.assertEqual(db.list_pending(conn,viewer='favorite-login')['total'],2)
    case.assertEqual(conn.execute('SELECT publisher_id FROM pending_records WHERE id=?',(more[0],)).fetchone()[0],bound)
    result=db.approve_pending(conn,master,ids[0],reviewed_by='fixture-admin')
    case.assertTrue(result['ok'],result)
    case.assertEqual(db.query(conn,master,'rates',{'viewer':'favorite-login'})['total'],1)
    case.assertEqual(records(conn,{'user_id':uid})['data']['total'],1)
    case.assertTrue(db.approve_pending(conn,master,ids[1],reviewed_by='fixture-admin')['ok'])
    case.assertEqual(db.query(conn,master,'rates',{'viewer':'login-b'})['total'],1)
    own=db.query(conn,master,'rates',{'viewer':'favorite-login'})
    case.assertEqual(own['total'],1)
    case.assertEqual(own['rows'][0]['status'],db.DEFAULT_STATUS['rates'])
    space=offer('dws-a','space');space['舱位类型']='现舱'
    sid=db.stage_pending(conn,master,'space',[space],preserve_other_sources=True)[0]
    result=db.approve_pending(conn,master,sid,reviewed_by='fixture-admin')
    case.assertTrue(result['ok'],result)
    case.assertEqual(db.query(conn,master,'space',{'viewer':'favorite-login'})['total'],1)
    snap=db.export_all(conn)
    db.load_snapshot(conn,snap)
    case.assertEqual(db.list_pending(conn,viewer='favorite-login')['total'],1)
    case.assertEqual(records(conn,{'user_id':uid})['data']['total'],2)
    case.assertEqual(conn.execute('SELECT publisher_id FROM operators WHERE user_id=?',(uid,)).fetchone()[0],bound)


class PublisherIdentityTests(unittest.TestCase):
    def test_name_scoped_exclusion_can_remove_unique_unopened_placeholder_by_user_id(self):
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        try:
            db.init_schema(conn)
            identities.resolve(conn,{'来源群':'测试群','发送人':'普通成员','发送人ID':'known-once'},db.now_cn())
            account=conn.execute("SELECT user_id FROM operators WHERE publisher_name='普通成员'").fetchone()
            self.assertIsNotNone(account)
            removed=identities.remove_unopened_placeholder_by_user_id(
                conn,account['user_id'],'管理员','admin',db.now_cn())
            self.assertEqual(removed,1)
            self.assertIsNone(conn.execute('SELECT 1 FROM operators WHERE user_id=?',(account['user_id'],)).fetchone())
        finally:conn.close()

    def test_new_sender_gets_one_disabled_identity_across_groups(self):
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        try:
            db.init_schema(conn)
            a=identities.resolve(conn,{'来源群':'甲群','发送人':'同名','发送人ID':'one','发送人OpenID':'open-one'},db.now_cn())
            b=identities.resolve(conn,{'来源群':'乙群','发送人':'改昵称','发送人OpenID':'open-one'},db.now_cn())
            c=identities.resolve(conn,{'来源群':'甲群','发送人':'同名','发送人ID':'two'},db.now_cn())
            self.assertEqual(a,b);self.assertNotEqual(a,c)
            users=[dict(r) for r in conn.execute('SELECT * FROM operators')]
            self.assertEqual(len(users),2)
            self.assertTrue(all(not r['active'] and not r['password_hash'] for r in users))
            with self.assertRaises(ValueError):
                identities.resolve(conn,{'发送人ID':'two','发送人OpenID':'open-one'},db.now_cn())
        finally:conn.close()

    def test_same_name_confirmed_people_do_not_claim_a_legacy_login(self):
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        try:
            db.init_schema(conn)
            db.save_operator(conn,{'name':'旧账号','publisher_name':'同名','role':'operator'})
            old=conn.execute("SELECT publisher_id FROM operators WHERE name='旧账号'").fetchone()[0]
            identities.sync_confirmed(conn,[{'name':'同名','sender_id':'one'},{'name':'同名','sender_id':'two'}],db.now_cn())
            ids={r[0] for r in conn.execute('SELECT publisher_id FROM publisher_dingtalk_ids')}
            self.assertEqual(len(ids),2);self.assertNotIn(old,ids)
        finally:conn.close()
    def test_sqlite_identity_lifecycle(self):
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        try:db.init_schema(conn);verify_identity(self,conn)
        finally:conn.close();db.invalidate_master()

    def test_schema_upgrade_does_not_rewrite_historical_quotes(self):
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        try:
            db.init_schema(conn)
            for login,publisher in [('old-login','历史姓名'),('same-a','重名'),('same-b','重名')]:
                db.save_operator(conn,{'name':login,'publisher_name':publisher})
            conn.execute('UPDATE operators SET publisher_id=NULL')
            for index,name in enumerate(('历史姓名','重名')):
                conn.execute('INSERT INTO pending_records(id,kind,payload,entry_id,check_status,review_status) VALUES(?,?,?,?,?,?)',
                             (index+1,'rates',json.dumps({'来源群':'旧群','发送人':name},ensure_ascii=False),'legacy-'+str(index),'passed','awaiting'))
            before=[dict(r) for r in conn.execute('SELECT * FROM pending_records ORDER BY id')]
            db.ensure_account_schema(conn)
            self.assertEqual(before,[dict(r) for r in conn.execute('SELECT * FROM pending_records ORDER BY id')])
            self.assertEqual(db.list_pending(conn,viewer='old-login')['total'],0)
            self.assertEqual(db.list_pending(conn,viewer='same-a')['total'],0)
            self.assertEqual(db.list_pending(conn,viewer='same-b')['total'],0)
            snapshot=db.export_all(conn);db.ensure_account_schema(conn)
            for table in db.PUBLISHER_TABLES+('pending_records','operators'):
                self.assertEqual(snapshot[table],db.export_all(conn)[table])
            # A new source in another group is not claimed merely because its name matches.
            pid=identities.resolve(conn,{'来源群':'新群','发送人':'历史姓名'},db.now_cn())
            self.assertNotEqual(pid,conn.execute("SELECT publisher_id FROM operators WHERE name='old-login'").fetchone()[0])
        finally:conn.close();db.invalidate_master()

    @unittest.skipUnless(os.environ.get('FREIGHT_POSTGRES_INTEGRATION_TEST')=='1','isolated PostgreSQL opt-in')
    def test_postgres_identity_lifecycle(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        import postgresdb
        from ops.postgres_sync import load_dsn
        from ops.postgres_migrate import postgres_schema
        dsn=load_dsn(postgresdb.DEFAULT_ENV_FILE)
        schema='publisher_test_'+uuid.uuid4().hex
        with psycopg.connect(dsn,autocommit=True) as admin:
            admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        conn=None
        try:
            conn=postgresdb.Connection(make_conninfo(dsn,options='-c search_path='+schema))
            for statement in postgres_schema():conn.execute(statement)
            conn.commit();db.ensure_account_schema(conn)
            verify_identity(self,conn)
        finally:
            if conn:conn.close()
            with psycopg.connect(dsn,autocommit=True) as admin:
                admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
            db.invalidate_master()

if __name__=='__main__':unittest.main()
