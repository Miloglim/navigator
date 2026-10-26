"""Publisher management and collection agree through an isolated roster."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from freight_identity import runtime as r
from freight_identity.services.publisher import assess_group
from freight_identity.services.collection import collection_decision
import pipeline as p
import record_builder as builder

GROUPS = [{'slug':'fixture','cid':'cid1','name':'测试来源群'}]
GROUPS_TWO = GROUPS + [{'slug':'fixture_other','cid':'cid2','name':'另一个来源群'}]
AS_OF = '2026-09-28T16:00:00+08:00'
MESSAGE = {'id':'m1','s':'航管甲','senderId':'sid1','senderOpenDingTalkId':'oid1',
           't':AS_OF,'c':'@所有人 报价更新','img':['media1']}


class PublisherSyncTests(unittest.TestCase):
    def setUp(self):
        # These suites isolate ingestion/repair; knowledge-file receipts have their own tests.
        knowledge = patch.object(p, '_ensure_knowledge', return_value=True)
        knowledge.start(); self.addCleanup(knowledge.stop)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.work=Path(self.tmp.name)/'work';self.work.mkdir()
        self.roster=Path(self.tmp.name)/'roster.json'
        r.atomic_json(self.roster, {'version':'1.1.0','revision':0,'members':[],'audit':[]})
        folder=self.work/'msgs_fixture';folder.mkdir()
        r.atomic_json(folder/'fixture.json',[MESSAGE])
    def snapshot(self,*args,**kwargs):
        return r.snapshot(self.work,groups=GROUPS,roster_path=self.roster,as_of=AS_OF)
    def candidate(self):
        return self.snapshot()['data']['candidates'][0]
    def save(self,action,revision=0):
        return r.save_decision({'action':action,'expected_revision':revision,'note':'隔离测试依据'},
                              self.candidate(),'管理员测试','usr_fixture',self.roster)
    def test_confirm_exclude_reset_same_authority_and_history(self):
        self.assertTrue(self.save('confirm')['ok'])
        snapshot=self.snapshot();candidate=snapshot['data']['candidates'][0]
        self.assertEqual(candidate['assessment']['basis'],'user_confirmed_account')
        self.assertEqual(r.decision_for(snapshot,'fixture',MESSAGE)['status'],'publisher')
        self.assertTrue(self.save('exclude',1)['ok'])
        snapshot=self.snapshot()
        self.assertEqual(snapshot['data']['candidates'][0]['assessment']['basis'],'user_excluded_account')
        self.assertEqual(r.decision_for(snapshot,'fixture',MESSAGE)['action'],'exclude')
        self.assertTrue(self.save('reset',2)['ok'])
        self.assertEqual(r.decision_for(self.snapshot(),'fixture',MESSAGE)['action'],'parse')
        roster=r.load_roster(self.roster)
        self.assertEqual(roster['revision'],3);self.assertEqual(len(roster['audit']),3)
        self.assertEqual(roster['audit'][1]['before'][0]['decision'],'publisher')
        self.assertEqual(roster['audit'][1]['after']['decision'],'excluded')
    def test_same_name_history_is_one_candidate_and_account_decision(self):
        history = {'id':'old','s':'航管甲','t':AS_OF,'c':'@所有人 历史价'}
        r.atomic_json(self.work/'msgs_fixture'/'fixture.json',[MESSAGE,history])
        result=self.snapshot()
        self.assertEqual(len(result['data']['candidates']),1)
        candidate=result['data']['candidates'][0]
        self.assertEqual(candidate['assessment']['message_count'],2)
        self.assertEqual(candidate['assessment']['historical_message_count'],1)
        self.assertEqual(candidate['sender_open_id'],'oid1')
        self.assertEqual(r.decision_for(result,'fixture',MESSAGE)['status'],'publisher')
        self.assertEqual(r.decision_for(result,'fixture',history)['action'],'parse')
        self.assertFalse(r.decision_for(result,'fixture',history)['identity_ready'])
        self.save('exclude')
        excluded=self.snapshot()
        self.assertEqual(r.decision_for(excluded,'fixture',MESSAGE)['action'],'exclude')
        self.assertEqual(r.decision_for(excluded,'fixture',history)['action'],'exclude')

    def test_managed_account_absorbs_same_name_history_without_account_message(self):
        self.save('confirm')
        history={'id':'old','s':'航管甲','t':AS_OF,'c':'@所有人 历史价'}
        r.atomic_json(self.work/'msgs_fixture'/'fixture.json',[history])
        result=self.snapshot()
        self.assertEqual(len(result['data']['candidates']),1)
        candidate=result['data']['candidates'][0]
        self.assertEqual(candidate['sender_open_id'],'oid1')
        self.assertEqual(candidate['assessment']['message_count'],1)
        self.assertEqual(candidate['assessment']['historical_message_count'],1)
        self.assertEqual(r.decision_for(result,'fixture',history)['action'],'parse')

    def test_stale_write_cannot_overwrite_newer_confirmation(self):
        self.save('confirm')
        result=self.save('exclude')
        self.assertEqual(result['code'],'revision_conflict')
        self.assertEqual(r.load_roster(self.roster)['members'][0]['decision'],'publisher')
    def test_stable_account_can_be_confirmed_when_rule_evidence_is_insufficient(self):
        candidate=self.candidate();candidate['assessment']['identity_ready']=False
        candidate['assessment']['status']='insufficient'
        result=r.save_decision({'action':'confirm','expected_revision':0,'note':'管理员核实'},candidate,'admin','id',self.roster)
        self.assertTrue(result['ok'],result)
        self.assertEqual(r.load_roster(self.roster)['members'][0]['decision'],'publisher')
    def test_name_only_can_be_excluded_in_current_group_without_affecting_other_groups(self):
        name_message={'id':'name-1','s':'明显不是航管','t':AS_OF,'c':'普通询价'}
        r.atomic_json(self.work/'msgs_fixture/fixture.json',[name_message])
        other_folder=self.work/'msgs_fixture_other';other_folder.mkdir()
        other_message=dict(name_message,id='name-2')
        r.atomic_json(other_folder/'fixture_other.json',[other_message])
        candidate=self.candidate()

        result=r.save_decision({'action':'exclude','expected_revision':0,'note':'管理员排除'},
                               candidate,'admin','id',self.roster)

        self.assertTrue(result['ok'],result)
        roster=r.load_roster(self.roster)
        self.assertEqual(roster['exclusions'],[])
        self.assertEqual(roster['members'][0]['scope'],'group')
        self.assertEqual(roster['members'][0]['decision'],'excluded')
        snapshot=r.snapshot(self.work,groups=GROUPS_TWO,roster_path=self.roster,as_of=AS_OF)
        fixture=next(c for c in snapshot['data']['candidates'] if c['cid']=='cid1')
        other=next(c for c in snapshot['data']['candidates'] if c['cid']=='cid2')
        self.assertEqual(fixture['assessment']['basis'],'user_excluded_name')
        self.assertEqual(r.decision_for(snapshot,'fixture',name_message)['action'],'exclude')
        self.assertNotEqual(other['assessment']['status'],'excluded')
        self.assertEqual(r.decision_for(snapshot,'fixture_other',other_message)['action'],'exclude')
        self.assertEqual(len(snapshot['data']['exclusions']),1)
        self.assertEqual(snapshot['data']['exclusions'][0]['scope'],'group')
        r.atomic_json(self.work/'msgs_fixture/fixture.json',[])
        without_recent=r.snapshot(self.work,groups=GROUPS,roster_path=self.roster,as_of=AS_OF)
        retained=without_recent['data']['candidates'][0]
        self.assertEqual(retained['assessment']['basis'],'user_excluded_name')
        self.assertFalse(retained['assessment']['identity_ready'])

    def test_name_only_still_cannot_confirm_publisher(self):
        candidate=self.candidate();candidate.update(identity_key='name:航管甲',sender_open_id='',sender_id='')
        candidate['assessment']['identity_ready']=False
        result=r.save_decision({'action':'confirm','expected_revision':0,'note':'test'},candidate,'admin','id',self.roster)
        self.assertEqual(result['code'],'identity_required')
    def test_repeated_confirmation_is_idempotent(self):
        first=self.save('confirm')
        self.assertTrue(first['ok'],first)
        repeated=self.save('confirm',1)
        self.assertTrue(repeated['ok'],repeated)
        self.assertTrue(repeated['data']['unchanged'])
        roster=r.load_roster(self.roster)
        self.assertEqual(roster['revision'],1)
        self.assertEqual(len(roster['audit']),1)
    def test_identity_conflict_cannot_be_managed_or_filtered(self):
        original=dict(MESSAGE,senderOpenDingTalkId='other')
        raw=self.work/'run_fixture/raw';raw.mkdir(parents=True)
        r.atomic_json(raw/'pull_fixture.json',[original])
        candidate=self.candidate()
        self.assertTrue(candidate['identity_conflict'])
        self.assertFalse(candidate['assessment']['identity_ready'])
        self.assertEqual(r.decision_for(self.snapshot(),'fixture',MESSAGE)['action'],'exclude')
        self.assertEqual(self.save('exclude')['code'],'identity_required')
    def test_unknown_is_suppressed_but_kept_in_shadow_review(self):
        r.atomic_json(self.work/'msgs_fixture/fixture.json',[dict(MESSAGE,c='能帮忙查价吗')])
        decision=r.decision_for(self.snapshot(),'fixture',MESSAGE)
        self.assertEqual(decision['action'],'exclude');self.assertEqual(decision['shadow_action'],'identity_review')
    def test_exclusion_is_global_person_scoped_overrides_mention_all(self):
        self.save('exclude')
        roster=r.load_roster(self.roster)
        decisions=roster['members'] + roster['exclusions']
        message=r.normalize_message(MESSAGE)
        self.assertEqual(assess_group([message],as_of=AS_OF,group_id='cid1',confirmations=decisions)['data']['members'][0]['status'],'excluded')
        self.assertEqual(assess_group([message],as_of=AS_OF,group_id='other',confirmations=decisions)['data']['members'][0]['status'],'excluded')
        other=dict(message,sender_open_id='oid-other',sender_id='sid-other')
        self.assertEqual(assess_group([other],as_of=AS_OF,group_id='cid1',confirmations=decisions)['data']['members'][0]['status'],'publisher')

    def test_one_exclusion_covers_other_and_future_groups_and_has_one_list_entry(self):
        other_folder=self.work/'msgs_fixture_other';other_folder.mkdir()
        other_message=dict(MESSAGE,id='m2',t=AS_OF)
        r.atomic_json(other_folder/'fixture_other.json',[other_message])
        self.save('exclude')

        result=r.snapshot(self.work,groups=GROUPS_TWO,roster_path=self.roster,as_of=AS_OF)
        candidates=[candidate for candidate in result['data']['candidates']
                    if candidate['identity_key']=='open:oid1']
        self.assertEqual(len(candidates),2)
        self.assertTrue(all(candidate['assessment']['basis']=='user_excluded_account'
                            for candidate in candidates))
        self.assertEqual(r.decision_for(result,'fixture_other',other_message)['action'],'exclude')
        self.assertEqual(len(result['data']['exclusions']),1)
        self.assertEqual(result['data']['exclusions'][0]['identity_key'],'open:oid1')
        self.assertEqual(result['data']['exclusions'][0]['observed_groups'],['另一个来源群','测试来源群'])

    def test_global_exclusion_can_be_restored_from_deduplicated_list(self):
        self.save('exclude')
        excluded=self.snapshot()['data']['exclusions'][0]
        result=r.save_decision({'action':'reset','expected_revision':1,'note':'恢复识别'},
                               excluded,'管理员测试','usr_fixture',self.roster)
        self.assertTrue(result['ok'],result)
        roster=r.load_roster(self.roster)
        self.assertEqual(roster['exclusions'],[])
        self.assertEqual(r.decision_for(self.snapshot(),'fixture',MESSAGE)['action'],'parse')

    def test_confirmation_remains_group_scoped(self):
        self.save('confirm')
        roster=r.load_roster(self.roster)
        message=r.normalize_message(MESSAGE)
        self.assertEqual(assess_group([message],as_of=AS_OF,group_id='cid1',confirmations=roster['members'])['data']['members'][0]['status'],'publisher')
        self.assertNotEqual(assess_group([message],as_of=AS_OF,group_id='cid2',confirmations=roster['members'])['data']['members'][0]['basis'],'user_confirmed_account')
    def test_invalid_roster_fails_instead_of_disabling_policy(self):
        self.roster.write_text('{broken',encoding='utf-8')
        with self.assertRaises(ValueError):self.snapshot()
    def test_version_1_2_roster_upgrades_without_changing_revision(self):
        roster=r.load_roster(self.roster)
        roster['version']='1.2.0';r.atomic_json(self.roster,roster)
        upgraded=r.load_roster(self.roster)
        self.assertEqual(upgraded['version'],'1.3.0')
        self.assertEqual(upgraded['revision'],0)
    def test_managed_account_kept_without_recent_messages(self):
        self.save('confirm')
        r.atomic_json(self.work/'msgs_fixture/fixture.json',[])
        candidate=self.candidate()
        self.assertEqual(candidate['name'],'航管甲');self.assertEqual(candidate['assessment']['message_count'],0)
        self.assertEqual(candidate['assessment']['basis'],'user_confirmed_account')
    def test_receipt_full_coverage_requires_matching_archive(self):
        folder=self.work/'run_fixture';raw=folder/'raw_archive/pull_fixture.json';raw.parent.mkdir(parents=True)
        envelope={'messages':[],'count':0,'complete':True,'paginationKnown':True,'hasMore':False}
        raw.write_text(json.dumps(envelope),encoding='utf-8')
        r.atomic_json(folder/'pull_receipt_fixture.json',{'created_at':'2026-09-26T00:00:00+08:00','groups':{
            'fixture':{'status':'ok','raw_sha256':hashlib.sha256(raw.read_bytes()).hexdigest(),
                       'raw_file':'raw_archive/pull_fixture.json','watermark_before':'2026-09-25T00:00:00+08:00'}}})
        self.assertEqual(self.snapshot()['data']['groups'][0]['covered_workdays'],1)
        raw.write_text('{}',encoding='utf-8')
        self.assertEqual(self.snapshot()['data']['groups'][0]['covered_workdays'],0)
    def test_builder_skips_explicit_exclusion_preserves_raw(self):
        self.save('exclude')
        folder=self.work/'msgs_fixture'
        unknown=dict(MESSAGE,id='unknown',s='乙',senderOpenDingTalkId='oid2',senderId='sid2',c='请问价格')
        r.atomic_json(folder/'fixture.json',[MESSAGE,unknown])
        original=(folder/'fixture.json').read_bytes()
        with patch.object(builder,'WORK',str(self.work)),patch.object(builder,'GMAP',{'fixture':GROUPS[0]}),\
             patch.object(builder,'publisher_snapshot',side_effect=self.snapshot),patch.object(builder.P,'sfx',return_value=''),\
             patch('sys.argv',['record_builder.py','--date','fixture']):
            builder.main()
        needs=r.read_json(self.work/'draft_fixture/needs_llm.json')
        self.assertEqual([n['id'] for n in needs],[])
        self.assertEqual((folder/'fixture.json').read_bytes(),original)
    def test_ocr_queue_skips_excluded_without_ai_or_download(self):
        self.save('exclude');state={'new_ids':['m1']}
        def fake_builder(*args,**kwargs):
            folder=self.work/'draft_fixture';folder.mkdir(exist_ok=True)
            r.atomic_json(folder/'needs_llm.json',[])
        with patch.object(p,'WORK',str(self.work)),patch.object(p,'GROUPS',GROUPS),patch.object(p,'GNAME',{'fixture':'测试来源群'}),\
             patch.object(p,'publisher_snapshot',side_effect=self.snapshot),patch.object(p.P,'sfx',return_value=''),\
             patch.object(p,'_ensure_knowledge',return_value=True),\
             patch.object(p,'save_state'),patch.object(p,'emit'),patch.object(p,'step_build') as build,\
             patch.object(p.subprocess,'run',side_effect=fake_builder),patch.object(p.db_client,'images_dir',return_value=str(self.work)):
            p.run_parse('fixture',state)
        build.assert_called_once();self.assertEqual(state['publisher_excluded_ids'],['m1'])
        queue=r.read_json(self.work/'run_fixture/identity_review_queue.json')
        self.assertEqual(queue['data']['messages'],[])
        audit=r.read_json(self.work/'run_fixture'/state['publisher_audit_file'])
        self.assertEqual(audit['data']['counts']['excluded'],1)
    def test_stale_submission_rebuilds_before_write(self):
        with patch.object(p,'load_roster',return_value={'revision':2}),patch.object(p,'run_parse') as parse,\
             patch.object(p.db_client,'submit') as submit:
            p.advance_submit('fixture',{'publisher_revision':1})
        parse.assert_called_once();submit.assert_not_called()
    def test_all_excluded_can_finish_only_with_durable_decision(self):
        audit=self.work/'run_fixture/identity_decisions_r0_test.json'
        r.atomic_json(audit,{'ok':True,'data':{'roster_revision':0,'messages':[{'message_id':'m1','action':'exclude','identity_ready':True}]}})
        state={'publisher_audit_file':audit.name,'publisher_revision':0,'new_ids':['m1'],'publisher_excluded_ids':['m1'],
               'precheck_validated':[],'submit_queue':[],'submit_done':[]}
        with patch.object(p,'WORK',str(self.work)),patch.object(p,'publisher_policy_changed',return_value=False),\
             patch.object(p,'save_state'),patch.object(p,'commit_watermark') as wm,patch.object(p,'done_summary'),\
             patch.object(p.db_client,'submit') as submit:
            p.advance_submit('fixture',state)
        self.assertEqual(state['stage'],'DONE');wm.assert_called_once();submit.assert_not_called()

    def test_missing_exclusion_audit_never_advances_watermark(self):
        state={'publisher_revision':0,'publisher_excluded_ids':['m1'],'publisher_audit_file':'absent.json'}
        with patch.object(p,'WORK',str(self.work)),patch.object(p.P,'sfx',return_value=''):
            with self.assertRaises(OSError):p.verified_publisher_exclusions('fixture',state)
    def test_concurrent_managers_do_not_lose_history(self):
        from concurrent.futures import ThreadPoolExecutor
        candidate=self.candidate()
        def update(action):
            return r.save_decision({'action':action,'expected_revision':0,'note':'parallel'},candidate,'admin','id',self.roster)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(update,['confirm','exclude']))
        self.assertEqual(sum(result['ok'] for result in results),1)
        self.assertEqual(len(r.load_roster(self.roster)['audit']),1)

if __name__=='__main__':unittest.main()
