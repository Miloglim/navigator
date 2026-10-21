import sqlite3
import unittest
from unittest.mock import patch

import manualdb
import manual_server


class AliasPaginationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE alias_scope(alias TEXT, kind TEXT, target TEXT, scope_group TEXT, evidence_count INTEGER, source TEXT, note TEXT, created_at TEXT)')
        self.db.executemany('INSERT INTO alias_scope VALUES(?,?,?,?,0,?,?,?)',
                            [('alias%03d' % i, 'pod', 'target%03d' % i, 'group', 'fixture', '', '') for i in range(123)])

    def test_pages_cover_every_row_without_overlap(self):
        pages = [manualdb.alias_scope_list(self.db, limit=50, offset=n) for n in (0, 50, 100)]
        self.assertEqual([len(p['rows']) for p in pages], [50, 50, 23])
        self.assertTrue(all(p['total'] == 123 for p in pages))
        self.assertEqual(len({r['alias'] for p in pages for r in p['rows']}), 123)

    def test_filters_count_before_limit_and_legacy(self):
        result = manualdb.alias_scope_list(self.db, q='target12', kind='pod', group='group', limit=1)
        self.assertEqual(result['total'], 3)
        self.assertEqual(result['rows'][0]['alias'], 'alias120')
        self.assertEqual(len(manualdb.alias_scope_list(self.db)), 123)
        self.assertEqual(manualdb.alias_scope_list(self.db, q='absent', limit=50)['total'], 0)
        self.assertEqual(manualdb.alias_scope_list(self.db, limit=999, offset=-1)['limit'], 200)

    def test_route_contract(self):
        class Connection:
            def __init__(self, db): self.db = db
            def execute(self, *args): return self.db.execute(*args)
            def close(self): pass  # Fixture owns the connection.
        with patch.object(manual_server, 'conn', return_value=Connection(self.db)):
            result = manual_server.act_alias_list({'limit': '50', 'offset': '50'})
            self.assertTrue(result['ok'])
            self.assertEqual(result['data']['offset'], 50)
            self.assertEqual(len(result['data']['rows']), 50)
            self.assertFalse(manual_server.act_alias_list({'limit': 'bad'})['ok'])


if __name__ == '__main__':
    unittest.main()
