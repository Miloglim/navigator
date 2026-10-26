"""Account growth contract on disposable databases and real HTTP sessions."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import manualdb
import manual_server as server
from ops import account_growth
from core.growth_contracts import (GROWTH_APPROVAL_EVENT, GROWTH_APPROVAL_POINTS,
                                   GROWTH_CORRECTION_EVENT, GROWTH_CORRECTION_POINTS,
                                   GROWTH_DAILY_ACTIVITY_POINTS,
                                   GROWTH_REVIEW_EVENT,GROWTH_RESET_EVENT,
                                   GROWTH_SYNC_TABLE)
from services.account_growth import GROWTH_XP_PER_CYCLE, map_legacy_xp, summary
from transport.access_rules import authorize


def verify_growth(case, connect):
    connection = connect()
    try:
        connection.execute('DELETE FROM account_experience_events')
        connection.execute('DELETE FROM account_growth_sync_outbox')
        connection.execute('DELETE FROM auth_sessions')
        connection.execute('DELETE FROM operators')
        connection.commit()
        for name in ('growth-sales', 'growth-other'):
            manualdb.save_operator(connection, {'name': name, 'role': 'sales',
                                                'password_hash': server.password_hash('fixture-pass')})
    finally:
        connection.close()
    with patch.object(server, 'conn', side_effect=connect):
        http = server.FreightHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=http.serve_forever)
        thread.start()
        base = 'http://127.0.0.1:%d' % http.server_port

        def call(path, body=None, cookie='', headers=None):
            request = urllib.request.Request(base + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers={'Content-Type': 'application/json', 'Cookie': cookie, **(headers or {})})
            try:
                result = urllib.request.urlopen(request, timeout=15)
            except urllib.error.HTTPError as error:
                result = error
            with result:
                return result.status, json.load(result), result.headers

        try:
            case.assertEqual(call('/api/account/growth')[0], 401)
            case.assertEqual(call('/api/account/growth/search', {})[0], 401)
            case.assertEqual(authorize('GET', '/api/account/growth', agent=True)['code'], 'forbidden')
            case.assertEqual(authorize('POST', '/api/account/growth/search', agent=True)['code'], 'forbidden')
            cookie = call('/api/auth/login', {'name': 'growth-sales', 'password': 'fixture-pass'})[2]['Set-Cookie'].split(';')[0]
            other = call('/api/auth/login', {'name': 'growth-other', 'password': 'fixture-pass'})[2]['Set-Cookie'].split(';')[0]
            connection = connect()
            try:
                growth_user_id = connection.execute("SELECT user_id FROM operators WHERE name='growth-sales'").fetchone()[0]
            finally:
                connection.close()
            with patch.dict(server.CFG, {'center_auth_url': 'https://center.example'}, clear=False), \
                    patch.object(server.Handler, '_session', return_value={'name': 'growth-sales', 'user_id': growth_user_id, 'role': 'sales'}), \
                    patch.object(server.Handler, '_center_profile', return_value={'growth': None}), \
                    patch.object(server.Handler, '_sync_growth_center'):
                fallback = call('/api/account/growth', cookie=cookie)[1]
            case.assertEqual(fallback['data']['summary']['total_xp'], 0)
            case.assertEqual(call('/api/account/growth?limit=0', cookie=cookie)[0], 400)
            case.assertEqual(call('/api/account/growth/search', {'user_id': 'someone-else'}, cookie)[0], 400)
            case.assertEqual(call('/api/account/growth/search', {}, cookie,
                                  {'Origin': 'https://external.invalid'})[0], 403)
            case.assertEqual(call('/api/account/growth', cookie=cookie)[1]['data']['summary']['total_xp'], 0)
            with ThreadPoolExecutor(max_workers=5) as executor:
                responses = list(executor.map(lambda _: call('/api/account/growth/search', {}, cookie), range(5)))
            case.assertEqual([status for status, _, _ in responses], [200] * 5)
            case.assertEqual(sum(int(result['data']['awarded']) for _, result, _ in responses), 1)
            case.assertEqual(call('/api/account/growth', cookie=cookie)[1]['data']['summary']['total_xp'], 18)
            case.assertEqual(call('/api/account/growth', cookie=other)[1]['data']['summary']['total_xp'], 0)
            case.assertEqual(call('/api/account/growth/search', {}, cookie)[1]['data']['awarded'], False)
            connection = connect()
            try:
                case.assertEqual(connection.execute('SELECT COUNT(*) FROM account_experience_events').fetchone()[0], 1)
                case.assertEqual(connection.execute("SELECT role FROM operators WHERE name='growth-sales'").fetchone()[0], 'sales')
            finally:
                connection.close()
        finally:
            http.shutdown()
            thread.join(timeout=10)
            http.server_close()


class GrowthTests(unittest.TestCase):
    def test_reset_debit_does_not_reopen_daily_allowance(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = manualdb.connect(Path(directory) / 'growth.db')
            try:
                connection.execute("INSERT INTO operators(user_id,name,role,active) VALUES('usr_reset','reset','admin',1)")
                stamp = '2026-10-09T09:00:00+08:00'
                account_growth._record(connection,'usr_reset',GROWTH_REVIEW_EVENT,'2026-10-09',18,stamp)
                connection.execute(
                    'INSERT INTO account_experience_events(event_id,user_id,event_type,subject_key,points,rule_version,created_at) VALUES(?,?,?,?,?,?,?)',
                    ('xp_reset','usr_reset',GROWTH_RESET_EVENT,'reset',18,2,stamp))
                state = account_growth._read(connection,'usr_reset','2026-10-09',10,0)
                self.assertEqual(state['summary']['total_xp'],0)
                self.assertEqual(state['today_xp'],18)
                result = account_growth._record(connection,'usr_reset','other_daily_activity','2026-10-09',18,stamp)
                self.assertEqual(result['points'],12)
                self.assertEqual(account_growth._read(connection,'usr_reset','2026-10-09',10,0)['today_xp'],30)
            finally:
                connection.close()

    def test_projection_and_day_rollover(self):
        self.assertEqual(GROWTH_XP_PER_CYCLE, 1026)
        self.assertEqual(summary(11, False)['level'], 1)
        self.assertEqual(summary(12, True)['level'], 2)
        self.assertEqual(summary(GROWTH_XP_PER_CYCLE, False)['cycle'], 2)
        self.assertEqual(summary(GROWTH_XP_PER_CYCLE, False)['level'], 1)
        legacy_admin = summary(map_legacy_xp(3930), False)
        self.assertEqual((legacy_admin['cycle'], legacy_admin['level']), (8, 44))
        with tempfile.TemporaryDirectory() as directory:
            connect = lambda: manualdb.connect(Path(directory) / 'growth.db')
            c = connect()
            try:
                c.execute("INSERT INTO operators(user_id,name,role,active) VALUES('usr_growth','growth','sales',1)")
                c.commit()
            finally:
                c.close()
            session = {'user_id': 'usr_growth'}
            self.assertTrue(account_growth.award_search(connect, session, {}, lambda: '2026-09-30')['data']['awarded'])
            self.assertFalse(account_growth.award_search(connect, session, {}, lambda: '2026-09-30')['data']['awarded'])
            self.assertTrue(account_growth.award_search(connect, session, {}, lambda: '2026-10-01')['data']['awarded'])
            self.assertEqual(account_growth.read(connect, session, {}, lambda: '2026-10-01')['data']['summary']['total_xp'], 36)

    def test_verified_work_is_idempotent_and_daily_capped(self):
        with tempfile.TemporaryDirectory() as directory:
            connect = lambda: manualdb.connect(Path(directory) / 'growth.db')
            connection = connect()
            try:
                connection.execute("INSERT INTO operators(user_id,name,role,active) VALUES('usr_growth','growth','operator',1)")
                first = account_growth.award_business_activity(connection, 'usr_growth', GROWTH_REVIEW_EVENT, [
                    (GROWTH_APPROVAL_EVENT, 'pending:1', GROWTH_APPROVAL_POINTS),
                    (GROWTH_CORRECTION_EVENT, 'pending:2', GROWTH_CORRECTION_POINTS),
                ], '2026-09-30T09:00:00+08:00')
                self.assertEqual(first['awarded_points'], 26)
                self.assertTrue(first['level_up'])
                second = account_growth.award_business_activity(connection, 'usr_growth', GROWTH_REVIEW_EVENT, [
                    (GROWTH_APPROVAL_EVENT, 'pending:1', GROWTH_APPROVAL_POINTS),
                    (GROWTH_CORRECTION_EVENT, 'pending:3', GROWTH_CORRECTION_POINTS),
                ], '2026-09-30T10:00:00+08:00')
                self.assertEqual(second['awarded_points'], 6)
                self.assertEqual(second['today_xp'], 32)
                self.assertEqual(second['daily_cap'], 30)
                self.assertEqual(connection.execute(
                    'SELECT SUM(points) FROM account_experience_events WHERE user_id=?', ('usr_growth',)).fetchone()[0], 32)
            finally:
                connection.close()

    def test_center_growth_outbox_is_durable_and_marks_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = manualdb.connect(Path(directory) / 'growth.db')
            try:
                connection.execute("INSERT INTO operators(user_id,name,role,active) VALUES('usr_sync','sync','sales',1)")
                account_growth.award_business_activity(
                    connection, 'usr_sync', GROWTH_REVIEW_EVENT,
                    [(GROWTH_APPROVAL_EVENT, 'pending:sync', GROWTH_APPROVAL_POINTS)],
                    '2026-09-30T09:00:00+08:00')
                pending = connection.execute(
                    f"SELECT event_id,status,attempts FROM {GROWTH_SYNC_TABLE}").fetchall()
                self.assertEqual(len(pending), 2)
                self.assertTrue(all(row['status'] == 'pending' and row['attempts'] == 0 for row in pending))
                with patch.object(account_growth.center_auth, 'record_growth_event', return_value={'awarded': True}):
                    self.assertEqual(account_growth.sync_center(connection, 'http://center', 'token', 'usr_sync'), 2)
                sent = connection.execute(
                    f"SELECT status,attempts,sent_at FROM {GROWTH_SYNC_TABLE} ORDER BY event_id").fetchall()
                self.assertTrue(all(row['status'] == 'sent' and row['attempts'] == 1 and row['sent_at'] for row in sent))
            finally:
                connection.close()

    def test_bulk_uncapped_approval_events_are_batched_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = manualdb.connect(Path(directory) / 'growth.db')
            try:
                connection.execute("INSERT INTO operators(user_id,name,role,active) VALUES('usr_bulk','bulk','operator',1)")
                subjects = [(GROWTH_APPROVAL_EVENT, f'pending:{index}', GROWTH_APPROVAL_POINTS)
                            for index in range(250)]
                first = account_growth.award_business_activity(
                    connection, 'usr_bulk', GROWTH_REVIEW_EVENT, subjects,
                    '2026-09-30T09:00:00+08:00')
                expected = GROWTH_DAILY_ACTIVITY_POINTS + 250 * GROWTH_APPROVAL_POINTS
                self.assertEqual(first['awarded_points'], expected)
                second = account_growth.award_business_activity(
                    connection, 'usr_bulk', GROWTH_REVIEW_EVENT, subjects,
                    '2026-09-30T10:00:00+08:00')
                self.assertEqual(second['awarded_points'], 0)
                count = connection.execute(
                    'SELECT COUNT(*) FROM account_experience_events WHERE user_id=?',
                    ('usr_bulk',)).fetchone()[0]
                self.assertEqual(count, 251)
            finally:
                connection.close()

    def test_center_growth_outbox_keeps_event_pending_on_center_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = manualdb.connect(Path(directory) / 'growth.db')
            try:
                connection.execute("INSERT INTO operators(user_id,name,role,active) VALUES('usr_sync_fail','sync','sales',1)")
                account_growth.award_business_activity(
                    connection, 'usr_sync_fail', GROWTH_REVIEW_EVENT,
                    [(GROWTH_APPROVAL_EVENT, 'pending:fail', GROWTH_APPROVAL_POINTS)],
                    '2026-09-30T09:00:00+08:00')
                with patch.object(account_growth.center_auth, 'record_growth_event',
                                  side_effect=account_growth.center_auth.CenterAuthError('offline')):
                    self.assertEqual(account_growth.sync_center(connection, 'http://center', 'token', 'usr_sync_fail'), 0)
                pending = connection.execute(
                    f"SELECT status,attempts,last_error FROM {GROWTH_SYNC_TABLE} ORDER BY event_id").fetchall()
                self.assertTrue(all(row['status'] == 'pending' for row in pending))
                self.assertEqual(sorted(row['attempts'] for row in pending), [0, 1])
                self.assertEqual([row['last_error'] for row in pending if row['attempts'] == 1], ['offline'])
            finally:
                connection.close()

    def test_sqlite_http(self):
        with tempfile.TemporaryDirectory() as directory:
            verify_growth(self, lambda: manualdb.connect(Path(directory) / 'growth.db'))

    def test_verified_business_response_carries_growth_feedback(self):
        with tempfile.TemporaryDirectory() as directory:
            connect = lambda: manualdb.connect(Path(directory) / 'growth.db')
            connection = connect()
            try:
                connection.execute(
                    "INSERT INTO operators(user_id,name,role,active) VALUES('usr_ops','ops-growth','operator',1)")
                cursor = connection.execute(
                    "INSERT INTO pending_records(entry_id,kind,payload,check_status,review_status,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    ('growth-reject', 'rates', '{}', 'failed', 'awaiting', '2026-09-30 09:00:00'))
                pending_id = cursor.lastrowid
                connection.commit()
            finally:
                connection.close()
            with patch.object(server, 'conn', side_effect=connect):
                result = server.act_pending_reject({
                    'id': pending_id, 'note': '字段需复核', '_actor': 'ops-growth',
                    '_actor_id': 'usr_ops'})
            self.assertTrue(result['ok'])
            self.assertEqual(result['growth_award']['awarded_points'], 18)
            self.assertEqual(result['growth_award']['today_xp'], 18)
            self.assertIn('summary', result['growth_award'])

    def test_snapshot_includes_growth_and_accepts_older_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = manualdb.connect(Path(directory) / 'growth.db')
            try:
                snapshot = manualdb.export_all(connection)
                self.assertIn('account_experience_events', snapshot)
                snapshot.pop('account_experience_events')
                self.assertIsInstance(manualdb.load_snapshot(connection, snapshot), str)
            finally:
                connection.close()


if __name__ == '__main__':
    unittest.main()
