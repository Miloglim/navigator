"""Shared visibility and derived ownership; disposable databases only."""
import json
import sqlite3
import unittest
import manualdb as db
from services.access_policy import scoped_sales_query
from test_port_routes import seed, quote, LANE_A


def verify_ownership(test, conn):
    seed(conn);db.invalidate_master()
    for name,role in [('Alice','operator'),('Bob','operator'),('Sales','sales'),('Admin','admin')]:
        db.save_operator(conn,{'name':name,'role':role})
    conn.execute("UPDATE operators SET groups='legacy-hidden',routes='legacy-route',match_mode='all' WHERE name IN ('Alice','Sales')")
    conn.commit()
    records=[dict(quote(LANE_A),**{'来源群':name+'-source-group','消息ID':name,'发送人':name}) for name in ('Alice','Bob')]
    master=db.get_master(conn)
    ids=db.stage_pending(conn,master,'rates',records,preserve_other_sources=True)
    from ops.publisher_identity import bind
    from ops.admin_accounts import detail
    for name,pid in zip(('Alice','Bob'),ids):
        uid=conn.execute('SELECT user_id FROM operators WHERE name=?',(name,)).fetchone()[0]
        publisher=conn.execute('SELECT publisher_id FROM pending_records WHERE id=?',(pid,)).fetchone()[0]
        bind(conn,{'user_id':uid,'publisher_id':publisher,'expected_signature':detail(conn,uid,{})['data']['signature'],
                   '_actor':'test','_actor_id':'test'},db.now_cn())
    before=[dict(r) for r in conn.execute('SELECT * FROM pending_records ORDER BY id')]
    test.assertEqual(db.list_pending(conn,viewer='Alice')['total'],1)
    test.assertEqual(db.list_pending(conn,viewer='Alice')['rows'][0]['payload']['发送人'],'Alice')
    test.assertEqual(db.list_pending(conn,viewer='Bob')['total'],1)
    for viewer in ('','Sales','Admin'):
        test.assertEqual(db.list_pending(conn,viewer=viewer)['total'],2)
    test.assertEqual(db.list_pending(conn,viewer='Missing')['total'],0)
    conn.execute("UPDATE operators SET display_name='Bob' WHERE name='Alice'");conn.commit()
    test.assertEqual(db.list_pending(conn,viewer='Alice')['rows'][0]['payload']['发送人'],'Alice')
    accounts={a['name']:a for a in db.list_operators(conn)}
    test.assertEqual(accounts['Alice']['routes_list'],[LANE_A])
    for retired in ('routes','groups','groups_list','match_mode'):
        test.assertNotIn(retired,accounts['Alice'])
    test.assertEqual(accounts['Sales']['routes_list'],[])
    test.assertEqual([dict(r) for r in conn.execute('SELECT * FROM pending_records ORDER BY id')],before)
    result=db.approve_all_pending(conn,master,reviewed_by='Alice',viewer='Alice')
    test.assertEqual(result['approved'],1)
    test.assertEqual(db.list_pending(conn)['rows'][0]['payload']['发送人'],'Bob')
    test.assertEqual(db.query(conn,master,'rates',{'viewer':'Sales'})['total'],1)
    result=db.approve_all_pending(conn,master,reviewed_by='Bob',viewer='Bob')
    test.assertEqual(result['approved'],1)
    mine=db.query(conn,master,'rates',{'viewer':'Alice','include_source_groups':'1'})
    test.assertEqual(mine['total'],1)
    test.assertEqual(mine['source_groups'],['Alice-source-group'])
    all_rows=db.query(conn,master,'rates',{'group':'Alice-source-group','limit':'1','include_source_groups':'1'})
    test.assertEqual(all_rows['total'],1)
    test.assertEqual(all_rows['source_groups'],['Alice-source-group','Bob-source-group'])


class AccountOwnershipTests(unittest.TestCase):
    def test_derived_ownership_and_shared_visibility(self):
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        try:db.init_schema(conn);verify_ownership(self,conn)
        finally:conn.close();db.invalidate_master()
    def test_scope_is_display_selection_not_hidden_permission(self):
        operator={'name':'Alice','role':'operator'}
        for role in ('sales','operator','admin'):
            self.assertEqual(scoped_sales_query('/api/rates',{'viewer':'someone'}, {'name':'Alice','role':role}),{})
        self.assertEqual(scoped_sales_query('/api/pending',{},operator),{'viewer':'Alice'})
        self.assertEqual(scoped_sales_query('/api/pending',{'ownership':'all'},operator),{})
        self.assertEqual(scoped_sales_query('/api/rates',{'ownership':'mine'},operator),{'viewer':'Alice'})
        self.assertEqual(scoped_sales_query('/api/rates',{'ownership':'mine','include_source_groups':'1'},operator),
                         {'include_source_groups':'1','viewer':'Alice'})
        self.assertEqual(scoped_sales_query('/api/pending',{'ownership':'mine'},{'name':'Admin','role':'admin'}),{})

if __name__=='__main__':unittest.main()
