"""Product coexistence, release, replacement and legacy fingerprints."""
import hashlib
import json
import sqlite3
import unittest

import manualdb
from core.contracts import QUOTE_VARIANT_FIELDS


def verify_quote_variants(test, conn):
    for port in ('Variant Origin', 'Variant Destination'):
        conn.execute('INSERT INTO ports(name,active) VALUES(?,1)', (port,))
    conn.execute('INSERT INTO carriers(code,active) VALUES(?,1)', ('VARTEST',))
    conn.commit()
    master = manualdb.Master(conn)
    seed = {'起运港': 'Variant Origin', '目的港': 'Variant Destination', '船司': 'VARTEST',
            '柜型': '20GP', '海运费USD': '5700', '消息ID': 'variant-test', '消息原文': 'three services',
            '消息时间': manualdb.now_cn(), '有效期起': '2026-09-30', '有效期止': '2099-10-14'}
    records = [dict(seed, 航线服务=service, 海运费USD=str(price))
               for service, price in [('Ipanema', 5700), ('Carioca', 5500), ('Santana', 4700)]]
    # Same port and price, but separate destination terminals must also coexist.
    records += [dict(seed, 目的码头=terminal) for terminal in ('Terminal A', 'Terminal B')]
    ids = manualdb.stage_pending(conn, master, 'rates', records)
    test.assertEqual(len(ids), len(records))
    test.assertEqual(manualdb.list_pending_grouped(conn, message_id=seed['消息ID'])['total'], len(records))
    # Changed ETD still only replaces the matching service in overlapping windows.
    revised = dict(records[0], 海运费USD='5750', 船期ETD='2026-10-01')
    new_id = manualdb.stage_pending(conn, master, 'rates', [revised])[0]
    pending = manualdb.list_pending(conn, message_id=seed['消息ID'])['rows']
    test.assertEqual(len(pending), len(records))
    test.assertNotIn(ids[0], [r['id'] for r in pending])
    for row in pending:
        result = manualdb.approve_pending(conn, master, row['id'])
        test.assertTrue(result['ok'], result)
    current = manualdb.query(conn, master, 'rates', {'message_id': seed['消息ID'], 'status': manualdb.DEFAULT_STATUS['rates']})
    test.assertEqual(current['total'], len(records))
    grouped = manualdb.query_grouped(conn, master, 'rates', {'message_id': seed['消息ID']})
    test.assertEqual(grouped['total'], len(records))
    test.assertEqual({g['current']['service_name'] for g in grouped['groups'] if g['current']['service_name']}, {'Ipanema', 'Carioca', 'Santana'})
    filtered = manualdb.query(conn, master, 'rates', {'service_name': 'Carioca'})
    test.assertEqual(filtered['total'], 1)
    test.assertEqual(filtered['rows'][0]['price_usd'], 5500)
    # Explicit change only covers the matching product, including reference fallback.
    changed = dict(revised, 海运费USD='5800', 消息ID='variant-next', 消息原文='changed')
    pid = manualdb.stage_pending(conn, master, 'rates', [changed])[0]
    test.assertTrue(manualdb.approve_pending(conn, master, pid)['ok'])
    test.assertEqual(manualdb.query(conn, master, 'rates', {'carrier': 'VARTEST', 'status': manualdb.DEFAULT_STATUS['rates']})['total'], len(records))
    test.assertIsNone(manualdb.find_quoted_target(conn, 'rates', seed['消息ID'], None, '20GP', dict(service_name='Other')))
    target = manualdb.find_quoted_target(conn, 'rates', seed['消息ID'], None, '20GP', dict(service_name='Ipanema'))
    row = conn.execute('SELECT service_name,price_usd FROM freight_items WHERE content_key=?', (target,)).fetchone()
    test.assertEqual((row['service_name'], row['price_usd']), ('Ipanema', 5800))
    test.assertEqual(manualdb.query(conn, master, 'rates', {'destination_terminal': 'Terminal B'})['total'], 1)
    # New optional columns must not change historical content keys when absent.
    normalized = manualdb.normalize(seed, 'rates', manualdb.now_cn(), 'agent')
    old_keys = [k for k in manualdb.TABLES['rates']['key'] if k not in QUOTE_VARIANT_FIELDS.values()]
    basis = '\x1f'.join(manualdb.canon_for_key(str(normalized.get(k) or '')) for k in old_keys)
    test.assertEqual(normalized['content_key'], hashlib.sha256(basis.encode()).hexdigest())
    test.assertNotEqual(normalized['content_key'], manualdb.normalize(records[0], 'rates', manualdb.now_cn(), 'agent')['content_key'])


