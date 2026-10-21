"""Group-local synchronization, including forged IDs and edited source labels."""
import json
import sqlite3
import unittest

import manualdb


class PendingSyncScopeTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        manualdb.init_schema(self.conn)
        for name in ('Sync Origin', 'Sync Destination'):
            self.conn.execute('INSERT INTO ports(name,active) VALUES(?,1)', (name,))
        self.conn.execute("INSERT INTO carriers(code,active) VALUES('SYNCTEST',1)")
        self.conn.commit()
        self.master = manualdb.Master(self.conn)

    def tearDown(self):
        self.conn.close()

    def add(self, message, group='Sync Group', container='20GP', kind='rates'):
        rec = {'起运港': 'Sync Origin', '目的港': 'Sync Destination', '船司': 'SYNCTEST',
               '柜型': container, '海运费USD': '1200', '价格USD': '1200',
               '消息ID': message, '来源群': group, '消息原文': 'sync quote',
               '发送人': 'Before', '消息时间': manualdb.now_cn()}
        return manualdb.stage_pending(self.conn, self.master, kind, [rec],
                                      preserve_other_sources=bool(message),
                                      preserve_existing_source_rows=bool(message))[0]

    def test_preview_and_execution_reject_other_groups_and_kinds(self):
        anchor = self.add('msg//Same==')
        sibling = self.add('Same', container='40HQ/HC')
        other_message = self.add('Other')
        other_group = self.add('Same', group='Other Group', container='40NOR')
        other_kind = self.add('Same', kind='space')
        result = manualdb.scan_sync_targets(self.conn, '发送人', 'Before', 'After', exclude_id=anchor)
        self.assertEqual(result['ids'], [sibling])
        result = manualdb.sync_pending_field(self.conn, self.master, '发送人', 'Before', 'After',
                                            [anchor, sibling, other_message, other_group, other_kind],
                                            anchor_id=anchor, actor='test')
        self.assertEqual(result['synced'], 1)
        for pid in (anchor, other_message, other_group, other_kind):
            row = self.conn.execute('SELECT payload FROM pending_records WHERE id=?', (pid,)).fetchone()
            self.assertEqual(json.loads(row['payload'])['发送人'], 'Before')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM freight_items').fetchone()[0], 0)

    def test_edited_source_label_does_not_change_sync_identity(self):
        anchor = self.add('Source')
        sibling = self.add('Source', container='40HQ/HC')
        manualdb.update_pending(self.conn, self.master, anchor, {'来源群': 'Renamed'}, actor='test')
        result = manualdb.scan_sync_targets(self.conn, '来源群', 'Sync Group', 'Renamed', exclude_id=anchor)
        self.assertEqual(result['ids'], [sibling])

    def test_missing_anchor_and_missing_message_do_not_widen_scope(self):
        anchor = self.add('')
        self.add('', container='40HQ/HC')
        result = manualdb.scan_sync_targets(self.conn, '发送人', 'Before', 'After', exclude_id=anchor)
        self.assertEqual(result['ids'], [])
        self.assertFalse(manualdb.scan_sync_targets(self.conn, '发送人', 'Before', 'After')['ok'])
        self.assertFalse(manualdb.sync_pending_field(self.conn, self.master, '发送人',
                                                    'Before', 'After', [anchor])['ok'])


if __name__ == '__main__':
    unittest.main()
