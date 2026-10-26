import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from pending_quality.services.review import group_rows, check_report
from pending_quality.transport.store import Store
from pending_quality.transport import runner

def row(pid=1,check='passed',origin='agent',review='awaiting'):
    return {'id':pid,'kind':'space','origin':origin,'review_status':review,'check_status':check,
            'payload':{'消息ID':'m','来源群':'g','消息原文':'1850/2900+ [图片消息](mediaId=@abc)'},'edit_log':None}

class QualityTests(unittest.TestCase):
    def test_passed_placeholder_is_review_target(self):
        groups=group_rows([row()])
        self.assertEqual(len(groups),1)
        self.assertIn('正文出现双档金额，结构化价格全部为空',groups[0]['signals'])
    def test_passed_business_risk_is_ahead_of_known_failed(self):
        passed=group_rows([row()])[0];failed=group_rows([row(check='failed')])[0]
        self.assertGreater(passed['priority'],failed['priority'])
    def test_only_unreviewed_agent_sources(self):
        self.assertEqual(group_rows([row(origin='manual'),row(review='approved')]),[])
    def test_same_message_id_different_group_is_not_merged(self):
        other=row(2);other['payload']['来源群']='other'
        self.assertEqual(len(group_rows([row(),other])),2)
    def test_price_or_cabin_change_invalidates_snapshot(self):
        original=group_rows([row()])[0];changed=row();changed['payload']['价格USD']=1900
        self.assertNotEqual(original['fingerprint'],group_rows([changed])[0]['fingerprint'])
    def test_failed_rows_cannot_receive_second_repair(self):
        before=[row(check='failed')];after=[row()];after[0]['payload']['价格USD']=1900
        with self.assertRaises(ValueError):check_report({'outcome':'repaired','note':'A verified change to the price','evidence':['image']},before,after)
    def test_review_cannot_hide_concurrent_change(self):
        before=[row()];after=copy.deepcopy(before);after[0]['payload']['价格USD']=1900
        with self.assertRaises(ValueError):check_report({'outcome':'reviewed','note':'Read and confirmed full image','evidence':['image']},before,after)
    def test_queue_claim_is_exclusive_and_repair_is_not_requeued(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'queue.sqlite3';a=Store(path);b=Store(path)
            try:
                original=group_rows([row()]);a.sync(original);claim=a.claim(1)[0]
                self.assertEqual(b.claim(1),[])
                after=row();after['payload']['价格USD']=1900;version=group_rows([after])[0]
                a.finish(claim['claim'],version,{'outcome':'repaired','note':'Full source repair','evidence':['image']})
                b.sync([version]);self.assertEqual(b.claim(1),[])
                self.assertEqual(b.status()['repaired'],2)
            finally:a.close();b.close()
    def test_expired_repeated_runs_stop(self):
        with tempfile.TemporaryDirectory() as temp:
            a=Store(Path(temp)/'queue.sqlite3')
            try:
                items=group_rows([row()]);a.sync(items);a.claim(1)
                a.c.execute('UPDATE reviews SET lease_until=0, attempts=2');a.c.commit()
                a.sync(items);self.assertEqual(a.claim(1),[])
                self.assertEqual(a.status()['needs_human'],1)
            finally:a.close()
    def test_partial_pagination_fails_closed(self):
        with patch.object(runner.ledger_client,'_request',side_effect=[{'ok':True,'total':2,'rows':[row()]},{'ok':True,'total':2,'rows':[]} ]):
            with self.assertRaises(RuntimeError):runner.fetch_rows()
    def test_pagination_does_not_stop_at_server_page_limit(self):
        with patch.object(runner.ledger_client,'_request',side_effect=[{'ok':True,'total':2,'rows':[row()]},{'ok':True,'total':2,'rows':[row(2)]}]):
            self.assertEqual(len(runner.fetch_rows()),2)
    def test_human_approval_prevents_finish(self):
        with self.assertRaises(ValueError):check_report({'outcome':'reviewed','note':'All source fields are checked','evidence':['source']},[row()],[row(review='approved')])

class EvidenceBoundaryTests(unittest.TestCase):
    def test_dollar_prefixed_media_is_routed_to_original_image(self):
        item=group_rows([row()])[0]
        item['rows'][0]['payload']['消息原文']='[图片消息](mediaId=$dollar_image)'
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'Database/images').mkdir(parents=True)
            image=root/'Database/images/$dollar_image.png';image.write_bytes(b'image')
            with patch.object(runner,'PROJECT',root), \
                 patch.object(runner.paths,'chat_groups',return_value=[]):
                packet=runner.evidence(item)
            self.assertIn(str(image.resolve()),packet['original_images'])

    def test_existing_edit_log_never_grants_repair(self):
        edited=row();edited['payload']['消息原文']='HPL Qingdao Santos 2850';edited['edit_log']='human changed cabin'
        item=group_rows([edited])[0]
        with patch.object(runner.paths,'chat_groups',return_value=[{'name':'g','slug':'g'}]),patch.object(runner.spec_index,'brief',return_value={}):
            packet=runner.evidence(item)
        self.assertTrue(packet['existing_edits_protected'])
        self.assertFalse(packet['automatic_repair_allowed'])
    def test_repair_cannot_change_provenance(self):
        before=[row()];after=copy.deepcopy(before);after[0]['payload']['消息时间']='changed'
        with self.assertRaises(ValueError):check_report({'outcome':'repaired','note':'Changed payload with evidence','evidence':['source']},before,after)
    def test_pending_total_drift_aborts_full_source_read(self):
        with patch.object(runner.ledger_client,'_request',side_effect=[{'ok':True,'total':2,'rows':[row()]},{'ok':True,'total':3,'rows':[row(2)]}]):
            with self.assertRaises(RuntimeError):runner.fetch_rows()
    def test_missing_knowledge_cannot_complete_review(self):
        item=group_rows([row()])[0]
        with tempfile.TemporaryDirectory() as temp:
            runtime=Path(temp);store=Store(runtime/'reviews.sqlite3');store.sync([item]);claimed=store.claim(1)[0];store.close()
            packet_dir=runtime/'evidence';packet_dir.mkdir()
            (packet_dir/(item['key']+'-'+item['fingerprint']+'.json')).write_text('{"missing_evidence":["group knowledge"]}',encoding='utf-8')
            report=runtime/'report.json';report.write_text('{"outcome":"reviewed","note":"Confirmed source and image","evidence":["source"]}',encoding='utf-8')
            with patch.object(runner,'PROJECT',runtime),patch.object(runner,'RUNTIME',runtime):
                with self.assertRaisesRegex(ValueError,'missing source/knowledge'):runner.finish(claimed['claim'],report)

if __name__=='__main__':unittest.main()
