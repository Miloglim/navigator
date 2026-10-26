# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen

from freight_flow.transport.daemon import service_status, start_service, stop_service


class ServiceControlTests(unittest.TestCase):
    def test_detached_service_start_status_and_stop_without_scheduler_or_dws(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            schedule = root / "schedule.json"
            schedule.write_text(json.dumps({
                "enabled": False,
                "tick_seconds": 1,
                "weekdays": [1, 2, 3, 4, 5],
                "shard_offsets_seconds": {"A": 0, "B": 300},
                "windows": [{"start": "09:00", "end": "18:59", "interval_seconds": 3600}],
            }), encoding="utf-8")
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            started = None
            try:
                started = start_service(
                    Path(__file__).parent / "orchestrator.py",
                    root,
                    "codex-not-invoked-in-disabled-schedule",
                    "test-model",
                    "dws-not-invoked-in-disabled-schedule",
                    schedule,
                    port,
                    1,
                )
                self.assertTrue(started["started"])
                status = service_status(root)
                self.assertEqual(status["state"], "running")
                self.assertEqual(status["data"]["workers"], 1)
                self.assertFalse(status["data"]["schedule"]["enabled"])
                with urlopen(f"http://127.0.0.1:{port}/api/health") as response:
                    health = json.load(response)["data"]
                self.assertEqual(health["mode"], "service")
                self.assertTrue(health["worker_alive"])
            finally:
                if started:
                    stopped = stop_service(root)
                    self.assertTrue(stopped["stopped"])
                    self.assertEqual(service_status(root)["state"], "stopped")


if __name__ == "__main__":
    unittest.main()
