import sys
import tempfile
import unittest
from pathlib import Path

from agent_watch.transport.orchestrator import OrchestratorReader

PROJECT = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT / 'ops' / 'freight-orchestrator'))
from freight_flow.transport.store import Store


class OrchestratorHealthTests(unittest.TestCase):
    def test_full_queue_reveals_old_failed_lane_and_keeps_recent_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'control.sqlite3')
            old = store.enqueue('old', 'group:one', {}, ['pull'], mode='production-batch')
            message = store.enqueue('message', 'message:one', {}, ['extract'], mode='production-message')
            store.depend(old, message)
            with store.connect() as db:
                db.execute("UPDATE tasks SET status='FAILED' WHERE run_id=?", (message,))
                store.event(db, old, 'pull', 'SUCCEEDED', 123)
            for i in range(101):
                store.enqueue(str(i), 'group:one', {}, ['pull'], mode='replay')
            # Emulate pre-upgrade history; the read-only monitor must not compact it.
            with store.connect() as db:
                db.execute("UPDATE runs SET mode='production-batch' WHERE mode='replay'")
            reader = OrchestratorReader(PROJECT)
            reader.store = Store(store.path, readonly=True)
            before = Path(store.path).read_bytes()
            health = reader.health()
            self.assertEqual(health['queue']['batches']['BLOCKED'], 1)
            self.assertEqual(health['queue']['batches']['QUEUED'], 101)
            self.assertEqual(health['queue']['messages']['FAILED'], 1)
            self.assertEqual(health['queue']['lane_blocked_batches'], 101)
            self.assertEqual(health['queue']['running_tasks'], 0)
            self.assertEqual(health['queue']['last_progress_at'], 123)
            self.assertEqual(health['business_state'], 'blocked')
            self.assertEqual(len(reader.runs()), 100)
            self.assertEqual(Path(store.path).read_bytes(), before)

    def test_empty_missing_database_is_idle_and_never_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            reader = OrchestratorReader(PROJECT)
            reader.store = Store(Path(tmp) / 'missing.sqlite3', readonly=True)
            self.assertEqual(reader.health()['business_state'], 'idle')
            self.assertFalse(Path(reader.store.path).exists())

    def test_replay_failures_do_not_mark_production_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'control.sqlite3')
            run = store.enqueue('replay', 'test', {}, ['extract'])
            with store.connect() as db:
                db.execute("UPDATE tasks SET status='FAILED' WHERE run_id=?", (run,))
            reader = OrchestratorReader(PROJECT)
            reader.store = Store(store.path, readonly=True)
            self.assertEqual(reader.health()['business_state'], 'idle')

    def test_running_waiting_and_done_are_distinct_and_queue_events_are_not_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'control.sqlite3')
            run = store.enqueue('batch', 'group:test', {}, ['pull'], mode='production-batch')
            reader = OrchestratorReader(PROJECT)
            reader.store = Store(store.path, readonly=True)
            self.assertEqual(reader.health()['business_state'], 'waiting')
            self.assertIsNone(reader.health()['queue']['last_progress_at'])
            with store.connect() as db:
                db.execute("UPDATE tasks SET status='RUNNING' WHERE run_id=?", (run,))
            self.assertEqual(reader.health()['business_state'], 'running')
            self.assertEqual(reader.health()['queue']['running_tasks'], 1)
            with store.connect() as db:
                db.execute("UPDATE tasks SET status='SUCCEEDED' WHERE run_id=?", (run,))
                store.event(db, run, 'pull', 'SUCCEEDED', 456)
            self.assertEqual(reader.health()['business_state'], 'idle')
            self.assertEqual(reader.health()['queue']['batches']['DONE'], 1)
            self.assertEqual(reader.health()['queue']['last_progress_at'], 456)

    def test_production_mode_acceptance_lanes_do_not_pollute_business_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'control.sqlite3')
            batch = store.enqueue('real', 'group:real', {}, ['pull'], mode='production-batch')
            test_message = store.enqueue('test-message', 'production-test:message', {}, ['extract'], mode='production-message')
            test_batch = store.enqueue('test-batch', 'production-test:batch', {}, ['pull'], mode='production-batch')
            with store.connect() as db:
                db.execute("UPDATE tasks SET status='SUCCEEDED' WHERE run_id=?", (batch,))
                db.execute("UPDATE tasks SET status='FAILED' WHERE run_id IN (?,?)", (test_message, test_batch))
                store.event(db, batch, 'pull', 'SUCCEEDED', 123)
                store.event(db, test_message, 'extract', 'SUCCEEDED', 999)
            reader = OrchestratorReader(PROJECT)
            reader.store = Store(store.path, readonly=True)
            health = reader.health()
            self.assertEqual(health['business_state'], 'idle')
            self.assertEqual(health['queue']['batches']['DONE'], 1)
            self.assertEqual(health['queue']['batches']['FAILED'], 0)
            self.assertEqual(health['queue']['messages']['FAILED'], 0)
            self.assertEqual(health['queue']['last_progress_at'], 123)
            self.assertEqual(len(reader.runs()), 3, 'acceptance runs stay available in optional development list')


if __name__ == '__main__':
    unittest.main()
