"""Isolated regressions for evidence scope and honest review metrics."""
import json
import sqlite3
import unittest
import manualdb as db


def seed(conn):
    db.invalidate_master()
    for name,kind in [('ScopeFixtureOriginZX','pol'),('ScopeFixturePortAX','pod'),('ScopeFixturePortBX','pod')]:
        conn.execute('INSERT INTO ports(name,kind,active) VALUES(?,?,1)',(name,kind))
    for code in ('AAA','BBB'):
        conn.execute('INSERT INTO carriers(code,active) VALUES(?,1)',(code,))
    conn.execute("INSERT INTO chat_groups(name) VALUES('group')")
    conn.commit()


def candidate(conn, target='ScopeFixturePortAX', scope='sender+carrier+group', key='Alice|AAA|group'):
    cur=conn.execute('INSERT INTO rule_candidates(scope_kind,scope_key,target_layer,pattern_type,alias,kind,suggested_target,reason,confidence,evidence_count,evidence_refs,source,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (scope,key,'alias_scope','alias','SHORT','pod',target,'fixture',0.9,1,'[1]','audit_correction','pending','2026-09-29'))
    conn.commit()
    return cur.lastrowid


def verify_scope(test, conn):
    seed(conn)
    cid=candidate(conn)
    test.assertTrue(db.candidates_approve(conn,cid,'tester')['ok'])
    test.assertEqual(conn.execute("SELECT COUNT(*) FROM alias_scope WHERE alias='SHORT'").fetchone()[0],0)
    master=db.Master(conn)
    base={'起运港':'ScopeFixtureOriginZX','目的港':'SHORT','船司':'AAA','来源群':'group','发送人':'Alice'}
    test.assertEqual(db.resolve_core(master,'rates',base)[0]['目的港'],'ScopeFixturePortAX')
    test.assertEqual(master.expand_cn(base)[0]['目的港'],'ScopeFixturePortAX')
    for change in ({'发送人':'Bob'},{'船司':'BBB'},{'来源群':'other'},{'发送人':''}):
        with test.subTest(change=change):
            resolved,errors=db.resolve_core(master,'rates',dict(base,**change))
            test.assertEqual(resolved['目的港'],'SHORT')
            test.assertTrue(any(e['field']=='目的港' for e in errors))
    conflict=candidate(conn,'ScopeFixturePortBX')
    test.assertFalse(db.candidates_approve(conn,conflict,'tester')['ok'])
    test.assertEqual(conn.execute('SELECT status FROM rule_candidates WHERE id=?',(conflict,)).fetchone()[0],'pending')


def verify_stats(test, conn):
    conn.execute("INSERT INTO chat_groups(name) VALUES('group')")
    for index,(review,check,edits) in enumerate([
        ('approved','passed',[]),('approved','passed',[{'action':'update','field':'目的港'}]),
        ('awaiting','passed',[]),('rejected','passed',[]),('awaiting','failed',[])]):
        conn.execute('INSERT INTO pending_records(entry_id,kind,payload,review_status,check_status,edit_log,created_at) VALUES(?,?,?,?,?,?,?)',
          ('stats-'+str(index),'rates',json.dumps({'来源群':'group'}),review,check,json.dumps(edits),'2026-09-29'))
    conn.commit()
    result=db.group_accuracy_stats(conn)['groups'][0]
    test.assertEqual(result['clean'],1)
    test.assertEqual(result['reviewed_total'],3)
    test.assertEqual(result['awaiting'],2)
    test.assertEqual(result['modified_approved'],1)
    test.assertEqual(result['mechanical_passed'],4)
    test.assertEqual(result['clean_rate'],33.3)


class ReviewMechanismTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row;db.init_schema(self.c)
    def tearDown(self):
        self.c.close();db.invalidate_master()
    def test_context_rule_does_not_expand_to_entire_group(self):verify_scope(self,self.c)
    def test_stats_require_actual_review(self):verify_stats(self,self.c)
    def test_conflicting_group_alias_never_overwrites(self):
        seed(self.c)
        self.c.execute("INSERT INTO alias_scope(alias,kind,scope_group,target) VALUES('SHORT','pod','group','ScopeFixturePortAX')");self.c.commit()
        cid=candidate(self.c,'ScopeFixturePortBX','group+column','pod|group')
        self.assertFalse(db.candidates_approve(self.c,cid,'tester')['ok'])
        self.assertEqual(self.c.execute("SELECT target FROM alias_scope WHERE alias='SHORT'").fetchone()[0],'ScopeFixturePortAX')
    def test_no_reviewed_samples_has_no_score(self):
        self.c.execute("INSERT INTO chat_groups(name) VALUES('group')")
        self.c.execute("INSERT INTO pending_records(entry_id,kind,payload,review_status,check_status,created_at) VALUES('unreviewed','rates','{\"来源群\":\"group\"}','awaiting','passed','2026-09-29')")
        self.assertIsNone(db.group_accuracy_stats(self.c)['groups'][0]['clean_rate'])


if __name__=='__main__':unittest.main()
