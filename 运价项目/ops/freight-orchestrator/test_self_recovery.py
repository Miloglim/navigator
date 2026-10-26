import tempfile
import time
import unittest
from pathlib import Path
from freight_flow.transport.store import Store
from freight_flow.core.contracts import PRODUCTION_BATCH_STAGES, INFRA_RECOVERY_SECONDS


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'control.sqlite3')
        self.source = {'group': {'slug': 'A', 'name': 'Test group'}, 'shard': 'A'}

    def tearDown(self):
        self.tmp.cleanup()

    def batch(self, key):
        return self.store.enqueue(key, 'group:A', self.source, PRODUCTION_BATCH_STAGES, mode='production-batch')

    def test_coalesces_unstarted_polls_and_keeps_trigger_identity(self):
        first = self.batch('first')
        self.assertEqual(first, self.batch('second'))
        self.assertEqual(first, self.store.trigger('second'))
        task = self.store.claim('worker')
        follow = self.batch('third')
        self.assertNotEqual(first, follow)
        self.assertEqual(follow, self.batch('fourth'))
        self.assertEqual(len(self.store.runs()), 2)
        self.assertIsNone(self.store.claim('other'), 'same group cannot pull twice')
        self.store.finish(task, result={})

    def test_failed_message_keeps_watermark_but_releases_group_for_new_messages(self):
        parent = self.batch('first')
        for stage in ('pull', 'fanout'):
            task = self.store.claim('worker')
            self.assertEqual(task['stage'], stage)
            self.store.finish(task, result={})
        child = self.store.enqueue('source:one', 'message:A:one', {}, ('extract', 'submit', 'readback'), mode='production-message')
        self.store.depend(parent, child)
        task = self.store.claim('worker')
        self.store.finish(task, error='unsupported quote', retryable=False, terminal_status='NEEDS_REVIEW')
        later = self.batch('later')
        next_task = self.store.claim('worker')
        self.assertEqual(next_task['run_id'], later)
        self.assertEqual(self.store.detail(parent)['status'], 'NEEDS_REVIEW')
        self.assertEqual(self.store.detail(child)['status'], 'NEEDS_REVIEW')
        self.assertEqual([task['status'] for task in self.store.detail(child)['tasks']],
                         ['NEEDS_REVIEW', 'BLOCKED', 'BLOCKED'])
        self.assertEqual(self.store.results(parent), {'pull': {}, 'fanout': {}})
        self.assertEqual(self.store.detail(parent)['tasks'][-1]['status'], 'HELD')
        self.assertEqual(self.store.enqueue('source:one', 'message:A:one', {}, ('extract',), mode='production-message'), child)

    def test_unfinished_sibling_prevents_group_release(self):
        parent = self.batch('first')
        for _ in range(2):
            self.store.finish(self.store.claim('w'), result={})
        failed = self.store.enqueue('source:bad', 'message:A:bad', {}, ('extract',), mode='production-message')
        good = self.store.enqueue('source:good', 'message:A:good', {}, ('extract',), mode='production-message')
        self.store.depend(parent, failed)
        self.store.depend(parent, good)
        bad = self.store.claim('w')
        self.store.finish(bad, error='bad evidence', retryable=False)
        self.batch('next')
        task = self.store.claim('w')
        self.assertEqual(task['run_id'], good)
        self.assertIsNone(self.store.claim('other'))

    def test_infrastructure_recovers_after_cooldown_not_business_failure(self):
        run = self.store.enqueue('infra', 'message:A:infra', {}, ('prepare',), mode='production-message')
        task = self.store.claim('w')
        with self.store.connect() as db:
            db.execute('UPDATE tasks SET attempt=3 WHERE id=?', (task['id'],))
        self.store.finish(task, error='network unavailable', retryable=True)
        self.assertIsNone(self.store.claim('w'))
        recovered = self.store.claim('w', now=time.time()+INFRA_RECOVERY_SECONDS+1)
        self.assertEqual(recovered['run_id'], run)
        self.assertEqual(recovered['recoveries'], 1)
        self.assertGreater(recovered['generation'], task['generation'])
        with self.assertRaises(ValueError):
            self.store.finish(task, result={})

    def test_failed_batch_does_not_block_next_pull(self):
        old = self.batch('old')
        task = self.store.claim('w')
        self.store.finish(task, error='bad group configuration', retryable=False)
        new = self.batch('new')
        self.assertNotEqual(old, new)
        self.assertEqual(self.store.claim('w')['run_id'], new)

    def test_old_infrastructure_recovery_cannot_overlap_a_newer_group_pull(self):
        old = self.batch('old')
        task = self.store.claim('w')
        with self.store.connect() as db:
            db.execute('UPDATE tasks SET attempt=3 WHERE id=?', (task['id'],))
        self.store.finish(task, error='network', retryable=True)
        new = self.batch('new')
        active = self.store.claim('w')
        self.assertEqual(active['run_id'], new)
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET available=0 WHERE run_id=? AND status='FAILED'", (old,))
        self.assertIsNone(self.store.claim('other'))

    def test_legacy_compaction_does_not_touch_message_or_started_batch(self):
        old = self.batch('started')
        task = self.store.claim('w')
        pending = self.batch('pending')
        duplicate = self.store.enqueue('legacy', 'group:A', self.source, PRODUCTION_BATCH_STAGES)
        with self.store.connect() as db:
            db.execute("UPDATE runs SET mode='production-batch' WHERE id=?", (duplicate,))
        self.store.maintain()
        self.assertEqual(self.store.detail(duplicate)['status'], 'COALESCED')
        self.assertEqual(self.store.detail(pending)['status'], 'QUEUED')
        self.assertEqual(self.store.detail(old)['tasks'][0]['generation'], task['generation'])

    def test_held_parent_resumes_after_explicit_source_fix(self):
        parent = self.batch('parent')
        for _ in range(2):
            self.store.finish(self.store.claim('w'), result={})
        child = self.store.enqueue('source', 'message:A:source', {}, ('coverage', 'submit'), mode='production-message')
        self.store.depend(parent, child)
        self.store.finish(self.store.claim('w'), error='evidence mismatch', retryable=False,
                          terminal_status='NEEDS_REVIEW')
        self.store.maintain()
        self.assertEqual(self.store.detail(parent)['status'], 'NEEDS_REVIEW')
        self.store.resume(child)
        self.assertNotEqual(self.store.detail(parent)['status'], 'NEEDS_REVIEW')

    def test_manual_result_override_persists_guidance_and_reopens_downstream(self):
        run = self.store.enqueue('interactive', 'message:A:interactive', {},
                                 ('extract', 'inventory', 'coverage'), mode='production-message')
        first = self.store.claim('w')
        self.store.finish(first, result={'records': []})
        second = self.store.claim('w')
        self.store.finish(second, error='evidence mismatch', retryable=False,
                          terminal_status='NEEDS_REVIEW')
        self.store.amend_result(run, 'extract', {'records': []}, instruction='Local 表不是海运报价')
        detail = self.store.detail(run)
        self.assertEqual(detail['repair_guidance'], 'Local 表不是海运报价')
        self.assertEqual([task['status'] for task in detail['tasks']],
                         ['SUCCEEDED', 'QUEUED', 'QUEUED'])

    def test_mark_noise_soft_skips_blocked_chain_and_preserves_tasks(self):
        run = self.store.enqueue('noise', 'message:A:noise', {}, ('extract', 'submit'),
                                 mode='production-message')
        first = self.store.claim('w')
        self.store.finish(first, error='not a quote', retryable=False,
                          terminal_status='NEEDS_REVIEW')
        result = self.store.mark_noise(run, '群内闲聊，不含报价')
        detail = self.store.detail(run)
        self.assertEqual(result['skipped_tasks'], 2)
        self.assertEqual(detail['status'], 'DONE')
        self.assertEqual(detail['noise_resolution']['reason'], '群内闲聊，不含报价')
        self.assertEqual([task['status'] for task in detail['tasks']], ['SKIPPED', 'SKIPPED'])
        self.assertTrue(all('人工判噪' in task['error'] for task in detail['tasks']))


if __name__ == '__main__':
    unittest.main()
