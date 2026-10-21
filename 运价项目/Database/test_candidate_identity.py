"""Port correction must not become a persistent alias across distinct ports."""
import tempfile
import json
import unittest
from pathlib import Path
import manualdb as db


class CandidateIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.c = db.connect(Path(self.temp.name) / 'test.db')
        for name in ('Izmit', 'İzmir', 'Acajutla'):
            self.c.execute('INSERT INTO ports(name,active) VALUES(?,1)', (name,))
        self.c.commit()
        self.cand = dict(scope_kind='sender+carrier+group', scope_key='sender|MSC|group',
                         alias='Izmit', kind='pod', suggested_target='İzmir',
                         target_layer='alias_scope', source='difflib_scan', evidence_refs=[1])

    def tearDown(self):
        self.c.close()
        self.temp.cleanup()

    def test_distinct_ports_blocked_across_sources_and_layers(self):
        for source in ('agent_observation', 'audit_correction', 'difflib_scan', 'manual'):
            for layer in ('alias_scope', 'master_aliases'):
                result = db.candidates_upsert(self.c, dict(self.cand, source=source, target_layer=layer))
                self.assertEqual(result['action'], 'skipped_entity_conflict')
        self.assertEqual(self.c.execute('SELECT count(*) FROM rule_candidates').fetchone()[0], 0)

    def test_real_misspelling_still_proposed(self):
        result = db.candidates_upsert(self.c, dict(self.cand, alias='Acajulta', suggested_target='Acajutla'))
        self.assertEqual(result['action'], 'created')

    def test_stale_candidate_cannot_be_approved(self):
        self.c.execute("UPDATE ports SET active=0 WHERE name='Izmit'")
        candidate = db.candidates_upsert(self.c, self.cand)
        self.c.execute("UPDATE ports SET active=1 WHERE name='Izmit'")
        self.c.commit()
        result = db.candidates_approve(self.c, candidate['id'], 'test')
        self.assertFalse(result['ok'])
        self.assertEqual(self.c.execute('SELECT count(*) FROM alias_scope').fetchone()[0], 0)
        self.assertEqual(self.c.execute('SELECT status FROM rule_candidates').fetchone()[0], 'pending')

    def test_default_suggestion_not_treated_as_alias(self):
        self.assertFalse(db._candidate_port_identity_conflict(self.c, dict(self.cand, target_layer='field_default')))

    def test_currency_group_service_terminal_and_duplicate(self):
        self.c.execute("INSERT INTO port_groups(name,active) VALUES('华南基本港',1)")
        self.c.execute("UPDATE ports SET aliases='Acajulta' WHERE name='Acajutla'")
        self.c.commit()
        for alias in ('USD', '华南基本港', 'Spainsh base ports', 'TERPORT VILLETA',
                      'PUERTO SEGURO FLUVIAL', 'CAACUPEMI ASUNCION', 'Base port via Ipanema', 'Acajulta'):
            with self.subTest(alias=alias):
                result=db.candidates_upsert(self.c, dict(self.cand, alias=alias, suggested_target='Acajutla'))
                self.assertEqual(result['action'], 'skipped_entity_conflict')

    def test_approval_snapshot_never_becomes_human_evidence(self):
        self.assertEqual(db._observe_from_diffs(self.c, {}, [dict(field='目的港',old='Acajulta',new='Acajutla')], 1), 0)
        result=db.candidates_upsert(self.c, dict(self.cand, alias='Acajulta',suggested_target='Acajutla',source='agent_observation'))
        self.assertEqual(result['action'], 'skipped_entity_conflict')

    def _seed_edits(self, mids, actor='admin', action='update', sync=False):
        self.c.execute("INSERT INTO operators(name,role,active) VALUES('admin','admin',1)")
        for index, mid in enumerate(mids):
            payload={'消息ID':mid,'来源群':'group','发送人':'sender','船司':'MSC','目的港':'Acajutla'}
            edit={'ts':f'2026-09-{10+index} 12:00:00','actor':actor,'action':action,
                  'field':'目的港','old':'Acajulta','new':'Acajutla'}
            if sync: edit['sync_from']=1
            self.c.execute('INSERT INTO pending_records(entry_id,kind,payload,edit_log) VALUES(?,?,?,?)',
                           (db.new_entry_id(),'rates',json.dumps(payload),json.dumps([edit])))
        self.c.commit()

    def test_one_message_split_across_dates_is_one_source(self):
        self._seed_edits(['m1','m1','m1'])
        self.assertEqual(db.scan_audit_corrections(self.c)['candidates_created'],0)

    def test_three_independent_human_edits_can_propose_typo(self):
        self._seed_edits(['m1','m2','m3'])
        result=db.scan_audit_corrections(self.c)
        self.assertEqual(result['candidates_created'],1)
        self.assertEqual(self.c.execute('SELECT evidence_count FROM rule_candidates').fetchone()[0],3)

    def test_agent_edits_do_not_become_human_candidates(self):
        self._seed_edits(['m1','m2','m3'],actor='agent')
        self.assertEqual(db.scan_audit_corrections(self.c)['candidates_created'],0)

    def test_approval_diffs_are_not_direct_edits(self):
        self._seed_edits(['m1','m2','m3'],action='approve_diff')
        self.assertEqual(db.scan_audit_corrections(self.c)['candidates_created'],0)

    def test_bulk_sync_is_not_independent_confirmation(self):
        self._seed_edits(['m1','m2','m3'],sync=True)
        self.assertEqual(db.scan_audit_corrections(self.c)['candidates_created'],0)


if __name__ == '__main__':
    unittest.main()