class QuoteVariantsTests(unittest.TestCase):
    def test_disjoint_weekly_rates_coexist_in_pending_and_formal(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        try:
            manualdb.init_schema(conn)
            for port in ('Week Origin', 'Week Destination'):
                conn.execute('INSERT INTO ports(name,active) VALUES(?,1)', (port,))
            conn.execute('INSERT INTO carriers(code,active) VALUES(?,1)', ('WEEKTEST',))
            conn.commit()
            master = manualdb.Master(conn)
            base = {'起运港': 'Week Origin', '目的港': 'Week Destination', '船司': 'WEEKTEST',
                    '柜型': '20GP', '消息ID': 'two-week-cards', '消息原文': '41 week 42 week',
                    '消息时间': '2026-09-28 15:16', '来源群': 'Week Test'}
            week41 = dict(base, 海运费USD=3030, 有效期船期='41周',
                          有效期起='2026-10-05', 有效期止='2026-10-11')
            week42 = dict(base, 海运费USD=2730, 有效期船期='42周',
                          有效期起='2026-10-12', 有效期止='2026-10-18')
            ids = manualdb.stage_pending(conn, master, 'rates', [week41, week42],
                                         auto_suggest=False, preserve_other_sources=True)
            self.assertEqual(len(ids), 2)
            for pid in ids:
                self.assertTrue(manualdb.approve_pending(conn, master, pid)['ok'])
            current = conn.execute("SELECT valid_from,valid_to,price_usd FROM freight_items "
                                   "WHERE carrier='WEEKTEST' AND status=? ORDER BY valid_from",
                                   (manualdb.DEFAULT_STATUS['rates'],)).fetchall()
            self.assertEqual([(r['valid_from'], r['valid_to'], r['price_usd']) for r in current],
                             [('2026-10-05', '2026-10-11', 3030),
                              ('2026-10-12', '2026-10-18', 2730)])
        finally:
            conn.close()

    def test_grouped_keeps_distinct_validity_windows(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        try:
            manualdb.init_schema(conn)
            conn.execute("INSERT INTO ports(name,active) VALUES('Validity Origin',1)")
            conn.execute("INSERT INTO ports(name,active) VALUES('Validity Destination',1)")
            conn.execute("INSERT INTO carriers(code,active) VALUES('VALIDITYTEST',1)")
            conn.commit()
            master = manualdb.Master(conn)
            base = {'起运港': 'Validity Origin', '目的港': 'Validity Destination', '船司': 'VALIDITYTEST',
                    '柜型': '20GP', '消息ID': 'validity-windows', '消息原文': 'two image validity windows',
                    '消息时间': '2026-10-09 10:00', '来源群': 'Validity Test'}
            records = [
                dict(base, 海运费USD=3300, 有效期船期='10.01-10.31', 有效期起='2026-10-01', 有效期止='2026-10-31'),
                dict(base, 消息ID='validity-windows-2', 海运费USD=3700, 有效期船期='10.19-10.31', 有效期起='2026-10-19', 有效期止='2026-10-31'),
            ]
            for index, record in enumerate(records):
                conn.execute(
                    "INSERT INTO freight_items(entry_id,content_key,record_type,pol,pod,carrier,container_type,price_usd,validity_raw,valid_from,valid_to,message_time,mid,status,origin) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ('validity-entry-' + str(index), 'validity-key-' + str(index), 'rates',
                     record['起运港'], record['目的港'], record['船司'], record['柜型'], record['海运费USD'],
                     record['有效期船期'], record['有效期起'], record['有效期止'], record['消息时间'], record['消息ID'],
                     manualdb.DEFAULT_STATUS['rates'], 'agent'))
            conn.commit()
            manualdb._GROUPED_CACHE.clear()
            grouped = manualdb.query_grouped(conn, master, 'rates', {})
            self.assertEqual(grouped['total'], 2)
            windows = {(g['current']['valid_from'], g['current']['valid_to']) for g in grouped['groups']}
            self.assertEqual(windows, {('2026-10-01', '2026-10-31'), ('2026-10-19', '2026-10-31')})
        finally:
            conn.close()

    def test_sqlite_products(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        try:
            manualdb.init_schema(conn)
            verify_quote_variants(self, conn)
        finally:
            conn.close()
