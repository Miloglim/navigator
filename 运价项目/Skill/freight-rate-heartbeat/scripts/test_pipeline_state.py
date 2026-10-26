"""Run startup must distinguish a new scope from lost ingestion state."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pipeline as p


class StartupTests(unittest.TestCase):
    def test_legacy_done_with_missing_coverage_finishes(self):
        state={'date':'1008','stage':'DONE','coverage_issues':[{'price':'missing'}]}
        p.save_state('1008',state)
        with patch.object(p,'step_pull') as pull, patch.object(p,'emit'):
            p.next_step('1008')
        pull.assert_not_called()
        current = p.load_state('1008')
        self.assertEqual(current['stage'], 'DONE')
        self.assertEqual(current['coverage_issues'], state['coverage_issues'])

    def test_incomplete_refresh_keeps_source_and_submission_state(self):
        state = {'date':'1008','stage':'COVERAGE_PENDING','new_ids':['blocked','approved'],
                 'semantic_issues':[{'message_id':'blocked'}],
                 'submit_done_records':['approved-signature'],
                 'pending_watermark':{'g':'2026-10-08 12:00:00'}}
        p.save_state('1008',state)
        with patch.object(p,'step_pull'), patch.object(p,'commit_watermark') as commit:
            p.refresh_incomplete_pull('1008',state)
        current=p.load_state('1008')
        for field in ('new_ids','semantic_issues','submit_done_records','pending_watermark'):
            self.assertEqual(current[field],state[field])
        commit.assert_not_called()

    def test_partial_commit_preserves_previous_group_receipt(self):
        state = {'date':'1008','pending_watermark':{'later':'2026-10-09 10:00:00'},
                 'pull_receipt':{'earlier':{'watermark_before':'old','watermark_after':'new',
                                          'watermark_advanced':True}, 'later':{'watermark_before':'old'}}}
        with patch.object(p.subprocess,'run'), patch.object(p,'update_committed_message_index'), patch.object(p,'emit'):
            p.commit_watermark('1008',state)
        self.assertEqual(state['pull_receipt']['earlier']['watermark_after'],'new')
        self.assertTrue(state['pull_receipt']['earlier']['watermark_advanced'])

    def test_reconciliation_only_commits_after_live_manifest_coverage(self):
        row = {'来源群':'G','消息ID':'current','海运费USD':3000}
        state = {'date':'1008','stage':'COVERAGE_PENDING','new_ids':['current'],
                 'expected_submission':[{'kind':'rates','record':row}],
                 'coverage_issues':[{'price':'obsolete'}],
                 'coverage_blocked_groups':['g'],'review_blocked_groups':['g']}
        for present in (False,True):
            import copy
            current = copy.deepcopy(state)
            rows = [{'kind':'rates','entry_id':'fixture','payload':row,
                     'review_status':'awaiting','check_status':'passed'}] if present else []
            with patch.object(p.db_client,'_request',return_value={'ok':True,'total':len(rows),'rows':rows}), \
                 patch.object(p.db_client,'validate',side_effect=lambda kind,records:
                    {'ok':True,'results':[{'ok':True,'resolved':{}} for record in records]}), \
                 patch.object(p,'commit_watermark') as commit, patch.object(p,'emit'):
                p.reconcile_pending_coverage('1008',current)
            self.assertEqual(current['stage'],'DONE' if present else 'COVERAGE_PENDING')
            self.assertEqual(commit.call_count,int(present))
            if present:
                self.assertEqual(current['coverage_issues'],[])
                self.assertEqual(len(current['resolved_coverage_history'][0]['issues']),1)

    def test_coverage_failure_cannot_leave_done_state(self):
        state = {'date':'1008', 'stage':'DONE', 'coverage_issues':[{'price':'3000'}]}
        with patch.object(p, 'emit'):
            p.done_summary('1008', state, 1)
        self.assertEqual(p.load_state('1008')['stage'], 'DONE')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for owner, key, value in ((p, "WORK", self.temp.name), (p.P, "SHARD", "A"),
                                  (p.P, "ONLY", "")):
            change = patch.object(owner, key, value)
            change.start()
            self.addCleanup(change.stop)

    def test_fresh_run_creates_readable_state_without_business_io(self):
        with patch.object(p.db_client, "submit") as submit, patch.object(p, "commit_watermark") as commit:
            result = p.start_run("1008")
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["created"])
        self.assertEqual(p.load_state("1008")["stage"], "PULL")
        self.assertNotIn("new_ids", p.load_state("1008"))
        submit.assert_not_called()
        commit.assert_not_called()

    def test_start_preserves_pending_and_done_states_exactly(self):
        for stage in ("LLM_PENDING", "AGENT_REPAIR", "DONE"):
            state = {"date": "1008", "stage": stage, "new_ids": {"group": ["source"]}}
            p.save_state("1008", state)
            before = Path(p.rdir("1008"), "state.json").read_bytes()
            result = p.start_run("1008")
            self.assertTrue(result["ok"])
            self.assertFalse(result["data"]["created"])
            self.assertEqual(Path(result["data"]["state_file"]).read_bytes(), before)

    def test_orphaned_artifacts_refuse_state_reconstruction(self):
        for relative in ("run_1008_A/raw/pull.json", "msgs_1008_A/group.json",
                         "draft_1008_A/draft.json", "llm_out_1008_A/source.json"):
            with self.subTest(relative=relative):
                artifact = Path(self.temp.name, relative)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text("{}", encoding="utf-8")
                self.assertFalse(p.start_run("1008")["ok"])
                self.assertFalse(Path(p.rdir("1008"), "state.json").exists())
                artifact.unlink()

    def test_other_shards_and_dates_do_not_become_current_scope(self):
        for name in ("run_1008_B", "msgs_1007_A"):
            artifact = Path(self.temp.name, name, "source.json")
            artifact.parent.mkdir(parents=True)
            artifact.write_text("{}", encoding="utf-8")
        self.assertTrue(p.start_run("1008")["ok"])

    def test_corrupt_or_wrong_date_state_is_preserved_and_refused(self):
        path = Path(p.rdir("1008"), "state.json")
        for content in ("{", json.dumps({"date": "1007", "stage": "PULL"}), "[]"):
            path.write_text(content, encoding="utf-8")
            self.assertFalse(p.start_run("1008")["ok"])
            self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_failed_atomic_replace_preserves_previous_state(self):
        state = {"date": "1008", "stage": "LLM_PENDING"}
        p.save_state("1008", state)
        with patch("freight_identity.runtime.os.replace", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                p.save_state("1008", {"date": "1008", "stage": "DONE"})
        self.assertEqual(p.load_state("1008"), state)


if __name__ == "__main__":
    unittest.main()
