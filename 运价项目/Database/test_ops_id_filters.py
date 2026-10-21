"""Exact operator identity queries on isolated SQLite/PostgreSQL records."""
import json
import sqlite3
import unittest
import manualdb


def verify_ops_id_filters(test, connection):
    fixtures = [
        ('a', 'rates', 'passed', 'awaiting', 'msg//shared=='),
        ('b', 'rates', 'failed', 'awaiting', 'shared'),
        ('c', 'space', 'passed', 'approved', 'msg//shared='),
        ('d', 'rates', 'passed', 'awaiting', 'shared-extra'),
        ('e', 'rates', 'passed', 'awaiting', 42),
        ('f', 'rates', 'passed', 'awaiting', ''),
    ]
    for key, kind, check, review, mid in fixtures:
        payload = {'起运港':'Tianjin', '目的港':'Santos', '船司':'CMA', '柜型':'20GP',
                   '来源群':'ID test', '消息原文':'identity fixture '+key}
        if mid is not None:
            payload['消息ID'] = mid
        connection.execute('INSERT INTO pending_records(entry_id,kind,payload,check_status,review_status,created_at) '
                           'VALUES(?,?,?,?,?,?)', ('id-test-'+key,kind,json.dumps(payload),check,review,'2026-09-28 10:00:00'))
        connection.execute('INSERT INTO freight_items(entry_id,content_key,record_type,mid,pol,pod,carrier,container_type,status) '
                           'VALUES(?,?,?,?,?,?,?,?,?)', ('id-test-'+key,key,kind,str(mid) if mid is not None else None,
                                                     'Tianjin','Santos','CMA','20GP','当前生效'))
    connection.commit()
    baseline = [(r['entry_id'],r['payload'],r['check_status'],r['review_status']) for r in connection.execute('SELECT * FROM pending_records ORDER BY id')]
    def pending(**kw):
        return manualdb.list_pending(connection,status=kw.pop('status','all'),**kw)
    test.assertEqual({r['entry_id'] for r in pending(message_id='shared')['rows']}, {'id-test-a','id-test-b','id-test-c'})
    page = pending(message_id='shared',limit=1,offset=1)
    test.assertEqual(page['total'],3)
    test.assertEqual(len(page['rows']),1)
    test.assertEqual(pending(message_id='shared',status='awaiting',check='passed')['total'],1)
    test.assertEqual(pending(message_id='shared',kind='space')['total'],1)
    test.assertEqual(pending(entry_id='id-test-c',status='awaiting')['total'],0)
    test.assertEqual(pending(entry_id='id-test-c')['total'],1)
    test.assertEqual(pending(message_id=' 42 ')['total'],1)
    test.assertEqual(pending(message_id="shared' OR 1=1 --")['total'],0)
    test.assertEqual(pending(entry_id='id-test')['total'],0)
    test.assertEqual(pending(entry_id='id-test-a',message_id='shared-extra')['total'],0)
    for value in ('shared', 'msg//shared==', '组:msg//shared==', '消息：shared'):
        test.assertEqual({r['entry_id'] for r in pending(id_query=value)['rows']}, {'id-test-a','id-test-b','id-test-c'})
    test.assertEqual(pending(id_query='条目:id-test-a')['total'],1)
    test.assertEqual(pending(id_query='id-test-c',status='awaiting')['total'],0)
    test.assertEqual(pending(id_query='shared',check='failed')['total'],1)
    test.assertEqual(pending(id_query='shared',limit=1,offset=1)['total'],3)
    for bad in ('share', 'all', 'msg//==', "shared' OR 1=1 --"):
        test.assertEqual(pending(id_query=bad)['total'],0)
    grouped = manualdb.list_pending_grouped(connection,status='all',message_id='shared')
    test.assertTrue(grouped['groups'])
    master = manualdb.Master(connection)
    test.assertEqual(manualdb.query(connection,master,'rates',{'message_id':'shared'})['total'],2)
    test.assertEqual(manualdb.query(connection,master,'space',{'message_id':'shared'})['total'],1)
    for value in ('shared', 'msg//shared==', '组：shared'):
        test.assertEqual(manualdb.query(connection,master,'rates',{'id_query':value})['total'],2)
        test.assertEqual(manualdb.query(connection,master,'space',{'id_query':value})['total'],1)
    test.assertEqual(manualdb.query(connection,master,'rates',{'id_query':'条目:id-test-a'})['total'],1)
    test.assertEqual(manualdb.query(connection,master,'rates',{'id_query':'share'})['total'],0)
    test.assertEqual(manualdb.query(connection,master,'rates',{'id_query':'all'})['total'],0)
    test.assertEqual(manualdb.query(connection,master,'rates',{'id_query':'msg//=='})['total'],0)
    test.assertEqual(manualdb.query(connection,master,'rates',{'id_query':"shared' OR 1=1 --"})['total'],0)

    test.assertEqual(manualdb.query(connection,master,'rates',{'entry_id':'id-test-a'})['total'],1)
    test.assertEqual(manualdb.query(connection,master,'rates',{'message_id':'all'})['total'],0)
    test.assertEqual(manualdb.query(connection,master,'rates',{'message_id':"shared' OR 1=1 --"})['total'],0)
    test.assertEqual(baseline,[(r['entry_id'],r['payload'],r['check_status'],r['review_status']) for r in connection.execute('SELECT * FROM pending_records ORDER BY id')])


class OpsIdFilterTests(unittest.TestCase):
    def test_sqlite_id_filters(self):
        connection=sqlite3.connect(':memory:')
        connection.row_factory=sqlite3.Row
        try:
            manualdb.init_schema(connection)
            verify_ops_id_filters(self,connection)
        finally:
            connection.close()


if __name__ == '__main__':
    unittest.main()
