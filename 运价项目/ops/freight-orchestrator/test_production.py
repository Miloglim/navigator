import json
import tempfile
import unittest
from pathlib import Path
from freight_flow.transport.store import Store
from freight_flow.services.production import parse_pull, records_for_ledger
from freight_flow.services.production import _date
from freight_flow.services.production import classify_quoted_followup
from freight_flow.services.production import merge_products
from freight_flow.services.production import partial_records
from freight_flow.services.production import source_for_message
from freight_flow.services.workflow import coverage
from datetime import date
from freight_flow.transport.production import Production, unique_image_files
from freight_flow.transport.worker import work
from freight_flow.core.contracts import HISTORY_BATCH_STAGES
import threading

PROJECT = Path(__file__).resolve().parents[2]


class FakeLedger:
    def __init__(self):
        self.rows, self.writes, self.lose_receipt = [], 0, False
        self.issues = []
        self.failed_pods = set()
        self.resolution_complete = True

    def _request(self, method, url):
        return {'ok': True, 'rows': self.rows, 'total': len(self.rows)}

    def chunks(self, rows):
        if rows:
            yield rows

    def submit_missing(self, kind, rows):
        self.writes += 1
        for rec in rows:
            self.rows.append({'id': len(self.rows) + 1, 'kind': kind, 'payload': rec})
        return {'ok': not self.lose_receipt, 'coverage': {'ok': True}}

    def validate(self, kind, rows):
        aliases = {'宁波': 'Ningbo', 'QINGDAO, CHINA': 'Qingdao'}
        return {'ok': True, 'results': [{'ok': row.get('目的港') not in self.failed_pods, 'resolved': {
            key: aliases[value] for key, value in (('起运港', row.get('起运港')), ('目的港', row.get('目的港')))
            if value in aliases
        }} for row in rows]}

    def resolve_names(self, group, items):
        if not self.resolution_complete:
            return {'ok': False, 'data': []}
        aliases = {'宁波': 'Ningbo', '桑托斯': 'Santos', 'BRRIO': 'Rio de Janeiro'}
        return {'ok': True, 'data': [{'status': 'resolved', 'canonical': aliases.get(i['value'], i['value'])} for i in items]}

    def images_dir(self):
        return str(PROJECT / 'Database/images')

    def report_review_issues(self, issues):
        self.issues.extend(issues)
        return {'ok': True, 'data': {'count': len(issues)}}


