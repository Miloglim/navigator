"""Real build/checker interaction with temporary sources, no DWS or ledger writes."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pipeline as p


class SemanticGateTests(unittest.TestCase):
    def setUp(self):
        # These suites isolate ingestion/repair; knowledge-file receipts have their own tests.
        knowledge = patch.object(p, '_ensure_knowledge', return_value=True)
        knowledge.start(); self.addCleanup(knowledge.stop)
        report = patch.object(p.db_client, 'report_review_issues', side_effect=lambda rows: {'ok': True, 'data': {'count': len(rows)}})
        self.report = report.start(); self.addCleanup(report.stop)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.work=Path(self.tmp.name)
        self.events=[]
        for target, name, value in [(p,'WORK',str(self.work)),(p,'save_state',lambda *a:None),
                                    (p,'emit',lambda value:self.events.append(value)),
                                    (p,'publisher_policy_changed',lambda st:False),
                                    (p,'_ensure_knowledge',lambda *a,**k:True),
                                    (p,'_load_draft',lambda *a:[]),
                                    (p.llm_check.spec_index,'spec_for',lambda g:{'abbrev':[]}),
                                    (p.llm_check.spec_index,'resolve_basis',lambda g,b:(b!='broken','fixture'))]:
            item=patch.object(target,name,value);item.start();self.addCleanup(item.stop)
        proc=patch.object(p.subprocess,'run');self.proc=proc.start();self.addCleanup(proc.stop)
        self.messages=self.work/('msgs_fixture'+p.P.sfx());self.messages.mkdir()
        self.decisions=self.work/('llm_out_fixture'+p.P.sfx());self.decisions.mkdir()
        self.state={'stage':'BUILD','new_ids':['good','bad'],'pending_watermark':{'ningbo_space':'good-cursor','turkey_price':'bad-cursor'}}
        for mid,slug in [('good','ningbo_space'),('bad','turkey_price')]:
            source={'id':mid,'s':'sender','t':'2026-09-29 10:00','c':'source '+mid}
            (self.messages/(slug+'.json')).write_text(json.dumps([source]),encoding='utf-8')
            decision={'msg_id':mid,'group':slug,'decision':'records','rule_basis':'valid',
                      'records':[{'_table':'rate','目的港':'Santos','柜型':'20GP','海运费USD':100}]}
            (self.decisions/(mid+'.json')).write_text(json.dumps(decision),encoding='utf-8')
    def change(self,**updates):
        path=self.decisions/'bad.json';d=json.loads(path.read_text());d.update(updates);path.write_text(json.dumps(d),encoding='utf-8')

    def source_text(self, text):
        path = self.messages / 'turkey_price.json'
        source = json.loads(path.read_text(encoding='utf-8'))[0]
        source['c'] = text
        path.write_text(json.dumps([source]), encoding='utf-8')

    def test_group_mention_is_not_an_operation_notice(self):
        self.source_text('@所有人 MSC南美东SPOT LEVEL下调')
        p.step_build('fixture', self.state)
        self.assertFalse(self.state['semantic_issues'])
        self.assertEqual(len(self.state['submit_queue']), 2)

    def test_llm_ports_and_prices_are_checked_as_records_not_voyage_numbers(self):
        self.source_text('CMA 厦门10.17ETD COSCO ANTWERP BEX010W19\nDamietta 3300/4400')
        self.change(records=[{'_table': 'space', '起运港': '厦门', '目的港': 'Damietta',
                             '船司': 'CMA', '船名航次': 'COSCO ANTWERP BEX010W19',
                             '柜型': '20GP', '价格USD': 3300}])
        p.step_build('fixture', self.state)
        self.assertFalse(self.state['semantic_issues'])
        record = next(row['record'] for row in self.state['submit_queue']
                      if row['record']['消息ID'] == 'bad')
        self.assertEqual(record['船名航次'], 'COSCO ANTWERP BEX010W19')
        self.assertEqual(record['价格USD'], 3300)

    def test_inquiry_generated_as_quote_is_isolated(self):
        self.source_text('@所有人 2800 能接吗')
        self.change(records=[{'_table': 'rate', '目的港': 'Santos',
                             '柜型': '20GP', '海运费USD': 2800}])
        p.step_build('fixture', self.state)
        self.assertEqual({row['record']['消息ID'] for row in self.state['submit_queue']}, {'good', 'bad'})
        self.assertIn('inquiry', self.state['semantic_issues'][0]['errors'][0])

    def test_reviewed_operation_notice_is_complete_noise(self):
        self.source_text('@所有人 开港了，不出的及时退关')
        self.change(decision='noise', records=[])
        p.step_build('fixture', self.state)
        self.assertFalse(self.state['semantic_issues'])
        self.assertEqual({row['record']['消息ID'] for row in self.state['submit_queue']}, {'good', 'bad'})

    def test_operation_cannot_become_rate_but_space_fact_survives(self):
        self.source_text('COSCO 10.14 这水放舱了')
        p.step_build('fixture', self.state)
        self.assertTrue(self.state['semantic_issues'])
        self.change(records=[{'_table': 'space', '船司': 'COSCO', '目的港': 'CAR',
                             '舱位类型': '放舱开放'}])
        p.step_build('fixture', self.state)
        self.assertFalse(self.state['semantic_issues'])

    def test_missing_explicit_price_pair_still_blocks_after_llm_merge(self):
        self.source_text('HPL Santos 7500/40HQ 7600/40NOR')
        self.change(records=[{'_table': 'space', '船司': 'HPL', '目的港': 'Santos',
                             '柜型': '40HQ/HC', '价格USD': 7500}])
        p.step_build('fixture', self.state)
        self.assertEqual({row['record']['消息ID'] for row in self.state['submit_queue']}, {'good', 'bad'})
        self.assertTrue(self.state['semantic_issues'])
    def test_bad_basis_isolates_only_bad_message_and_preserves_watermark(self):
        self.change(rule_basis='broken')
        p.step_build('fixture',self.state)
        self.assertEqual({r['record']['消息ID'] for r in self.state['submit_queue']},{'good','bad'})
        self.assertEqual(self.state['review_blocked_groups'],['turkey_price'])
        issue = self.report.call_args.args[0][0]
        self.assertEqual((issue['source_group'], issue['message_id'], issue['message_text']),
                         ('turkey_price', 'bad', 'source bad'))
        self.assertTrue(issue['reasons'])
        p.commit_watermark('fixture',self.state)
        args=self.proc.call_args.args[0]
        self.assertEqual(json.loads(args[args.index('--map')+1]),
                         {'ningbo_space':'good-cursor','turkey_price':'bad-cursor'})
        p.done_summary('fixture',self.state,1)
        self.assertEqual(self.state['stage'], 'DONE')
        self.change(rule_basis='valid')
        p.step_build('fixture',self.state)
        self.assertEqual(self.state['review_blocked_groups'],[],'fixed evidence releases the cursor hold')
    def test_incomplete_image_blocks_even_a_valid_decision(self):
        path = self.messages / 'turkey_price.json'
        self.change(replace_template=True)
        source = json.loads(path.read_text())[0]; source['img'] = ['@partial']
        path.write_text(json.dumps([source]), encoding='utf-8')
        cache_dir = self.work / 'ocr_cache'; cache_dir.mkdir()
        (cache_dir / 'partial.json').write_text(json.dumps({'type':'rate_matrix','footer_notes':['原图起运港还包含太仓']}), encoding='utf-8')
        p.step_build('fixture', self.state)
        self.assertEqual({r['record']['消息ID'] for r in self.state['submit_queue']}, {'good','bad'})
        self.assertIn('turkey_price', self.state['review_blocked_groups'])

    def test_missing_ocr_blocks_text_decision_for_image_message(self):
        path = self.messages / 'turkey_price.json'
        source = json.loads(path.read_text())[0]
        source['img'] = ['@unread']
        path.write_text(json.dumps([source]), encoding='utf-8')
        p.step_build('fixture', self.state)
        self.assertEqual({r['record']['消息ID'] for r in self.state['submit_queue']}, {'good','bad'})
        self.assertTrue(any('尚无OCR缓存' in error for issue in self.state['semantic_issues']
                            for error in issue['errors']))
        self.assertEqual(self.report.call_args.args[0][0]['media_ids'], ['@unread'])

    def test_verified_formula_is_not_mistaken_for_an_empty_price_placeholder(self):
        path = self.messages / 'turkey_price.json'
        source = json.loads(path.read_text())[0]
        source.update(c='WHL table less USD200/200', img=['@formula'])
        path.write_text(json.dumps([source]),encoding='utf-8')
        cache = self.work/'ocr_cache/formula.json'
        cache.parent.mkdir()
        cache.write_text(json.dumps({'type':'formula_card','cost_formula':'table less USD200/200',
                         'source_rows':1,'source_priced_cells':1,'rows':[{'pod':'Santos','20GP':4000}]}),encoding='utf-8')
        self.change(replace_template=True,records=[{'_table':'rate','柜型':'20GP',
                    '起运港':'Qingdao','目的港':'Santos','船司':'WHL',
                    '备注':'参考FAK 4000; table less USD200/200'}])
        p.step_build('fixture',self.state)
        self.assertIn('bad',[r['record']['消息ID'] for r in self.state['submit_queue']])
        self.assertTrue(all(not r['record'].get('海运费USD') for r in self.state['submit_queue'] if r['record']['消息ID']=='bad'))

    def test_optional_space_type_keeps_empty_placeholder_guard(self):
        self.assertEqual(p.llm_check._invalid_space_fact({}, {'_table':'space','目的港':'Santos'}), [])
        self.assertEqual(p.llm_check._invalid_space_fact({}, {'_table':'space','航线':'地中海'}), [])
        self.assertEqual(p.llm_check._invalid_space_fact({}, {'_table':'space','航线服务':'WSA'}), [])
        self.assertTrue(p.llm_check._invalid_space_fact({}, {'_table':'space','船司':'HPL'}))

    def test_review_issue_write_failure_preserves_stage(self):
        self.change(rule_basis='broken')
        self.report.side_effect = lambda rows: {'ok': False, 'error': 'offline'}
        with self.assertRaises(SystemExit):
            p.step_build('fixture', self.state)
        self.assertEqual(self.state['stage'], 'BUILD')
        self.assertNotIn('submit_queue', self.state)

    def test_template_acceptance_requires_real_candidates(self):
        self.change(decision='template', records=[])
        p.step_build('fixture', self.state)
        self.assertEqual({r['record']['消息ID'] for r in self.state['submit_queue']}, {'good','bad'})
        self.assertTrue(any('没有对应模板候选' in error for item in self.state['semantic_issues'] for error in item['errors']))
        candidate={'record':{'消息ID':'bad','目的港':'Santos','柜型':'20GP','价格USD':100,'舱位类型':'现舱'},
                   '_meta':{'msg_id':'bad','table':'space','group':'turkey_price','conf':'med'}}
        with patch.object(p, '_load_draft', side_effect=lambda d,t: [candidate] if t == 'space' else []):
            p.step_build('fixture', self.state)
        self.assertFalse(self.state['semantic_issues'])
        self.assertEqual(len(self.state['submit_queue']), 2)

    def test_warning_does_not_block(self):
        self.change(records=[{'_table':'rate','目的港':'Santos','柜型':'45HQ','海运费USD':100}])
        p.step_build('fixture',self.state)
        self.assertEqual(len(self.state['submit_queue']),2)
        self.assertFalse(self.state['semantic_issues'])
    def test_wrong_group_and_missing_decision_are_isolated(self):
        self.change(group='ningbo_space')
        p.step_build('fixture',self.state)
        self.assertEqual(len(self.state['submit_queue']),2)
        (self.decisions/'bad.json').unlink()
        p.step_build('fixture',self.state)
        self.assertEqual(len(self.state['submit_queue']),2)
        self.assertTrue(any('没有模板' in e for i in self.state['semantic_issues'] for e in i['errors']))
    def test_unreadable_file_does_not_stop_other_valid_source(self):
        (self.decisions/'bad.json').write_text('{broken',encoding='utf-8')
        p.step_build('fixture',self.state)
        self.assertEqual({r['record']['消息ID'] for r in self.state['submit_queue']},{'good','bad'})
    def test_duplicate_conflicting_decisions_not_accepted(self):
        obj=json.loads((self.decisions/'bad.json').read_text());obj['decision']='noise';obj['records']=[]
        (self.decisions/'duplicate.json').write_text(json.dumps(obj),encoding='utf-8')
        p.step_build('fixture',self.state)
        self.assertEqual(len(self.state['submit_queue']),2)

if __name__=='__main__':unittest.main()
