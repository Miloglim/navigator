import json
import tempfile
import unittest
from pathlib import Path
from freight_flow.transport.production import Production
from freight_flow.transport.store import Store
from test_production import FakeLedger, PROJECT


class RecoveryContracts(unittest.TestCase):
    def _settlement_fixture(self, folder):
        import json
        class Ledger(FakeLedger):
            def __init__(self):
                super().__init__()
                self.settled = []
                self.fail_settlement = False
            def report_review_issues(self, issues):
                return {'ok': True, 'data': {'reported': 1}}
            def settle_review_issue(self, group, mid, ids):
                self.settled.append((group, mid, ids))
                if self.fail_settlement:
                    raise RuntimeError('network unavailable')
                return {'ok': True, 'data': {'resolved': True, 'pending_count': len(ids)}}
        source = {'message_id': 'm', 'text': 'raw', 'group': {'slug': 'g', 'name': 'Group'}}
        rec = {'消息ID': 'm', '来源群': 'Group', '消息原文': 'raw', '目的港': 'Callao', '柜型': '20GP', '海运费USD': 500}
        ledger = Ledger()
        ledger.rows = [{'id': 7, 'kind': 'rates', 'payload': rec, 'review_status': 'awaiting'}]
        adapter = Production(PROJECT, Path(folder), Store(Path(folder)/'c.db'), None, ledger=ledger)
        adapter.execution.task = {'run_id': 'r'}
        from unittest.mock import Mock
        adapter.write_guard = Mock()
        task = {'run_id': 'r', 'mode': 'production-message', 'input': json.dumps(source)}
        return adapter, source, {'rates': [rec]}, task

    def test_readback_settles_only_reported_complete_source(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter, source, records, task = self._settlement_fixture(folder)
            adapter.report_failure(task, 'old parser failure')
            self.assertTrue((Path(folder)/'artifacts/r/review-issue.json').is_file())
            result = adapter.readback(source, records)
            self.assertEqual(result['covered_ids'], [7])
            self.assertEqual(adapter.ledger.settled, [('g', 'm', [7])])

    def test_readback_without_report_receipt_does_not_settle(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter, source, records, _ = self._settlement_fixture(folder)
            self.assertTrue(adapter.readback(source, records)['ok'])
            self.assertEqual(adapter.ledger.settled, [])

    def test_settlement_failure_preserves_readback_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter, source, records, task = self._settlement_fixture(folder)
            adapter.report_failure(task, 'old failure')
            adapter.ledger.fail_settlement = True
            with self.assertRaisesRegex(RuntimeError, '结案'):
                adapter.readback(source, records)
            self.assertTrue((Path(folder)/'artifacts/r/review-issue.json').is_file())

    def test_partial_or_edited_source_cannot_settle(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter, source, records, task = self._settlement_fixture(folder)
            adapter.report_failure(task, 'old failure')
            adapter.ledger.rows.append({**adapter.ledger.rows[0], 'id': 8,
                'payload': {**adapter.ledger.rows[0]['payload'], '海运费USD': 900}})
            with self.assertRaisesRegex(RuntimeError, '完整'):
                adapter.readback(source, records)
            self.assertEqual(adapter.ledger.settled, [])
            adapter.ledger.rows = []
            with self.assertRaisesRegex(RuntimeError, '缺'):
                adapter.readback(source, records)
            self.assertEqual(adapter.ledger.settled, [])

    def test_report_failure_without_ack_does_not_authorize_settlement(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as folder:
            adapter, source, records, task = self._settlement_fixture(folder)
            adapter.ledger.report_review_issues = Mock(return_value={'ok': False})
            adapter.report_failure(task, 'not acknowledged')
            self.assertFalse((Path(folder)/'artifacts/r/review-issue.json').exists())
            self.assertTrue(adapter.readback(source, records)['ok'])
            self.assertEqual(adapter.ledger.settled, [])

    def test_settlement_rejects_changed_report_original_text(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter, source, records, task = self._settlement_fixture(folder)
            adapter.report_failure(task, 'old failure')
            with self.assertRaisesRegex(RuntimeError, '来源'):
                adapter.settle_reported_issue({**source, 'text': 'edited'}, records, [7])
            self.assertEqual(adapter.ledger.settled, [])

    def test_old_queued_media_owner_rebuilt_from_immutable_pull(self):
        import json
        from freight_flow.services.production import source_for_message
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            store = Store(folder/'c.db')
            resource = {'resourceId':'@img','type':'mediaId', 'download':{'arguments':{
                'message-id':'original','open-conversation-id':'c','resource-id':'@img'}}}
            message = {'id':'reply','createTime':'2026-10-10 12:00:00','content':'reply',
                       'resourceRefs':[resource], 'quotedMessage':{'id':'original','content':'[image]'}}
            source = source_for_message({'cid':'c'}, message, 'rules')
            old = {**source, 'media_messages':{'@img':'reply'}}
            parent = store.enqueue('p','group:g',{},['pull'])
            child = store.enqueue('c','message:g:reply',old,['prepare'])
            store.depend(parent,child)
            with store.connect() as db:
                db.execute("UPDATE tasks SET status='SUCCEEDED',result=? WHERE run_id=?", (json.dumps({'messages':[message]}),parent))
            adapter = Production(PROJECT,folder,store,None,ledger=FakeLedger())
            adapter.execution.task = {'run_id':child}
            fixed = adapter.media_source(old,folder)
            self.assertEqual(fixed['message_id'],'reply')
            self.assertEqual(fixed['media_messages'],{'@img':'original'})
            self.assertEqual(old['media_messages'],{'@img':'reply'})
            self.assertTrue((folder/'media-owner-recovery.json').exists())
            with self.assertRaises(ValueError):
                adapter.media_source({**old,'text':'altered'},folder)

    def test_merged_parent_resources_keep_verified_quoted_owner(self):
        from freight_flow.services.production import source_for_message
        resource={'resourceId':'@image','resourceIdType':'mediaId',
                  'download':{'arguments':{'resource-id':'@image','message-id':'original','open-conversation-id':'c'}}}
        message={'id':'reply','createTime':'2026-10-10 10:00:00','content':'reply',
                 'resourceRefs':[resource], 'quotedMessage':{'id':'original','content':'image','resourceRefs':[resource]}}
        source=source_for_message({'cid':'c'},message,'rules')
        self.assertEqual(source['message_id'],'reply')
        self.assertEqual(source['media_messages']['@image'],'original')
        self.assertEqual(source['display_media_ids'],['@image'])
        self.assertEqual(source['parse_media_ids'],[])
        self.assertEqual(source['quoted_media_ids'],['@image'])
        resource['download']['arguments']['open-conversation-id']='other-group'
        with self.assertRaises(ValueError):
            source_for_message({'cid':'c'},message,'rules')

    def test_source_rows_preserve_distinct_bills_without_hiding_duplicates(self):
        from freight_flow.services.production import merge_products, records_for_ledger
        from freight_flow.services.workflow import coverage
        base = {'_table':'space','起运港':'Ningbo','目的港':'Callao','船司':'EMC',
                '柜型':'40HQ/HC','海运费USD':'','箱量':'1','箱型箱量':'1*40HQ'}
        rows = merge_products([{**base,'_source_row':'bill:EGLV123'}, {**base,'_source_row':'bill:EGLV456'}])
        cells = [{'起运港':'Ningbo','pod':'Callao','container':'40HQ/HC','price':'','箱量':'1','source_row':r['_source_row']} for r in rows]
        self.assertTrue(coverage(rows,cells)['ok'])
        source={'message_id':'m','text':'raw','message_time':'2026-10-10 12:00:00','sender':'s','group':{'name':'g','cid':'c'}}
        converted=records_for_ledger(source,rows)['space']
        self.assertEqual(converted[0]['_source_row'],'bill:EGLV123')
        self.assertEqual(converted[1]['_source_row'],'bill:EGLV456')
        self.assertNotIn('备注',converted[0])
        with self.assertRaises(ValueError):
            merge_products([rows[0],rows[0]])
        with self.assertRaises(ValueError):
            coverage(rows,[{**c,'source_row':'invented'} for c in cells])

    def test_distinct_space_allocations_are_not_duplicate_quotes(self):
        from freight_flow.services.production import merge_products
        from freight_flow.services.workflow import coverage
        base = {'_table':'space','起运港':'Xingang','目的港':'Callao','船司':'CMA',
                '柜型':'40NOR','海运费USD':'','ETD':''}
        rows = merge_products([{**base,'箱量':'1','箱型箱量':'1*40NOR'}, {**base,'箱量':'3','箱型箱量':'3*40NOR'}])
        cells = [{'起运港':'Xingang','pod':'Callao','container':'40NOR','price':'','箱量':r['箱量']} for r in rows]
        self.assertTrue(coverage(rows,cells)['ok'])
        self.assertEqual(len(rows),2)
        with self.assertRaises(ValueError):
            coverage(rows, [{**cell,'箱量':'9'} for cell in cells])

    def test_source_recheck_preserves_evidence_and_repair_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder)/'c.db')
            run = store.enqueue('k', 'message:g:m', {}, ['prepare','extract','inventory','coverage','audit','submit'])
            with store.connect() as db:
                db.execute("UPDATE tasks SET status='SUCCEEDED',result='{}' WHERE run_id=? AND stage in ('prepare','extract','inventory')", (run,))
                db.execute("UPDATE tasks SET status='FAILED',error='old code' WHERE run_id=? AND stage='coverage'", (run,))
                db.execute("INSERT INTO repairs VALUES(?,?,?)", (run,0,'already used'))
            store.recheck_source(run)
            tasks = {t['stage']:t for t in store.detail(run)['tasks']}
            self.assertEqual(tasks['prepare']['status'], 'SUCCEEDED')
            self.assertEqual(tasks['prepare']['result'], '{}')
            self.assertEqual(tasks['extract']['status'], 'QUEUED')
            self.assertIsNone(tasks['extract']['result'])
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM repairs WHERE run_id=?',(run,)).fetchone()[0],1)
                db.execute("UPDATE tasks SET status='SUCCEEDED' WHERE run_id=? AND stage='submit'",(run,))
                db.execute("UPDATE tasks SET status='FAILED' WHERE run_id=? AND stage='audit'",(run,))
            with self.assertRaises(ValueError):
                store.recheck_source(run)

    def test_large_image_resolves_unique_names_in_bounded_batches(self):
        class BoundedLedger(FakeLedger):
            def resolve_names(self, group, items):
                if len(items) > 200:
                    return {'ok': False, 'error': 'items limit'}
                return super().resolve_names(group, items)
        with tempfile.TemporaryDirectory() as folder:
            adapter = Production(PROJECT, Path(folder), Store(Path(folder)/'c.db'), None, ledger=BoundedLedger())
            base = {'起运港': 'Ningbo', '柜型': '20GP', '海运费USD': 500, '_table': 'rate',
                    'ETD': '', '有效期开始': '2026/10/17', '有效期截止': '2026/10/18'}
            records = [{**base, '目的港': 'P'+str(i)} for i in range(220)]
            cells = [{'起运港': 'Ningbo', 'pod': r['目的港'], 'container': '20GP', 'price': 500,
                      '有效期开始': '2026-10-17', '有效期截止': '2026-10-18'} for r in records]
            result = adapter.compare_source_cells({'group': {'name': 'test'}, 'message_time': '2026-10-10 12:00:00'},
                                                 {'extract': {'records': records}, 'inventory': {'cells': cells}})
            self.assertTrue(result['ok'])
            self.assertEqual(len(result['records']), 220)

    def test_dws_auth_failure_keeps_safe_actionable_reason(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        result = SimpleNamespace(returncode=5, stdout='', stderr='{"error":{"code":5,"message":"resolve access token: 旧版登录态已无法刷新；请重新运行 dws auth login; secret=test"}}')
        with tempfile.TemporaryDirectory() as folder:
            adapter = object.__new__(Production)
            adapter.dws = 'dws'
            adapter.runtime = Path(folder)
            with patch('freight_flow.transport.production.subprocess.run', return_value=result):
                with self.assertRaisesRegex(RuntimeError, '重新授权') as raised:
                    adapter._dws([], Path('.'))
            self.assertNotIn('secret', str(raised.exception))
            health = json.loads((Path(folder) / 'dws-health.json').read_text(encoding='utf-8'))
            self.assertEqual(health['status'], 'authorization_required')
            self.assertNotIn('secret', json.dumps(health, ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
