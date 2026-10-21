"""Batch rejection scope regression; SQLite memory / PostgreSQL test schema only."""
import json
import sqlite3
import unittest

import manualdb


def verify_reject_ids(test, connection):
    ids = []
    for index, (message, port, kind, review, check) in enumerate([
        ('same', 'Santos', 'rates', 'awaiting', 'passed'),
        ('same', 'Itajai', 'rates', 'awaiting', 'failed'),
        ('other', 'Santos', 'rates', 'awaiting', 'passed'),
        ('same', 'Santos', 'rates', 'approved', 'passed'),
        ('same', 'Santos', 'space', 'awaiting', 'failed'),
    ]):
        entry = 'reject-test-' + str(index)
        connection.execute('INSERT INTO pending_records(entry_id,kind,payload,check_status,review_status,created_at) VALUES(?,?,?,?,?,?)',
                           (entry, kind, json.dumps({'消息ID': message, '目的港': port, '起运港': 'Shanghai'}),
                            check, review, '2026-09-28 16:00:00'))
        ids.append(connection.execute('SELECT id FROM pending_records WHERE entry_id=?', (entry,)).fetchone()['id'])
    connection.commit()
    def rows():
        return [dict(row) for row in connection.execute('SELECT id,payload,check_status,review_status,note,reviewed_by,edit_log FROM pending_records ORDER BY id')]
    before = rows()
    # First reproduce why a first-row group key cannot represent a message card.
    legacy_key = {'消息ID': 'same', 'pol': 'Shanghai', 'pod': 'Santos'}
    test.assertNotIn(ids[1], [pid for pid, _ in manualdb._find_group_pids(connection, legacy_key, 'rates')])
    result = manualdb.reject_pending_ids(connection, [ids[0], ids[1], ids[0], ids[3], ids[4]],
                                         note='「目的港」需核对', reviewed_by='test-reviewer')
    test.assertEqual(result['rejected'], ids[:2])
    test.assertEqual([r['id'] for r in result['skipped']], ids[3:])
    after = rows()
    for old, new in zip(before, after):
        test.assertEqual((old['payload'], old['check_status']), (new['payload'], new['check_status']))
    test.assertEqual(after[2:], before[2:], 'other sources, approved records and other kinds are unchanged')
    for row in after[:2]:
        test.assertEqual(row['review_status'], 'rejected')
        test.assertEqual(row['note'], '「目的港」需核对')
        test.assertEqual(row['reviewed_by'], 'test-reviewer')
        test.assertEqual(json.loads(row['edit_log'])[-1]['action'], 'reject_rework')
    test.assertEqual(manualdb.reject_pending_ids(connection, ids[:2])['rejected'], [])
    test.assertEqual(rows(), after, 'repeat request does not overwrite audit evidence')


def verify_pending_delete(test, connection):
    ids = {}
    for state in ('awaiting', 'rejected', 'approved'):
        cur = connection.execute('INSERT INTO pending_records(entry_id,kind,payload,check_status,review_status,created_at) VALUES(?,?,?,?,?,?)',
                                 ('delete-test-'+state,'rates','{}','failed',state,'2026-09-28'))
        ids[state] = cur.lastrowid
    connection.commit()
    approved = dict(connection.execute('SELECT * FROM pending_records WHERE id=?',(ids['approved'],)).fetchone())
    for state in ('awaiting','rejected'):
        test.assertEqual(manualdb.delete_pending(connection,ids[state]),{'ok':True,'deleted':1})
        test.assertFalse(manualdb.delete_pending(connection,ids[state])['ok'])
    test.assertFalse(manualdb.delete_pending(connection,ids['approved'])['ok'])
    test.assertEqual(dict(connection.execute('SELECT * FROM pending_records WHERE id=?',(ids['approved'],)).fetchone()),approved)


def verify_pending_delete_ids_atomic(test, connection):
    ids = []
    for entry, state in (('group-a', 'awaiting'), ('group-b', 'rejected')):
        cur = connection.execute(
            'INSERT INTO pending_records(entry_id,kind,payload,check_status,review_status,created_at) VALUES(?,?,?,?,?,?)',
            (entry, 'rates', json.dumps({'消息ID': 'group-delete'}), 'passed', state, '2026-09-28'))
        ids.append(cur.lastrowid)
    approved = connection.execute(
        'INSERT INTO pending_records(entry_id,kind,payload,check_status,review_status,created_at) VALUES(?,?,?,?,?,?)',
        ('group-c', 'rates', '{}', 'passed', 'approved', '2026-09-28')).lastrowid
    connection.commit()
    result = manualdb.delete_pending_ids(connection, ids, kind='rates')
    test.assertEqual(result, {'ok': True, 'deleted': 2, 'ids': ids})
    test.assertEqual(connection.execute('SELECT COUNT(*) FROM pending_records WHERE id IN (?,?)', ids).fetchone()[0], 0)
    blocked = manualdb.delete_pending_ids(connection, [approved, ids[0]], kind='rates')
    test.assertFalse(blocked['ok'])
    test.assertEqual(connection.execute('SELECT COUNT(*) FROM pending_records WHERE id=?', (approved,)).fetchone()[0], 1)


def verify_reject_learning(test, connection):
    def add(entry, note):
        payload = {'消息ID': entry, '来源群': '学习群', '消息原文': '原始消息 ' + entry}
        pid = connection.execute(
            'INSERT INTO pending_records(entry_id,kind,payload,check_status,review_status,created_at) VALUES(?,?,?,?,?,?)',
            (entry, 'rates', json.dumps(payload, ensure_ascii=False), 'failed', 'awaiting', '2026-09-28')).lastrowid
        connection.commit()
        return pid, manualdb.reject_pending(connection, pid, note=note, reviewed_by='reviewer')

    noise_id, noise = add('noise-message', '')
    reason_id, reason = add('bad-price-message', '「价格USD」识别错误，按原图重新抽取')
    test.assertTrue(noise['ok'] and reason['ok'])
    noise_log = json.loads(connection.execute('SELECT edit_log FROM pending_records WHERE id=?', (noise_id,)).fetchone()[0])[-1]
    reason_log = json.loads(connection.execute('SELECT edit_log FROM pending_records WHERE id=?', (reason_id,)).fetchone()[0])[-1]
    test.assertEqual(noise_log['action'], 'reject_noise')
    test.assertEqual(reason_log['action'], 'reject_rework')
    learned = connection.execute(
        'SELECT reasons FROM review_issues WHERE source_group=? AND message_id=?', ('学习群', 'noise-message')).fetchone()
    test.assertIsNotNone(learned)
    test.assertIn('垃圾条目', json.loads(learned[0])[0])
    reason_row = connection.execute(
        'SELECT reasons FROM review_issues WHERE source_group=? AND message_id=?', ('学习群', 'bad-price-message')).fetchone()
    test.assertIn('价格USD', json.loads(reason_row[0])[0])


class PendingRejectTests(unittest.TestCase):
    def test_delete_awaiting_and_rejected_but_not_approved(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        try:
            manualdb.init_schema(connection)
            verify_pending_delete(self, connection)
        finally:
            connection.close()
    def test_reject_ids_sqlite(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        try:
            manualdb.init_schema(connection)
            verify_reject_ids(self, connection)
        finally:
            connection.close()

    def test_delete_pending_ids_is_atomic(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        try:
            manualdb.init_schema(connection)
            verify_pending_delete_ids_atomic(self, connection)
        finally:
            connection.close()

    def test_reject_reason_is_optional_but_classified(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        try:
            manualdb.init_schema(connection)
            verify_reject_learning(self, connection)
        finally:
            connection.close()