class ProductionTests(unittest.TestCase):
    def test_production_runtime_is_absolute_for_detached_service(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / 'control.db')
            adapter = Production(PROJECT, Path('relative-runtime'), store, None, ledger=FakeLedger())
            self.assertTrue(adapter.runtime.is_absolute())

    def test_identical_image_aliases_are_collapsed_but_content_conflicts_block(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = root / 'iwExample.png'
            second = root / '$iwExample.png'
            first.write_bytes(b'same image')
            second.write_bytes(b'same image')
            self.assertEqual(len(unique_image_files([first, second])), 1)
            second.write_bytes(b'different')
            with self.assertRaisesRegex(RuntimeError, '内容不同'):
                unique_image_files([first, second])

    def test_partial_records_keep_usable_product_and_record_excluded_candidate(self):
        source = {'message_id': 'm', 'message_time': '2026-10-22 10:00:00', 'text': '宁波 桑托斯 500',
                  'group': {'name': 'g', 'cid': 'cid', 'default_pol': '宁波'}, 'sender': 'author',
                  'sender_id': 'sender-id', 'image_files': []}
        good = {'_table': 'rate', '起运港': '宁波', '目的港': '桑托斯', '船司': 'MSC',
                '柜型': '20GP', '海运费USD': 500, 'ETD': '', '有效期开始': '10/22',
                '有效期截止': '10/31', 'service': '', 'tag': '', 'source_row': 'text:1'}
        bad = {**good, 'source_row': 'invalid'}
        result = partial_records(source, [good, bad], 'coverage', '覆盖不一致', ['classify', 'extract'])
        self.assertEqual(len(result['records']['rates']), 1)
        self.assertEqual(result['identified'], 2)
        self.assertEqual(len(result['excluded']), 1)
        record = result['records']['rates'][0]
        self.assertFalse(record['partial_review'])
        self.assertTrue(record['message_partial_review'])
        self.assertEqual(record['partial_stage'], 'coverage')
        self.assertNotIn('[部分解析待核]', record['备注'])
        self.assertEqual(record['_source_row'], 'text:1')

    def test_partial_records_excludes_confirmed_non_products_but_keeps_single_gap(self):
        source = {'message_id': 'm', 'message_time': '2026-10-22 10:00:00', 'text': 'reference',
                  'group': {'name': 'g', 'cid': 'cid', 'default_pol': 'Ningbo'}, 'sender': 'author',
                  'sender_id': 'sender-id', 'image_files': []}
        good = {'_table': 'rate', '起运港': 'Ningbo', '目的港': 'Santos', '船司': 'MSC',
                '柜型': '20GP', '海运费USD': 500, 'ETD': '', '有效期开始': '10/22',
                '有效期截止': '10/31', 'service': '', 'tag': '', 'source_row': 'text:1'}
        no_service = {**good, '目的港': 'Suape', '海运费USD': '不收', '备注': '原图明确不收',
                      'source_row': 'text:2'}
        placeholder = {'_table': 'rate', '起运港': 'Ningbo', '目的港': '', '船司': '',
                       '柜型': '', '海运费USD': '', 'source_row': 'text:3'}
        one_gap = {**good, '目的港': 'Itajai', '船司': '', 'source_row': 'text:4'}
        result = partial_records(source, [good, no_service, placeholder, one_gap],
                                 'coverage', '覆盖不一致', ['classify', 'extract'])
        self.assertEqual(len(result['records']['rates']), 2)
        self.assertEqual({row['目的港'] for row in result['records']['rates']}, {'Santos', 'Itajai'})
        self.assertEqual(len(result['excluded']), 2)

    def test_partial_records_reference_never_becomes_a_space_product(self):
        source = {'message_id': 'm', 'message_time': '2026-10-19 16:51:30',
                  'text': '提单截图表', 'group': {'name': 'g', 'cid': 'cid',
                  'default_pol': 'Ningbo'}, 'sender': 'author', 'sender_id': 'sender-id',
                  'image_files': []}
        reference = {'_table': 'space', 'section_role': 'reference', '起运港': 'Ningbo',
                     '目的港': 'Callao', '船司': 'EMC', '柜型': '40HQ/HC', 'ETD': '2026-10-19',
                     '船名航次': 'WAN HAI A07 E023', '箱量': '1', '来源行': 'bill:1'}
        result = partial_records(source, [reference], 'coverage', '参考清单不能冒充产品')
        self.assertEqual(result['records'], {'rates': [], 'space': []})
        self.assertEqual(result['identified'], 1)
        self.assertEqual(len(result['excluded']), 1)
        self.assertIn('reference', result['excluded'][0]['reason'])

    def test_quoted_amendment_without_new_price_is_followup(self):
        source = {'own_text': '方案A可以先按照21天报，合同签订好了等修改',
                  'quoted_text': 'MSK 方案A/B 价格 3970/5030',
                  'quoted_message_id': 'source-message'}
        result = classify_quoted_followup(source, {'decision': 'quote', 'reason': 'has quote'})
        self.assertEqual(result['decision'], 'followup')
        self.assertIn('source-message', result['reason'])

    def test_quoted_amendment_with_new_price_remains_quote(self):
        source = {'own_text': '方案A改为 USD3990/5050，其余不变',
                  'quoted_text': 'MSK 方案A/B 价格 3970/5030',
                  'quoted_message_id': 'source-message'}
        result = classify_quoted_followup(source, {'decision': 'quote', 'reason': 'new price'})
        self.assertEqual(result['decision'], 'quote')

    def test_deliver_partial_writes_and_reads_products_and_keeps_issue_open(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / 'control.db')
            ledger = FakeLedger()
            adapter = Production(PROJECT, root, store, None, ledger=ledger)
            source = {'message_id': 'm', 'message_time': '2026-10-22 10:00:00', 'text': '宁波 桑托斯 500',
                      'group': {'name': 'g', 'cid': 'cid', 'slug': 'g', 'default_pol': '宁波'},
                      'sender': 'author', 'sender_id': 'sender-id', 'image_files': []}
            run = store.enqueue('source:m', 'message:g:m', source,
                                ('extract', 'coverage', 'normalize', 'validate', 'submit', 'readback'),
                                mode='production-message')
            task = store.claim('w')
            adapter.execution.task = task
            results = {
                'extract': {'records': [{'_table': 'rate', '起运港': '宁波', '目的港': '桑托斯',
                    '船司': 'MSC', '柜型': '20GP', '海运费USD': 500, 'ETD': '',
                    '有效期开始': '10/22', '有效期截止': '10/31', 'service': '', 'tag': '',
                    'source_row': 'text:1'}]},
                'inventory': {'cells': []},
            }
            receipt = adapter.deliver_partial(task, results, '独立盘点为空')
            store.finish_partial(task, receipt, '独立盘点为空')
            self.assertEqual(store.detail(run)['status'], 'PARTIAL_REVIEW_COMPLETE')
            self.assertEqual(receipt['readback']['covered_ids'], [1])
            self.assertEqual(len(ledger.issues), 1)
            self.assertFalse(ledger.rows[0]['payload']['partial_review'])
            self.assertTrue(ledger.rows[0]['payload']['message_partial_review'])
            self.assertNotIn('部分解析待核', ledger.rows[0]['payload']['备注'])
            self.assertEqual(ledger.rows[0]['payload']['_source_row'], 'text:1')
            self.assertTrue((root / 'artifacts' / run / 'partial-delivery.json').is_file())

    def test_deliver_partial_uses_authoritative_name_resolution_before_readback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / 'control.db')
            ledger = FakeLedger()
            adapter = Production(PROJECT, root, store, None, ledger=ledger)
            source = {'message_id': 'm', 'message_time': '2026-10-22 10:00:00', 'text': 'BRRIO 4120',
                      'group': {'name': 'g', 'cid': 'cid', 'slug': 'g', 'default_pol': '宁波'},
                      'sender': 'author', 'sender_id': 'sender-id', 'image_files': []}
            run = store.enqueue('source:m', 'message:g:m', source, ('extract', 'coverage'),
                                mode='production-message')
            task = store.claim('w')
            adapter.execution.task = task
            results = {'extract': {'records': [{'_table': 'rate', '起运港': '宁波', '目的港': 'BRRIO',
                '船司': 'HMM', '柜型': '20GP', '海运费USD': 4120, 'ETD': '',
                '有效期开始': '10/30', '有效期截止': '11/7', 'service': '', 'tag': '',
                'source_row': 'text:1'}]}}
            receipt = adapter.deliver_partial(task, results, 'coverage mismatch')
            store.finish_partial(task, receipt, 'coverage mismatch')
            self.assertEqual(receipt['readback']['covered_ids'], [1])
            self.assertEqual(ledger.rows[0]['payload']['目的港'], 'Rio de Janeiro')

    def test_deliver_partial_uses_validate_resolution_without_requiring_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / 'control.db')
            ledger = FakeLedger()
            adapter = Production(PROJECT, root, store, None, ledger=ledger)
            source = {'message_id': 'm', 'message_time': '2026-10-22 10:00:00', 'text': 'QINGDAO, CHINA Santos',
                      'group': {'name': 'g', 'cid': 'cid', 'slug': 'g'}, 'sender': 'author',
                      'sender_id': 'sender-id', 'image_files': []}
            run = store.enqueue('source:m', 'message:g:m', source, ('extract', 'coverage'),
                                mode='production-message')
            task = store.claim('w')
            adapter.execution.task = task
            results = {'extract': {'records': [{'_table': 'rate', '起运港': 'QINGDAO, CHINA',
                '目的港': 'Santos', '船司': 'MSC', '柜型': '20GP', '海运费USD': 500, 'ETD': '',
                '有效期开始': '10/22', '有效期截止': '10/31', 'service': '', 'tag': '',
                'source_row': 'text:1'}]}}
            receipt = adapter.deliver_partial(task, results, 'coverage mismatch')
            store.finish_partial(task, receipt, 'coverage mismatch')
            self.assertEqual(receipt['readback']['covered_ids'], [1])
            self.assertEqual(ledger.rows[0]['payload']['起运港'], 'Qingdao')

    def test_partial_validation_does_not_blank_distinct_unresolved_products(self):
        class BlankingLedger(FakeLedger):
            def validate(self, kind, rows):
                return {'ok': True, 'results': [
                    {'ok': False, 'resolved': {'目的港': row.get('目的港')},
                     'name_resolution': {'fields': [
                         {'field': '目的港', 'matches': [{'status': 'unknown'}]},
                     ]}} for row in rows
                ]}

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / 'control.db')
            ledger = BlankingLedger()
            adapter = Production(PROJECT, root, store, None, ledger=ledger)
            source = {'message_id': 'm', 'message_time': '2026-10-22 10:00:00', 'text': 'AR/UY USD 4100/NOR',
                      'group': {'name': 'g', 'cid': 'cid', 'slug': 'g'}, 'sender': 'author',
                      'sender_id': 'sender-id', 'image_files': []}
            run = store.enqueue('source:m', 'message:g:m', source, ('extract', 'coverage'),
                                mode='production-message')
            task = store.claim('w')
            adapter.execution.task = task
            base = {'_table': 'rate', '起运港': 'Qingdao', '船司': 'PIL', '柜型': '40NOR',
                    '海运费USD': 4100, 'ETD': '', '有效期开始': '10/19', '有效期截止': '10/21',
                    'service': '', 'tag': ''}
            results = {'extract': {'records': [
                dict(base, 目的港='Argentina', source_row='text:1'),
                dict(base, 目的港='Uruguay', source_row='text:1'),
            ]}}
            receipt = adapter.deliver_partial(task, results, 'coverage mismatch')
            store.finish_partial(task, receipt, 'coverage mismatch')
            self.assertEqual([row['payload']['目的港'] for row in ledger.rows], [''])
            self.assertTrue(all(row['payload']['partial_review'] for row in ledger.rows))
            self.assertIn('原始目的港=Argentina', ledger.rows[0]['payload']['备注'])
            self.assertIn('原始目的港=Uruguay', ledger.rows[0]['payload']['备注'])
            self.assertIn('原始目的港=Argentina', ledger.issues[0]['reasons'][-1])
            self.assertIn('原始目的港=Uruguay', ledger.issues[0]['reasons'][-1])
            self.assertEqual(receipt['readback']['covered_ids'], [1])

    def test_partial_delivery_marks_only_failed_product_incomplete(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / 'control.db')
            ledger = FakeLedger()
            ledger.failed_pods.add('Unknown Port')
            adapter = Production(PROJECT, root, store, None, ledger=ledger)
            source = {'message_id': 'm', 'message_time': '2026-10-22 10:00:00', 'text': 'Santos Unknown',
                      'group': {'name': 'g', 'cid': 'cid', 'slug': 'g', 'default_pol': '宁波'},
                      'sender': 'author', 'sender_id': 'sender-id', 'image_files': []}
            run = store.enqueue('source:m', 'message:g:m', source, ('extract', 'coverage'),
                                mode='production-message')
            task = store.claim('w')
            adapter.execution.task = task
            base = {'_table': 'rate', '起运港': '宁波', '船司': 'MSC', '柜型': '20GP',
                    '海运费USD': 500, 'ETD': '', '有效期开始': '10/22', '有效期截止': '10/31',
                    'service': '', 'tag': ''}
            results = {'extract': {'records': [dict(base, 目的港='Santos', source_row='text:1'),
                                                   dict(base, 目的港='Unknown Port', source_row='text:2')]}}
            receipt = adapter.deliver_partial(task, results, 'coverage mismatch')
            store.finish_partial(task, receipt, 'coverage mismatch')
            statuses = {row['payload']['目的港']: row['payload']['partial_review'] for row in ledger.rows}
            self.assertFalse(statuses['Santos'])
            self.assertTrue(statuses['Unknown Port'])

    def test_partial_readback_falls_back_to_stable_product_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ledger = FakeLedger()
            adapter = Production(PROJECT, root, None, None, ledger=ledger)
            source = {'message_id': 'm', 'text': 'source', 'group': {'name': 'g'}}
            expected = {'消息ID': 'm', '来源群': 'g', '消息原文': 'source', '目的港': 'Jakarta, Java',
                        '柜型': '40HQ/HC', '海运费USD': 1000, '备注': '[部分解析待核];来源行=text:1',
                        'partial_review': True, 'message_partial_review': True}
            actual = dict(expected, 目的港='Jakarta', partial_review=False)
            ledger.rows = [{'id': 7, 'kind': 'rates', 'payload': actual}]
            result = adapter.readback(source, {'rates': [expected]}, settle_issue=False)
            self.assertEqual(result['covered_ids'], [7])
            self.assertEqual(result['match_mode'], 'partial_product_counts')

    def test_partial_delivery_tolerates_incomplete_name_resolution_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / 'control.db')
            ledger = FakeLedger()
            ledger.resolution_complete = False
            adapter = Production(PROJECT, root, store, None, ledger=ledger)
            source = {'message_id': 'm', 'message_time': '2026-10-22 10:00:00', 'text': 'QINGDAO Santos',
                      'group': {'name': 'g', 'cid': 'cid', 'slug': 'g'}, 'sender': 'author',
                      'sender_id': 'sender-id', 'image_files': []}
            run = store.enqueue('source:m', 'message:g:m', source, ('extract', 'coverage'),
                                mode='production-message')
            task = store.claim('w')
            adapter.execution.task = task
            results = {'extract': {'records': [{'_table': 'rate', '起运港': 'QINGDAO, CHINA',
                '目的港': 'Santos', '船司': 'MSC', '柜型': '20GP', '海运费USD': 500, 'ETD': '',
                '有效期开始': '10/22', '有效期截止': '10/31', 'service': '', 'tag': '',
                'source_row': 'text:1'}]}}
            receipt = adapter.deliver_partial(task, results, 'coverage mismatch')
            store.finish_partial(task, receipt, 'coverage mismatch')
            self.assertEqual(receipt['readback']['covered_ids'], [1])

    def test_no_product_partial_delivery_still_writes_review_issue(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / 'control.db')
            ledger = FakeLedger()
            adapter = Production(PROJECT, root, store, None, ledger=ledger)
            source = {'message_id': 'm2', 'message_time': '2026-10-22 10:00:00', 'text': '未知报价',
                      'group': {'name': 'g', 'cid': 'cid', 'slug': 'g'}, 'sender': 'author',
                      'sender_id': 'sender-id', 'image_files': []}
            run = store.enqueue('source:m2', 'message:g:m2', source, ('extract',), mode='production-message')
            task = store.claim('w')
            adapter.execution.task = task
            receipt = adapter.deliver_partial(task, {}, '无法形成产品')
            store.finish_partial(task, receipt, '无法形成产品')
            self.assertEqual(receipt['pending_ids'], [])
            self.assertEqual(ledger.rows, [])
            self.assertTrue(ledger.issues[0]['reasons'])
            self.assertEqual(store.detail(run)['status'], 'PARTIAL_REVIEW_COMPLETE')

    def test_technical_failure_report_includes_completed_stage_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / 'control.db')
            ledger = FakeLedger()
            adapter = Production(PROJECT, root, store, None, ledger=ledger)
            source = {'message_id': 'm3', 'message_time': '2026-10-22 10:00:00', 'text': '报价图',
                      'group': {'name': 'g', 'cid': 'cid', 'slug': 'g'}, 'sender': 'author',
                      'sender_id': 'sender-id', 'image_files': [], 'media_ids': ['media-1']}
            run = store.enqueue('source:m3', 'message:g:m3', source, ('extract',),
                                mode='production-message')
            task = store.claim('w')
            results = {'classify': {'decision': 'quote', 'facts': [{'text': '日期 10.29'}]},
                       'vision': {'facts': [{'text': '价格表'}]},
                       'extract': {'records': [{'_table': 'rate'}, {'_table': 'space'}]},
                       'inventory': {'cells': [{'pod': 'Santos'}]}}
            self.assertTrue(adapter.report_failure(task, 'Agent 超时', results))
            reasons = '\n'.join(ledger.issues[0]['reasons'])
            self.assertIn('卡在extract', reasons)
            self.assertIn('已完成阶段：classify/vision/extract/inventory', reasons)
            self.assertIn('识别产品：2；独立盘点格：1', reasons)
            self.assertIn('需人工确认', reasons)

    def test_worker_uses_partial_terminal_after_business_failure(self):
        class Adapter:
            def __init__(self):
                self.delivered = 0

            def execute(self, task, results):
                raise ValueError('业务证据冲突')

            def deliver_partial(self, task, results, reason):
                self.delivered += 1
                return {'ok': True, 'status': 'PARTIAL_REVIEW_COMPLETE', 'records': {}, 'pending_ids': []}

        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'control.db')
            adapter = Adapter()
            run = store.enqueue('m', 'message:g:m', {}, ('extract', 'validate', 'readback'),
                                mode='production-message')
            work(store, adapter, threading.Event(), once=True)
            detail = store.detail(run)
            self.assertEqual(adapter.delivered, 1)
            self.assertEqual(detail['status'], 'PARTIAL_REVIEW_COMPLETE')
            self.assertEqual([task['status'] for task in detail['tasks']],
                             ['PARTIAL_REVIEW', 'SKIPPED', 'SKIPPED'])

    def test_worker_keeps_batch_failure_out_of_message_partial_delivery(self):
        class Adapter:
            def __init__(self):
                self.delivered = 0

            def execute(self, task, results):
                raise ValueError('批次来源不完整')

            def deliver_partial(self, task, results, reason):
                self.delivered += 1
                return {'ok': True, 'status': 'PARTIAL_REVIEW_COMPLETE'}

        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'control.db')
            adapter = Adapter()
            run = store.enqueue('batch', 'group:test', {}, ('pull', 'fanout', 'watermark'),
                                mode='production-batch')
            work(store, adapter, threading.Event(), once=True)
            detail = store.detail(run)
            self.assertEqual(adapter.delivered, 0)
            self.assertEqual(detail['status'], 'NEEDS_REVIEW')
            self.assertEqual(detail['tasks'][0]['status'], 'NEEDS_REVIEW')
            self.assertEqual(detail['tasks'][1]['status'], 'BLOCKED')

    def test_worker_uses_partial_terminal_after_technical_failure_when_extract_exists(self):
        class Adapter:
            def __init__(self):
                self.delivered = 0

            def execute(self, task, results):
                raise RuntimeError('Agent 超时，未完成本阶段')

            def deliver_partial(self, task, results, reason):
                self.delivered += 1
                self.saw_extract = bool(results.get('extract'))
                return {'ok': True, 'status': 'PARTIAL_REVIEW_COMPLETE', 'records': {'rates': 1}, 'pending_ids': [1]}

        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'control.db')
            adapter = Adapter()
            run = store.enqueue('m-tech', 'message:g:m-tech', {}, ('extract', 'inventory'),
                                mode='production-message')
            task = store.claim('w')
            with store.connect() as db:
                db.execute("UPDATE tasks SET status='SUCCEEDED',result=? WHERE id=?", (json.dumps({'records': [{'_table': 'rate'}]}), task['id']))
            work(store, adapter, threading.Event(), once=True)
            detail = store.detail(run)
            self.assertEqual(adapter.delivered, 1)
            self.assertTrue(adapter.saw_extract)
            self.assertEqual(detail['status'], 'PARTIAL_REVIEW_COMPLETE')
            self.assertEqual(detail['tasks'][1]['failure_kind'], 'technical')

    def test_worker_makes_extract_timeout_visible_even_without_products(self):
        class Adapter:
            def __init__(self):
                self.delivered = 0

            def execute(self, task, results):
                raise RuntimeError('Agent 超时，未完成本阶段')

            def deliver_partial(self, task, results, reason):
                self.delivered += 1
                self.saw_prepare = bool(results.get('prepare'))
                self.saw_extract = results.get('extract') is not None
                return {'ok': True, 'status': 'PARTIAL_REVIEW_COMPLETE', 'records': {},
                        'pending_ids': [], 'review_issue_acknowledged': True}

        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'control.db')
            adapter = Adapter()
            run = store.enqueue('m-timeout', 'message:g:m-timeout', {},
                                ('prepare', 'extract', 'inventory'), mode='production-message')
            prepare = store.claim('setup')
            store.finish(prepare, result={'text': '原始图片证据已固化'})
            work(store, adapter, threading.Event(), once=True)
            detail = store.detail(run)
            self.assertEqual(adapter.delivered, 1)
            self.assertTrue(adapter.saw_prepare)
            self.assertFalse(adapter.saw_extract)
            self.assertEqual(detail['status'], 'PARTIAL_REVIEW_COMPLETE')
            self.assertEqual(detail['tasks'][1]['failure_kind'], 'technical')

    def test_parent_reconciles_partial_review_child_as_closed_delivery(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / 'control.db')
            adapter = Production(PROJECT, root, store, None, ledger=FakeLedger())
            child = store.enqueue('source:m', 'message:g:m', {}, ('extract',), mode='production-message')
            child_task = store.claim('w')
            group = {'slug': 'g', 'name': 'g', 'cid': 'cid'}
            parent = store.enqueue('parent', 'group:g', {'group': group}, ('reconcile',), mode='production-batch')
            store.depend(parent, child)
            with store.connect() as db:
                db.execute("UPDATE tasks SET status='HELD' WHERE run_id=?", (parent,))
            receipt = {'ok': True, 'status': 'PARTIAL_REVIEW_COMPLETE', 'records': {'rates': 1},
                       'pending_ids': [1], 'review_issue_acknowledged': True}
            result = store.finish_partial(child_task, receipt, 'coverage conflict')
            self.assertEqual(result['released_parents'], 1)
            parent_task = store.claim('w')
            adapter.execution.task = parent_task
            output = adapter.execute(parent_task, {'pull': {'messages': [{'messageId': 'm'}]}})
            self.assertEqual(output['messages'], 1)
            self.assertEqual(output['receipts'][0]['status'], 'PARTIAL_REVIEW_COMPLETE')
            store.finish(parent_task, result=output)
            self.assertEqual(store.detail(parent)['status'], 'DONE')

    def test_cancel_history_only_cancels_history_tree(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'control.db')
            parent = store.enqueue('history-root', 'history:g', {'history_window': {}},
                                   ('pull', 'reconcile'), mode='history-batch')
            child = store.enqueue('source:m', 'message:g:m', {},
                                  ('prepare', 'extract'), mode='production-message')
            live = store.enqueue('source:live', 'message:g:live', {},
                                 ('prepare', 'extract'), mode='production-message')
            store.depend(parent, child)
            result = store.cancel_history('测试取消')
            self.assertEqual(result['roots'], 1)
            self.assertEqual(result['runs'], 2)
            self.assertEqual(result['cancelled_tasks'], 4)
            self.assertTrue(all(task['status'] == 'SKIPPED' for task in store.detail(parent)['tasks']))
            self.assertTrue(all(task['status'] == 'SKIPPED' for task in store.detail(child)['tasks']))
            self.assertTrue(all(task['status'] == 'QUEUED' for task in store.detail(live)['tasks']))

    def test_coverage_ignores_reference_and_local_charge_sections(self):
        product = {'目的港': 'Istanbul', '柜型': '40HQ/HC', '海运费USD': 2953,
                   '起运港': 'Nansha', 'source_row': 'text:1', 'section_role': 'product'}
        product_cell = {'pod': 'Istanbul', 'container': '40HQ/HC', 'price': 2953,
                        '起运港': 'Nansha', 'source_row': 'text:1', 'section_role': 'product'}
        reference_cell = {'pod': 'Ambarlı', 'container': '20GP', 'price': 0,
                          '起运港': 'Nansha', 'source_row': 'image:1:row:2', 'section_role': 'reference'}
        local_cell = {'pod': 'Gebze', 'container': '40HQ/HC', 'price': 75,
                      '起运港': 'Nansha', 'source_row': 'image:1:row:3', 'section_role': 'local_charge'}
        result = coverage([product], [product_cell, reference_cell, local_cell])
        self.assertEqual(result['priced_cells'], 1)
        self.assertEqual(result['ignored_inventory_cells'], 2)
        self.assertEqual(result['records'], [product])
        self.assertEqual(coverage([], [reference_cell, local_cell])['records'], [])

    def test_coverage_ignores_unpriced_product_cells_without_hiding_real_gaps(self):
        no_service = {'pod': 'Iquique', 'container': '40NOR', 'price': '不收',
                      '起运港': 'Yantian', 'source_row': 'image:1:row:14', 'section_role': 'product'}
        surcharge = {'pod': 'Venezia', 'container': '20GP', 'price': '+75',
                     '起运港': 'Ningbo', 'source_row': 'text:4', 'section_role': 'product'}
        result = coverage([], [no_service, surcharge])
        self.assertEqual(result['priced_cells'], 0)
        self.assertEqual(result['ignored_inventory_cells'], 2)

        priced = {**surcharge, 'price': 2400}
        with self.assertRaisesRegex(ValueError, '明确报价格未覆盖 1 格'):
            coverage([], [no_service, priced])

    def test_coverage_keeps_space_records_out_of_rate_cell_matrix(self):
        space = {'_table': 'space', 'section_role': 'product', '目的港': 'Santos',
                 '舱位类型': '现舱', '价格USD': 100, 'source_row': 'text:2'}
        result = coverage([space], [])
        self.assertEqual(result['priced_cells'], 0)
        self.assertEqual(result['records'], [space])

    def test_coverage_treats_vessel_slash_spacing_as_equivalent(self):
        record = {'目的港': 'Evyap', '柜型': '40HQ/HC', '海运费USD': 4100,
                  '起运港': 'Shanghai', '船名航次': 'MSC REEF / GJ643W',
                  'source_row': 'text:3', 'section_role': 'product'}
        cell = {'pod': 'Evyap', 'container': '40HQ/HC', 'price': 4100,
                '起运港': 'Shanghai', '船名航次': 'MSC REEF GJ643W',
                'source_row': 'text:3', 'section_role': 'product'}
        self.assertTrue(coverage([record], [cell])['ok'])

    def test_quoted_image_keeps_its_own_message_id(self):
        source = source_for_message({'cid': 'c'}, {'id': 'reply', 'createTime': '2026-10-10 10:00:00',
            'content': '价格补充', 'quotedMessage': {'id': 'original', 'content': '[图片消息](mediaId=@actual)',
            'resourceRefs': [{'resourceId': '@actual', 'resourceIdType': 'mediaId'}]}}, 'rules')
        self.assertEqual(source['media_ids'], ['@actual'])
        self.assertEqual(source['display_media_ids'], ['@actual'])
        self.assertEqual(source['parse_media_ids'], [])
        self.assertEqual(source['quoted_media_ids'], ['@actual'])
        self.assertEqual(source['media_messages']['@actual'], 'original')
        self.assertEqual(source['media_roles']['@actual'], 'quoted')

    def test_only_current_message_images_are_parseable(self):
        def resource(media_id, owner):
            return {'resourceId': media_id, 'resourceIdType': 'mediaId',
                    'download': {'arguments': {'message-id': owner, 'resource-id': media_id,
                                                'open-conversation-id': 'c'}}}
        message = {'id': 'reply', 'createTime': '2026-10-10 10:00:00',
                   'content': '[图片消息](mediaId=@own)',
                   'resourceRefs': [resource('@own', 'reply')],
                   'quotedMessage': {'id': 'original', 'content': '[图片消息](mediaId=@quoted)',
                                     'resourceRefs': [resource('@quoted', 'original')]}}
        source = source_for_message({'cid': 'c'}, message, 'rules')
        self.assertEqual(source['display_media_ids'], ['@quoted', '@own'])
        self.assertEqual(source['parse_media_ids'], ['@own'])
        self.assertEqual(source['quoted_media_ids'], ['@quoted'])
        self.assertEqual(source['media_roles'], {'@quoted': 'quoted', '@own': 'own'})

    def test_prepare_downloads_quoted_images_for_display_but_not_parsing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            images_dir = root / 'images'
            images_dir.mkdir()
            (images_dir / '@own.png').write_bytes(b'own image')
            (images_dir / '@quoted.png').write_bytes(b'quoted image')
            ledger = FakeLedger()
            ledger.images_dir = lambda: str(images_dir)
            adapter = Production(PROJECT, root, Store(root / 'control.db'), None, ledger=ledger)
            source = {'message_id': 'reply', 'message_time': '2026-10-10 10:00:00', 'text': 'reply',
                      'group': {'cid': 'c'}, 'display_media_ids': ['@quoted', '@own'],
                      'parse_media_ids': ['@own'], 'quoted_media_ids': ['@quoted'],
                      'media_messages': {'@quoted': 'original', '@own': 'reply'}}
            prepared = adapter.prepare(source, root / 'artifact')
            self.assertEqual(len(prepared['image_paths']), 1)
            self.assertEqual(len(prepared['display_image_paths']), 2)
            self.assertEqual(len(prepared['image_hashes']), 1)
            self.assertEqual(len(prepared['display_image_hashes']), 2)
            self.assertEqual(sorted(prepared['image_files']), ['@own.png', '@quoted.png'])

    def test_expired_lease_cannot_start_ledger_write(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'control.db')
            run = store.enqueue('k', 'lane', {}, ['submit'])
            task = store.claim('owner')
            adapter = Production(PROJECT, Path(folder), store, None, ledger=FakeLedger())
            adapter.execution.task = task
            with store.connect() as db:
                db.execute('UPDATE tasks SET lease_until=0 WHERE run_id=?', (run,))
            with self.assertRaises(RuntimeError):
                adapter.write_guard()
            self.assertEqual(adapter.ledger.writes, 0)
    def test_same_product_rate_space_merge_preserves_quantity(self):
        base = {'起运港': 'Ningbo', '目的港': 'Los Angeles', '船司': 'CMA', '柜型': '40HQ/HC',
                '海运费USD': 6800, 'ETD': '2026-10-16', '备注': 'reference', '证据': '6800/40H'}
        rows = merge_products([{**base, '_table': 'rate', '箱量': ''},
                               {**base, '_table': 'space', '箱量': '1', '舱位类型': '加班船'}])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['箱量'], '1')
        self.assertEqual(rows[0]['海运费USD'], 6800)
        self.assertEqual(rows[0]['_table'], 'space')
        with self.assertRaises(ValueError):
            merge_products([{**base, '_table': 'space', '箱量': '1'},
                            {**base, '_table': 'space', '箱量': '2'}])
    def test_source_dates_are_format_equivalent(self):
        for value in ('Oct-26', '26 Oct', '10/26', '10.26', '2026-10-26'):
            self.assertEqual(_date(value, date(2026, 10, 10)), '2026-10-26')

    def test_pull_requires_complete_envelope(self):
        with self.assertRaises(ValueError):
            parse_pull({'messages': [], 'complete': False, 'paginationKnown': True, 'hasMore': False})
        self.assertEqual(parse_pull({'messages': [], 'count': 0, 'complete': True,
                                    'paginationKnown': True, 'hasMore': False}), [])

    def test_canary_pull_selects_only_oldest_unprocessed_message(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / 'control.db')
            adapter = Production(PROJECT, root, store, None, ledger=FakeLedger())
            adapter.paths = SimpleNamespace(WORK=str(root))
            adapter.wm_path = root / 'watermark.json'
            adapter.wm_path.write_text(json.dumps({'g': '2026-10-22 00:00:00'}))
            adapter._legacy_guard = lambda group: None
            adapter._dws = lambda args, cwd: {
                'complete': True, 'paginationKnown': True, 'hasMore': False,
                'count': 2, 'partial': False, 'truncated': False,
                'truncatedByPageLimit': False, 'truncatedByResultLimit': False,
                'failedCount': 0, 'decryptFailedCount': 0, 'failures': [],
                'messages': [
                    {'messageId': 'new', 'content': 'new', 'createTime': '2026-10-22 11:00:00'},
                    {'messageId': 'old', 'content': 'old', 'createTime': '2026-10-22 10:00:00'},
                ],
            }
            group = {'slug': 'g', 'name': 'g', 'cid': 'cid'}
            store.enqueue('canary-old', 'group:g', {'group': group, 'message_id': 'old'},
                          ('pull',), mode='production-batch')
            task = store.claim('w')
            result = adapter.execute(task, {})
            self.assertEqual([item['messageId'] for item in result['messages']], ['old'])
            self.assertEqual(result['after'], '2026-10-22 10:00:00')

    def test_canary_pull_rejects_a_non_oldest_message(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / 'control.db')
            adapter = Production(PROJECT, root, store, None, ledger=FakeLedger())
            adapter.paths = SimpleNamespace(WORK=str(root))
            adapter.wm_path = root / 'watermark.json'
            adapter.wm_path.write_text(json.dumps({'g': '2026-10-22 00:00:00'}))
            adapter._legacy_guard = lambda group: None
            adapter._dws = lambda args, cwd: {
                'complete': True, 'paginationKnown': True, 'hasMore': False,
                'count': 2, 'partial': False, 'truncated': False,
                'truncatedByPageLimit': False, 'truncatedByResultLimit': False,
                'failedCount': 0, 'decryptFailedCount': 0, 'failures': [],
                'messages': [
                    {'messageId': 'new', 'content': 'new', 'createTime': '2026-10-22 11:00:00'},
                    {'messageId': 'old', 'content': 'old', 'createTime': '2026-10-22 10:00:00'},
                ],
            }
            group = {'slug': 'g', 'name': 'g', 'cid': 'cid'}
            store.enqueue('canary-new', 'group:g', {'group': group, 'message_id': 'new'},
                          ('pull',), mode='production-batch')
            task = store.claim('w')
            with self.assertRaisesRegex(ValueError, '当前最旧'):
                adapter.execute(task, {})

    def test_canary_pull_skips_boundary_message_already_in_control_queue(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / 'control.db')
            existing = store.enqueue('source:cid:old', 'message:g:old', {'message_id': 'old'},
                                     ('extract',), mode='production-message')
            store.finish(store.claim('w'), result={'records': []})
            adapter = Production(PROJECT, root, store, None, ledger=FakeLedger())
            adapter.paths = SimpleNamespace(WORK=str(root))
            adapter.wm_path = root / 'watermark.json'
            adapter.wm_path.write_text(json.dumps({'g': '2026-10-22 00:00:00'}))
            adapter._legacy_guard = lambda group: None
            adapter._dws = lambda args, cwd: {
                'complete': True, 'paginationKnown': True, 'hasMore': False,
                'count': 2, 'partial': False, 'truncated': False,
                'truncatedByPageLimit': False, 'truncatedByResultLimit': False,
                'failedCount': 0, 'decryptFailedCount': 0, 'failures': [],
                'messages': [
                    {'messageId': 'new', 'content': 'new', 'createTime': '2026-10-22 11:00:00'},
                    {'messageId': 'old', 'content': 'old', 'createTime': '2026-10-22 10:00:00'},
                ],
            }
            group = {'slug': 'g', 'name': 'g', 'cid': 'cid'}
            store.enqueue('canary-new', 'group:g', {'group': group, 'message_id': 'new'},
                          ('pull',), mode='production-batch')
            task = store.claim('w')
            result = adapter.execute(task, {})
            self.assertEqual([item['messageId'] for item in result['messages']], ['new'])
            self.assertTrue(existing)

    def test_history_batch_uses_fixed_window_and_never_advances_realtime_watermark(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / 'control.db')
            adapter = Production(PROJECT, root, store, None, ledger=FakeLedger())
            adapter.paths = SimpleNamespace(WORK=str(root))
            adapter.wm_path = root / 'watermark.json'
            original_wm = {'g': '2026-10-22 17:00:00'}
            adapter.wm_path.write_text(json.dumps(original_wm))
            adapter._legacy_guard = lambda group: None
            adapter._dws = lambda args, cwd: {
                'complete': True, 'paginationKnown': True, 'hasMore': False,
                'count': 3, 'partial': False, 'truncated': False,
                'truncatedByPageLimit': False, 'truncatedByResultLimit': False,
                'failedCount': 0, 'decryptFailedCount': 0, 'failures': [],
                'messages': [
                    {'messageId': 'after', 'content': 'after', 'createTime': '2026-10-22 17:37:00'},
                    {'messageId': 'newer', 'content': 'newer', 'createTime': '2026-10-22 16:00:00'},
                    {'messageId': 'older', 'content': 'older', 'createTime': '2026-10-19 01:00:00'},
                ],
            }
            group = {'slug': 'g', 'name': 'g', 'cid': 'cid'}
            window = {'start': '2026-10-19 00:00:00', 'end': '2026-10-22 17:36:00', 'batch_size': 1}
            run = store.enqueue('history-1', 'history:g', {'group': group, 'history_window': window},
                                HISTORY_BATCH_STAGES, mode='history-batch')
            task = store.claim('w')
            adapter.execution.task = task
            pulled = adapter.execute(task, {})
            self.assertEqual([row['messageId'] for row in pulled['messages']], ['older'])
            committed = adapter.commit_history(group['slug'], window, ['older'])
            self.assertEqual(committed['processed_after'], 1)
            self.assertEqual(json.loads(adapter.wm_path.read_text()), original_wm)
            store.finish(task, result=pulled)
            self.assertEqual(store.detail(run)['status'], 'QUEUED')

    def test_parent_wait_does_not_hold_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'control.db')
            parent = store.enqueue('p', 'group:g', {}, ['pull', 'reconcile'], mode='production-batch')
            pull = store.claim('w')
            child = store.enqueue('c', 'message:g:m', {'message_id': 'm'}, ['submit'], mode='production-message')
            store.depend(parent, child)
            store.finish(pull, result={})
            task = store.claim('w')
            self.assertEqual(task['run_id'], child)
            store.finish(task, result={'covered_ids': [9]})
            self.assertEqual(store.claim('w')['stage'], 'reconcile')

    def test_source_and_date_conversion(self):
        source = {'message_id': 'msgx', 'text': '原文 EUR100', 'message_time': '2026-10-10 10:00:00',
                  'group': {'name': '测试群', 'cid': 'cid', 'default_pol': '青岛'}, 'sender': '人',
                  'image_files': ['actual.png']}
        candidate = {'_table': 'rate', '起运港': '', '目的港': 'Santos', '柜型': '40HQ/HC',
                     '海运费USD': '100', 'currency': 'EUR', '船司': 'MSC', '有效期开始': '10/12',
                     '有效期截止': '10/18', 'ETD': '', 'service': 'IPANEMA', 'tag': '快提',
                     '备注': 'EUR100', '证据': '原文'}
        result = records_for_ledger(source, [candidate])['rates'][0]
        self.assertEqual(result['消息原文'], source['text'])
        self.assertEqual(result['有效期起'], '2026-10-12')
        self.assertEqual(result['有效期止'], '2026-10-18')
        self.assertEqual(result['航线服务'], 'IPANEMA')
        self.assertEqual(result['海运费USD'], '100')
        self.assertEqual(result['币种'], 'EUR')
        self.assertEqual(result['起运港'], '青岛')

    def test_lost_submit_receipt_reads_before_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = FakeLedger()
            adapter = Production(PROJECT, Path(folder), None, None, ledger=ledger)
            source = {'message_id': 'actual', 'text': 'original', 'group': {'name': 'g'}}
            records = {'rates': [{'消息ID': 'actual', '来源群': 'g', '消息原文': 'original',
                                  '海运费USD': 55, '柜型': '20GP'}]}
            ledger.lose_receipt = True
            with self.assertRaises(RuntimeError):
                adapter.submit(source, records, Path(folder))
            ledger.lose_receipt = False
            adapter.submit(source, records, Path(folder))
            self.assertEqual(ledger.writes, 1)
            self.assertEqual(adapter.readback(source, records)['covered_ids'], [1])

    def test_readback_rejects_changed_price_and_wrong_source(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = FakeLedger()
            adapter = Production(PROJECT, Path(folder), None, None, ledger=ledger)
            source = {'message_id': 'actual', 'text': 'original', 'group': {'name': 'g'}}
            rec = {'消息ID': 'actual', '来源群': 'g', '消息原文': 'original', '海运费USD': 55}
            ledger.rows = [{'id': 1, 'kind': 'rates', 'payload': {**rec, '海运费USD': 56}},
                           {'id': 2, 'kind': 'rates', 'payload': {**rec, '来源群': 'other'}}]
            with self.assertRaises(RuntimeError):
                adapter.readback(source, {'rates': [rec]})

    def test_cursor_compare_and_swap_preserves_other_groups(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter = Production(PROJECT, Path(folder), None, None, ledger=FakeLedger())
            adapter.wm_path = Path(folder) / 'watermark.json'
            adapter.wm_path.write_text(json.dumps({'g': '2026-10-10 09:00:00', 'other': '2026-10-09 08:00:00'}))
            pull = {'expected_before': '2026-10-10 09:00:00', 'after': '2026-10-10 10:00:00'}
            adapter.commit_cursor('g', pull)
            adapter.commit_cursor('g', pull)  # Receipt lost after cursor update is recoverable.
            data = json.loads(adapter.wm_path.read_text())
            self.assertEqual(data['other'], '2026-10-09 08:00:00')
            with self.assertRaises(RuntimeError):
                adapter.commit_cursor('g', {**pull, 'after': '2026-10-10 11:00:00'})
            self.assertEqual(json.loads(adapter.wm_path.read_text())['g'], pull['after'])

    def test_failed_child_blocks_parent_cursor(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'control.db')
            parent = store.enqueue('p', 'group:g', {}, ['reconcile', 'watermark'], mode='production-batch')
            child = store.enqueue('c', 'message:g:m', {}, ['audit'], mode='production-message')
            store.depend(parent, child)
            task = store.claim('w')
            self.assertEqual(task['run_id'], child)
            store.finish(task, error='bad evidence', retryable=False)
            self.assertIsNone(store.claim('w'))
            self.assertNotEqual(store.detail(parent)['status'], 'DONE')

    def test_full_batch_restart_and_delivery_before_cursor(self):
        from types import SimpleNamespace
        from freight_flow.core.contracts import PRODUCTION_BATCH_STAGES
        from freight_flow.services.workflow import coverage
        class FakeAgent:
            def execute(self, task, results):
                stage = task['stage']
                if stage == 'classify':
                    return {'decision': 'quote', 'reason': 'explicit price'}
                if stage == 'extract':
                    return {'records': [{'_table': 'rate', '目的港': '桑托斯', '起运港': '宁波',
                        '船司': 'MSC', '柜型': '20GP', '海运费USD': 500, '备注': '',
                        'ETD': '', '有效期开始': '10/10', '有效期截止': '10/12', 'service': '', 'tag': ''}]}
                if stage == 'inventory':
                    return {'cells': [{'pod': 'Santos', '起运港': 'Ningbo', 'container': '20GP', 'price': 500,
                        'ETD': '', '有效期开始': '2026-10-10', '有效期截止': '2026-10-12', 'service': '', 'tag': ''}]}
                if stage == 'audit':
                    return {'ok': True, 'issues': []}
                raise AssertionError(stage)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / 'control.db')
            ledger = FakeLedger()
            adapter = Production(PROJECT, root, store, FakeAgent(), ledger=ledger)
            adapter.paths = SimpleNamespace(WORK=str(root))
            adapter.wm_path = root / 'watermark.json'
            adapter._knowledge = lambda group: 'test rules'
            # Fixed source window: the fixture must not depend on today's date.
            adapter.wm_path.write_text(json.dumps({'g': '2026-10-10 00:00:00'}))
            adapter._dws = lambda args, cwd: {'complete': True, 'paginationKnown': True, 'hasMore': False,
                'count': 1, 'messages': [{'id': 'm', 'content': '宁波 桑托斯 MSC 20GP 500',
                                        'createTime': '2026-10-10 10:00:00', 'sender': 'author'}]}
            group = {'slug': 'g', 'name': 'g', 'cid': 'cid', 'default_pol': '宁波'}
            run = store.enqueue('batch', 'group:g', {'group': group}, PRODUCTION_BATCH_STAGES, mode='production-batch')
            work(store, adapter, threading.Event(), once=True)
            self.assertEqual(json.loads(adapter.wm_path.read_text())['g'], '2026-10-10 00:00:00')
            adapter.store = store = Store(root / 'control.db')  # Simulate process restart after pull.
            for _ in range(20):
                work(store, adapter, threading.Event(), once=True)
                if store.detail(run)['status'] == 'DONE':
                    break
            self.assertEqual(store.detail(run)['status'], 'DONE')
            self.assertEqual(ledger.writes, 1)
            self.assertEqual(json.loads(adapter.wm_path.read_text())['g'], '2026-10-10 10:00:00')
            self.assertEqual(store.results(run)['reconcile']['messages'], 1)


if __name__ == '__main__':
    unittest.main()
