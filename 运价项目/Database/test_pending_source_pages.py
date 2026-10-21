"""Source pagination boundaries, using records larger than an entry page."""
import json
import sqlite3
import unittest
import manualdb

LARGE_SOURCE_ROWS = 61
SOURCE_PAGE_SIZE = 1


def verify_whole_group_pages(test, connection):
    for index in range(LARGE_SOURCE_ROWS):
        mid = 'msg//oversized==' if index % 2 else 'oversized'
        payload = json.dumps({'消息ID': mid, '消息原文':'source paging regression', '柜型':'20GP'})
        connection.execute('INSERT INTO pending_records(entry_id,kind,payload,check_status,review_status,created_at) VALUES(?,?,?,?,?,?)',
                           ('paging-large-'+str(index),'rates',payload,'failed' if index == 0 else 'passed','awaiting','2026-09-28 10:00:00'))
    connection.execute('INSERT INTO pending_records(entry_id,kind,payload,check_status,review_status,created_at) VALUES(?,?,?,?,?,?)',
                       ('paging-small','rates',json.dumps({'消息ID':'small'}),'passed','awaiting','2026-09-28 10:00:00'))
    connection.commit()
    snapshot=[dict(r) for r in connection.execute('SELECT id,payload,check_status,review_status FROM pending_records ORDER BY id')]
    pages=[manualdb.list_pending_sources(connection,limit=SOURCE_PAGE_SIZE,offset=offset) for offset in range(2)]
    test.assertEqual([p['total'] for p in pages],[2,2])
    test.assertEqual([p['record_total'] for p in pages],[LARGE_SOURCE_ROWS+1,LARGE_SOURCE_ROWS+1])
    test.assertEqual(sorted(len(p['rows']) for p in pages),[1,LARGE_SOURCE_ROWS])
    keys=[{r['source_key'] for r in p['rows']} for p in pages]
    test.assertFalse(keys[0] & keys[1])
    identities=[{r['entry_id'] for r in p['rows']} for p in pages]
    test.assertFalse(identities[0] & identities[1])
    test.assertEqual(len(identities[0] | identities[1]),LARGE_SOURCE_ROWS+1)
    failed=manualdb.list_pending_sources(connection,check='failed',limit=SOURCE_PAGE_SIZE)
    test.assertEqual(failed['total'],1)
    test.assertEqual(len(failed['rows']),LARGE_SOURCE_ROWS)
    test.assertEqual(sum(r['check_status']=='passed' for r in failed['rows']),LARGE_SOURCE_ROWS-1)
    by_id=manualdb.list_pending_sources(connection,id_query='paging-large-0',limit=SOURCE_PAGE_SIZE)
    test.assertEqual(len(by_id['rows']),LARGE_SOURCE_ROWS)
    test.assertEqual(manualdb.list_pending(connection,id_query='paging-large-0')['total'],1)
    test.assertEqual([dict(r) for r in connection.execute('SELECT id,payload,check_status,review_status FROM pending_records ORDER BY id')],snapshot)


class PendingSourcePageTests(unittest.TestCase):
    def test_whole_group_pages_sqlite(self):
        connection=sqlite3.connect(':memory:')
        connection.row_factory=sqlite3.Row
        try:
            manualdb.init_schema(connection)
            verify_whole_group_pages(self,connection)
        finally:
            connection.close()
