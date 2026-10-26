# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen

from freight_flow.services.schedule import CN_TZ, due_triggers, next_slots
from freight_flow.transport.daemon import service_status
from freight_flow.transport.engine import EngineService
from freight_flow.transport.store import Store


CONFIG = {
    "enabled": True,
    "tick_seconds": 30,
    "weekdays": [1, 2, 3, 4, 5],
    "shard_offsets_seconds": {"A": 0, "B": 300, "C": 600, "D": 900},
    "windows": [
        {"start": "09:00", "end": "13:59", "interval_seconds": 3600},
        {"start": "14:00", "end": "18:59", "interval_seconds": 600},
    ],
}


def at(hour: int, minute: int, day: int = 19) -> float:
    return datetime(2026, 10, day, hour, minute, tzinfo=CN_TZ).timestamp()


class ScheduleTests(unittest.TestCase):
    def test_morning_a_and_b_shifts_deduplicate_slot(self):
        self.assertEqual(due_triggers(at(9, 0), CONFIG), {"A": "engine:20261019-090000:A"})
        self.assertEqual(due_triggers(at(9, 6), CONFIG), {
            "A": "engine:20261019-090000:A", "B": "engine:20261019-090500:B"})
        self.assertEqual(due_triggers(at(9, 29), CONFIG)["A"], "engine:20261019-090000:A")

    def test_four_shards_are_spaced_within_the_same_window(self):
        self.assertEqual(due_triggers(at(9, 16), CONFIG), {
            "A": "engine:20261019-090000:A",
            "B": "engine:20261019-090500:B",
            "C": "engine:20261019-091000:C",
            "D": "engine:20261019-091500:D",
        })
        self.assertEqual(due_triggers(at(9, 16, day=17), CONFIG), {})

    def test_afternoon_ten_minute_slots(self):
        self.assertEqual(due_triggers(at(14, 7), CONFIG), {
            "A": "engine:20261019-140000:A", "B": "engine:20261019-140500:B"})
        self.assertEqual(due_triggers(at(14, 13), CONFIG)["A"], "engine:20261019-141000:A")
        self.assertEqual(due_triggers(at(14, 13), CONFIG)["B"], "engine:20261019-140500:B")

    def test_weekend_and_after_hours_are_idle(self):
        self.assertEqual(due_triggers(at(9, 0, day=17), CONFIG), {})
        self.assertEqual(due_triggers(at(19, 0), CONFIG), {})

    def test_next_slots_cross_window_and_weekend(self):
        projected = next_slots(at(14, 13), CONFIG)
        self.assertEqual(projected["A"]["trigger"], "engine:20261019-142000:A")
        self.assertEqual(projected["B"]["trigger"], "engine:20261019-141500:B")
        self.assertEqual(projected["C"]["trigger"], "engine:20261019-142000:C")
        self.assertEqual(projected["D"]["trigger"], "engine:20261019-141500:D")
        monday = next_slots(at(18, 59, day=16), CONFIG)
        self.assertEqual(monday["A"]["trigger"], "engine:20261019-090000:A")
        self.assertEqual(monday["B"]["trigger"], "engine:20261019-090500:B")
        self.assertEqual(monday["C"]["trigger"], "engine:20261019-091000:C")
        self.assertEqual(monday["D"]["trigger"], "engine:20261019-091500:D")

    def test_engine_enqueues_due_shards(self):
        class FakeProduction:
            def __init__(self):
                self.calls = []
            def enqueue(self, shard, trigger):
                self.calls.append((shard, trigger))
                return [shard.lower()]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "schedule.json"
            path.write_text(json.dumps(CONFIG), encoding="utf-8")
            production = FakeProduction()
            engine = EngineService(Path(folder), None, production, path, "test-model")
            result = engine.enqueue_due(at(14, 7))
        self.assertEqual(result, {"A": ["a"], "B": ["b"]})
        self.assertEqual([call[0] for call in production.calls], ["A", "B"])

    def test_engine_does_not_repeat_already_enqueued_slot(self):
        class FakeProduction:
            def __init__(self):
                self.calls = []
            def enqueue(self, shard, trigger):
                self.calls.append((shard, trigger))
                return [shard.lower()]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "schedule.json"
            path.write_text(json.dumps(CONFIG), encoding="utf-8")
            store = Store(root / "control.sqlite3")
            store.enqueue("engine:20261019-140000:A:group", "group:A", {}, ("pull",))
            production = FakeProduction()
            engine = EngineService(root, store, production, path, "test-model")
            self.assertEqual(engine.enqueue_due(at(14, 7)), {"B": ["b"]})
        self.assertEqual([call[0] for call in production.calls], ["B"])

    def test_engine_recognizes_coalesced_trigger_alias(self):
        class FakeProduction:
            def __init__(self):
                self.calls = []
            def enqueue(self, shard, trigger):
                self.calls.append((shard, trigger))
                return [shard]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "schedule.json"
            path.write_text(json.dumps(CONFIG), encoding="utf-8")
            store = Store(root / "control.sqlite3")
            original = store.enqueue("manual-origin", "group:A", {}, ("pull",))
            with store.connect() as db:
                db.execute("INSERT INTO trigger_aliases VALUES(?,?)",
                           ("engine:20261019-140000:A:group", original))
            production = FakeProduction()
            engine = EngineService(root, store, production, path, "test-model")
            self.assertEqual(engine.enqueue_due(at(14, 7)), {"B": ["B"]})
        self.assertEqual([call[0] for call in production.calls], ["B"])

    def test_engine_service_is_observable_and_stops_cleanly(self):
        class FakeProduction:
            def enqueue(self, shard, trigger):
                return []

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            schedule = root / "schedule.json"
            schedule.write_text(json.dumps({**CONFIG, "enabled": False, "tick_seconds": 1}), encoding="utf-8")
            store = Store(root / "control.sqlite3")
            engine = EngineService(root, store, FakeProduction(), schedule, "test-model", concurrency=2)
            self.assertEqual(store.claim_limit, 2)
            thread = threading.Thread(
                target=engine.serve_forever,
                args=(Path(__file__).parent / "web/index.html", 0),
                daemon=True,
            )
            thread.start()
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and service_status(root)["state"] != "running":
                    time.sleep(0.02)
                status = service_status(root)
                self.assertEqual(status["state"], "running")
                self.assertEqual(status["data"]["workers"], 2)
                self.assertEqual(status["data"]["worker_threads"], [
                    {"name": "engine-worker-1", "alive": True},
                    {"name": "engine-worker-2", "alive": True},
                ])
                self.assertEqual(status["data"]["queue"]["active"], 0)
                self.assertEqual(status["data"]["dws"]["status"], "unknown")
                self.assertEqual(status["data"]["schedule"]["next_slots"], {})
                with urlopen(f"http://127.0.0.1:{engine.http_server.server_port}/api/health") as response:
                    health = json.load(response)["data"]
                self.assertEqual(health["mode"], "service")
                self.assertTrue(health["worker_alive"])
                (root / "worker-stop.request").write_text("{}", encoding="utf-8")
                thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(service_status(root)["state"], "stopped")
                self.assertFalse((root / "worker-health.json").exists())
                self.assertFalse((root / "worker-stop.request").exists())
            finally:
                if thread.is_alive():
                    (root / "worker-stop.request").write_text("{}", encoding="utf-8")
                    thread.join(3)

    def test_engine_agent_model_updates_without_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            schedule = root / "schedule.json"
            schedule.write_text(json.dumps({**CONFIG, "enabled": False}), encoding="utf-8")

            class Agent:
                model = "old-model"
                codex = "old-codex"
                def configure(self, model=None, codex=None):
                    self.model = model or self.model
                    self.codex = codex or self.codex

            agent = Agent()
            production = SimpleNamespace(agent=agent, dws="dws")
            engine = EngineService(root, Store(root / "control.sqlite3"), production,
                                   schedule, "old-model", workers_enabled=False)
            engine.configure_agent("new-model", "new-codex")
            self.assertEqual(engine.model, "new-model")
            self.assertEqual(agent.model, "new-model")
            self.assertEqual(agent.codex, "new-codex")
            (root / "runtime.json").write_text(json.dumps({
                "schema_version": 1,
                "worker": {"enabled": False, "model": "file-model", "concurrency": 1},
                "executables": {"codex_exe": "file-codex", "dws_exe": "dws"},
            }), encoding="utf-8")
            engine._reload_agent_config()
            self.assertEqual(agent.model, "file-model")
            self.assertEqual(agent.codex, "file-codex")


if __name__ == "__main__":
    unittest.main()
