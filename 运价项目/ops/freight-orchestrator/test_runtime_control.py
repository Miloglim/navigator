# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from freight_flow.transport.runtime_control import DwsAuthManager, RuntimeControl
from freight_flow.transport.store import Store


class RuntimeControlTests(unittest.TestCase):
    def test_dws_status_uses_fixed_safe_projection(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = DwsAuthManager(Path(folder), "dws")
            failed = SimpleNamespace(returncode=1, stdout="", stderr="secret-token")
            with patch("freight_flow.transport.runtime_control.subprocess.run", return_value=failed):
                manager._check_now()
            snapshot = manager.snapshot()
            self.assertEqual(snapshot["state"], "unavailable")
            self.assertNotIn("secret-token", json.dumps(snapshot, ensure_ascii=False))

            authenticated = SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True, "authenticated": True}),
                stderr="",
            )
            with patch("freight_flow.transport.runtime_control.subprocess.run", return_value=authenticated):
                manager._check_now()
            self.assertEqual(manager.snapshot()["state"], "authenticated")

    def test_snapshot_projects_timer_trigger_and_worker_task(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            schedule = root / "schedule.json"
            schedule.write_text(json.dumps({
                "enabled": True,
                "tick_seconds": 10,
                "weekdays": [1, 2, 3, 4, 5],
                "shard_offsets_seconds": {"A": 0, "B": 300},
                "windows": [{"start": "00:00", "end": "23:59", "interval_seconds": 600}],
            }), encoding="utf-8")
            store = Store(root / "control.sqlite3")
            run_id = store.enqueue("engine:test:A", "group:test", {"group": {"name": "测试群"}},
                                   ("pull", "fanout"), mode="production-batch")
            task = store.claim("engine-worker-1")
            self.assertEqual(task["run_id"], run_id)
            (root / "worker-health.json").write_text(json.dumps({
                "mode": "service",
                "at": time.time(),
                "model": "test-model",
                "workers": 1,
                "workers_alive": True,
                "scheduler_alive": True,
                "worker_threads": [{"name": "engine-worker-1", "alive": True}],
                "worker_config": {"model": "test-model", "capacity": 1,
                                  "agent": "codex-subprocess",
                                  "claim_names": ["engine-worker-1"]},
                "schedule": {"last_tick_at": time.time()},
            }), encoding="utf-8")
            control = RuntimeControl(root, store, schedule, model="fallback", concurrency=2)
            with patch.object(control.dws_auth, "snapshot", return_value={"state": "not_authenticated"}):
                snapshot = control.snapshot()
        self.assertEqual(snapshot["service"]["state"], "running")
        self.assertTrue(snapshot["scheduler"]["next_slots"])
        self.assertEqual(snapshot["triggers"][0]["trigger_key"], "engine:test:A")
        self.assertEqual(snapshot["workers"]["configured"][0]["id"], "engine-worker-1")
        self.assertEqual(snapshot["workers"]["configured"][0]["state"], "busy")
        self.assertEqual(snapshot["workers"]["configured"][0]["current"]["stage"], "pull")
        self.assertEqual(snapshot["workers"]["model"], "test-model")

    def test_schedule_and_worker_settings_are_editable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            schedule = root / "schedule.json"
            schedule.write_text(json.dumps({
                "enabled": True, "tick_seconds": 10, "weekdays": [1, 2, 3, 4, 5],
                "shard_offsets_seconds": {"A": 0, "B": 300},
                "windows": [{"start": "09:00", "end": "18:59", "interval_seconds": 600}],
            }), encoding="utf-8")
            class Engine:
                def __init__(self):
                    self.worker_gate = threading.Event()
                    self.worker_gate.set()
                    self.production = SimpleNamespace(groups=lambda shard: [])
                    self.configured = None
                def set_workers_enabled(self, enabled):
                    return enabled
                def configure_agent(self, model, codex=None):
                    self.configured = (model, codex)
            engine = Engine()
            control = RuntimeControl(root, Store(root / "control.sqlite3"), schedule,
                                     model="gpt-6-sol", concurrency=2, engine=engine)
            control.dws_auth.begin_check = lambda force=False: False
            saved_schedule = control.save_schedule({
                "enabled": False, "tick_seconds": 5, "weekdays": [1, 3, 5],
                "shard_offsets_seconds": {"A": 60, "B": 360},
                "windows": [{"start": "08:30", "end": "17:30", "interval_seconds": 900}],
            })
            self.assertFalse(saved_schedule["enabled"])
            self.assertEqual(json.loads(schedule.read_text())["tick_seconds"], 5)
            saved_worker = control.save_worker({
                "enabled": False, "model": "gpt-6-sol", "concurrency": 3,
                "codex_exe": "C:/bin/codex.exe", "dws_exe": "C:/bin/dws.exe",
            })
            self.assertEqual(saved_worker["concurrency"], 3)
            self.assertEqual(engine.configured, ("gpt-6-sol", "C:/bin/codex.exe"))
            persisted = json.loads((root / "runtime.json").read_text())
            self.assertEqual(persisted["worker"]["concurrency"], 3)
            with patch.object(control.dws_auth, "snapshot", return_value={"state": "authenticated"}):
                snapshot = control.snapshot()
            self.assertEqual(snapshot["settings"]["worker"]["codex_exe"], "C:/bin/codex.exe")
            self.assertEqual(snapshot["settings"]["worker"]["concurrency"], 3)
            self.assertEqual(snapshot["settings"]["effective"]["concurrency"], 2)
            self.assertEqual(snapshot["settings"]["effective"]["model"], "gpt-6-sol")
            self.assertFalse(snapshot["workers"]["control_enabled"])
            self.assertEqual(len(snapshot["pipeline"]), 15)

    def test_manual_group_trigger_uses_registered_shard_and_worker_gate(self):
        class Production:
            def __init__(self):
                self.calls = []
            def groups(self, shard):
                return [{"name": "测试群", "slug": "test_group"}] if shard == "C" else []
            def enqueue(self, shard, trigger, only=None, message_id=None, priority=0):
                self.calls.append((shard, trigger, only, message_id, priority))
                return ["run-1"]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            schedule = root / "schedule.json"
            schedule.write_text(json.dumps({
                "enabled": False, "tick_seconds": 10, "weekdays": [1],
                "shard_offsets_seconds": {"A": 0, "B": 300, "C": 600, "D": 900},
                "windows": [{"start": "09:00", "end": "10:00", "interval_seconds": 3600}],
            }), encoding="utf-8")
            production = Production()
            gate = threading.Event()
            gate.set()
            engine = SimpleNamespace(worker_gate=gate, production=production)
            control = RuntimeControl(root, Store(root / "control.sqlite3"), schedule, engine=engine)
            result = control.trigger_group("test_group", "msg-old")
            self.assertEqual(result["group"]["shard"], "C")
            self.assertEqual(result["message_id"], "msg-old")
            self.assertEqual(production.calls[0][0], "C")
            self.assertEqual(production.calls[0][2], "test_group")
            self.assertEqual(production.calls[0][3], "msg-old")
            self.assertEqual(production.calls[0][4], 1)
            gate.clear()
            with self.assertRaisesRegex(ValueError, "Worker 当前已暂停"):
                control.trigger_group("test_group")


if __name__ == "__main__":
    unittest.main()
