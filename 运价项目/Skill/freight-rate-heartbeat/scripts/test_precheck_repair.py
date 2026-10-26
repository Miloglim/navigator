import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pipeline as p

class PrecheckRepairTests(unittest.TestCase):
    def setUp(self):
        # These suites isolate ingestion/repair; knowledge-file receipts have their own tests.
        knowledge = patch.object(p, '_ensure_knowledge', return_value=True)
        knowledge.start(); self.addCleanup(knowledge.stop)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.work=patch.object(p,'WORK',self.temp.name);self.work.start();self.addCleanup(self.work.stop)
        policy=patch.object(p,'publisher_policy_changed',return_value=False);policy.start();self.addCleanup(policy.stop)
        gate=patch.object(p,'_ensure_knowledge',return_value=True);gate.start();self.addCleanup(gate.stop)
        self.events=[];self.state={'stage':'PRECHECK','submit_queue':[],'submit_done':[]}
        self.mocks=[]
        for name,side in [('save_state',lambda d,st:None),('emit',lambda x:self.events.append(x)),('commit_watermark',lambda *a,**kw:None)]:
            m=patch.object(p,name,side_effect=side);self.mocks.append(m.start());self.addCleanup(m.stop)
        self.validate=patch.object(p.db_client,'validate');self.v=self.validate.start();self.addCleanup(self.validate.stop)
        self.submit=patch.object(p.db_client,'submit',return_value={'ok':True,'staged':1});self.s=self.submit.start();self.addCleanup(self.submit.stop)
        m=patch.object(p.db_client,'master',return_value={'ok':True,'ports':[]});m.start();self.addCleanup(m.stop)
    def item(self,mid='one',group='turkey_price',bad=False):
        return {'kind':'rates','confidence':'low','record':{'消息ID':mid,'起运港':'Qingdao','来源群':p.GNAME[group],'目的港':'Missing' if bad else 'Poti'}}
    def checks(self,flags):
        return {'ok':True,'results':[{'ok':v,'errors':[] if v else [{'field':'目的港','msg':'不在主数据'}],'resolved':{}} for v in flags]}
    def test_clean_json_one_precheck_no_middle_check_or_confirmation(self):
        self.state['submit_queue']=[self.item()];self.v.return_value=self.checks([True])
        p.advance_precheck('fixture',self.state)
        self.assertEqual(self.v.call_count,1);self.assertEqual(self.s.call_count,1)
        self.assertEqual(self.state['stage'],'DONE')
    def test_failed_row_is_diagnosed_not_submitted(self):
        self.state['submit_queue']=[self.item(bad=True)];self.v.return_value=self.checks([False])
        p.advance_precheck('fixture',self.state)
        self.s.assert_not_called();self.assertEqual(self.state['stage'],p.AGENT_REPAIR_STAGE)
        data=json.loads(Path(p._review_path('fixture')).read_text(encoding='utf-8'))
        self.assertTrue(data['issues'][0]['spec_path']);self.assertTrue(data['issues'][0]['spec_base_path'])
        self.assertTrue(Path(data['master_path']).exists())

    def test_evidence_only_row_is_submitted_and_finishes(self):
        evidence = self.item(mid='evidence')
        evidence['force_pending'] = True
        evidence['record'] = {'消息ID': 'evidence', '来源群': p.GNAME['turkey_price'],
                              '消息原文': '无法完成解析', '备注': '自动收录待补解析'}
        self.state['submit_queue'] = [evidence]
        self.v.return_value = self.checks([False])
        p.advance_precheck('fixture', self.state)
        self.assertEqual(self.s.call_count, 1)
        self.assertEqual(self.state['stage'], 'DONE')
        self.assertTrue(self.state['agent_repair_completed'])

    def test_surcharge_notice_and_quoted_image_are_not_ledger_rows(self):
        messages = {
            'reply': {'c': 'CMA 南美西墨西哥中美洲小柜超重费调整',
                      'q': {'id': 'image'}},
            'image': {'c': '[图片消息]', 'q': {}},
        }
        self.assertEqual(p._non_ledger_notice_ids(messages, messages), {'reply', 'image'})
        self.assertEqual(p.classify_message_intent(messages['reply']['c']), 'surcharge_notice')

    def test_quote_with_surcharge_note_remains_a_quote(self):
        text = 'CMA FAK运价更新 5400/5800，另有小柜超重费调整'
        self.assertEqual(p.classify_message_intent(text), 'quote')

    def test_obvious_sales_chat_is_filtered_without_business_evidence(self):
        messages = {'chat': {'c': '麻烦做一下'}}
        self.assertTrue(p._is_obvious_sales_chat(messages['chat']))
        self.assertEqual(p._non_ledger_notice_ids(messages, messages), set())

    def test_business_fact_and_quoted_image_are_not_filtered(self):
        self.assertFalse(p._is_obvious_sales_chat({'c': 'LA-6800/40H'}))
        self.assertFalse(p._is_obvious_sales_chat({'c': '这个呢', 'q': {'id': 'original'}}))
    def rebuild_after_one_repair(self, items):
        self.state['new_ids']=[x['record']['消息ID'] for x in items]
        source=Path(p.WORK)/('msgs_fixture'+p.P.sfx());source.mkdir(exist_ok=True)
        (source/'fixture.json').write_text('[]',encoding='utf-8')
        drafts=[{'record':x['record'],'_meta':{'msg_id':x['record']['消息ID'],'table':'rate','group':'turkey_price','conf':'low'}} for x in items]
        with patch.object(p.subprocess,'run'),patch.object(p,'_load_draft',side_effect=lambda d,t:drafts if t=='rate' else []),patch.object(p.llm_check,'check',return_value={'n_err':0,'n_warn':0}):
            p.step_build('fixture',self.state)
        self.assertEqual(self.state['stage'],'SUBMIT')
        p.advance_submit('fixture',self.state)

    def test_good_rows_progress_and_are_not_reinserted_after_repair(self):
        good=self.item('good','dixi_hei_price');bad=self.item('bad',bad=True)
        self.state['submit_queue']=[good,bad];self.v.return_value=self.checks([True,False])
        p.advance_precheck('fixture',self.state)
        self.assertEqual(self.s.call_args.args[1],[good['record']]);self.assertEqual(self.state['stage'],p.AGENT_REPAIR_STAGE)
        self.mocks[-1].assert_not_called()
        bad['record']['目的港']='Poti';self.rebuild_after_one_repair([good,bad])
        self.assertEqual(self.s.call_count,2);self.assertEqual(self.v.call_count,1)
        self.assertEqual(self.s.call_args.args[1],[bad['record']]);self.assertEqual(self.state['stage'],'DONE')

    def test_repair_still_invalid_goes_to_pending_once_for_human(self):
        bad=self.item(bad=True);self.state['submit_queue']=[bad];self.v.return_value=self.checks([False])
        p.advance_precheck('fixture',self.state)
        issue={'pending_id':123,'entry_id':'frt_fixture','kind':'rates','record':bad['record'],'errors':[{'field':'目的港','msg':'不在主数据'}]}
        self.s.return_value={'ok':True,'staged':1,'check_failed':1,'validation_failures':[issue]}
        self.rebuild_after_one_repair([bad])
        self.assertEqual(self.v.call_count,1);self.assertEqual(self.s.call_count,1)
        self.assertEqual(self.state['stage'],'DONE');self.assertEqual(self.state['records_check_failed'],1)
        self.assertEqual(self.state['human_review_issues'],[issue]);self.assertFalse(self.state['repair_issues'])
        p.advance_precheck('fixture',self.state)
        self.assertEqual(self.v.call_count,1);self.assertEqual(self.s.call_count,1)

    def test_transport_retry_after_repair_never_calls_validate(self):
        bad=self.item(bad=True);self.state['submit_queue']=[bad];self.v.return_value=self.checks([False])
        p.advance_precheck('fixture',self.state)
        self.s.return_value={'ok':False,'error':'temporary transport failure'};self.rebuild_after_one_repair([bad])
        self.assertEqual(self.state['stage'],'SUBMIT')
        self.s.return_value={'ok':True,'staged':1,'check_failed':1};p.advance_submit('fixture',self.state)
        self.assertEqual(self.state['stage'],'DONE');self.assertEqual(self.state['records_check_failed'],1)
        self.assertEqual(self.v.call_count,1)

    def test_auth_failure_cannot_skip_gate(self):
        self.state['submit_queue']=[self.item()];self.v.return_value={'ok':False,'code':401}
        p.advance_precheck('fixture',self.state)
        self.s.assert_not_called();self.assertEqual(self.state['stage'],'PRECHECK')
    def test_changed_record_requires_precheck(self):
        old=self.item();self.state['submit_queue']=[old];self.state['precheck_validated']=[p._record_sig('rates',old['record'])]
        old['record']['目的港']='Missing';self.v.return_value=self.checks([False])
        p.advance_submit('fixture',self.state)
        self.s.assert_not_called();self.v.assert_called_once()
    def test_postcheck_failure_stays_in_human_review_without_agent_loop(self):
        self.state['submit_queue']=[self.item()];self.v.return_value=self.checks([True])
        issue={'pending_id':123,'entry_id':'frt_fixture','kind':'rates','record':self.item()['record'],'errors':[{'field':'目的港','msg':'master changed'}]}
        self.s.return_value={'ok':True,'staged':1,'check_failed':1,'validation_failures':[issue]}
        p.advance_precheck('fixture',self.state)
        self.assertEqual(self.v.call_count,1);self.assertEqual(self.state['stage'],'DONE')
        self.assertEqual(self.state['human_review_issues'],[issue]);self.assertEqual(self.state['records_check_failed'],1)
        self.assertNotIn('postcheck_issues',self.state)

    def test_missing_manifest_item_preserves_affected_group_cursor(self):
        self.state['submit_queue']=[self.item()];self.v.return_value=self.checks([True])
        self.s.return_value={'ok':True,'staged':0,'coverage':{'ok':False,'missing':[self.item()['record']],'entries':[]}}
        p.advance_precheck('fixture',self.state)
        self.assertIn('turkey_price',self.state['review_blocked_groups'])
        self.assertEqual(len(self.state['coverage_issues']),1)
        self.assertEqual(self.state['stage'], 'DONE')

    def test_legacy_staged_failures_are_handed_to_human_without_resubmit(self):
        item=self.item();issue={'reason':'pending_postcheck','entry_id':'frt_old','kind':'rates','record':item['record']}
        self.state.update(stage=p.AGENT_REPAIR_STAGE,submit_queue=[item],submit_done_records=[p._record_sig('rates',item['record'])],precheck_validated=[],repair_issues=[issue],postcheck_issues=[issue])
        p._emit_agent_repair('fixture',self.state)
        self.assertEqual(self.state['stage'],'DONE');self.assertEqual(self.state['human_review_issues'],[issue])
        self.s.assert_not_called();self.v.assert_not_called()

if __name__=='__main__':unittest.main()
