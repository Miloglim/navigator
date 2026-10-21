import tempfile
import unittest
from pathlib import Path

import manualdb
from ops import api_monitor
from services.api_monitor import health_snapshot
from transport.access_rules import authorize
from core.api_monitor_contracts import API_EVENT_TABLE, API_EVENTS_ROUTE, API_HEALTH_ROUTE
from core.activity_contracts import QUOTE_USAGE_TABLE


class ApiMonitorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.connection = manualdb.connect(Path(self.directory.name) / "monitor.sqlite3")
        self.connection.execute(
            "INSERT INTO operators(user_id,name,display_name,role,active) VALUES(?,?,?,?,?)",
            ("usr_sales", "sales-test", "销售测试", "sales", 1),
        )
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        self.directory.cleanup()

    def test_request_log_keeps_route_result_and_identity_without_payload(self):
        api_monitor.record_request(
            self.connection, "req-1", "POST", "/api/pending/approve", 200,
            18, "usr_sales", "sales", "10.0.0.8", "2026-10-15T10:00:00+08:00",
        )

        # Keep the assertion about the stored route/identity independent of
        # the calendar date on which the suite is run.
        result = api_monitor.events(self.connection, {"days": "30"})["data"]

        self.assertEqual(1, result["total"])
        self.assertEqual("/api/pending/approve", result["rows"][0]["path"])
        self.assertEqual(200, result["rows"][0]["status_code"])
        self.assertEqual("usr_sales", result["rows"][0]["user_id"])
        self.assertNotIn("body", result["rows"][0])
        self.assertNotIn("token", result["rows"][0])

    def test_request_summary_groups_calls_and_failures_by_route(self):
        api_monitor.record_request(self.connection, "req-1", "GET", "/api/rates", 200, 20,
                                   "usr_sales", "sales", "10.0.0.8")
        api_monitor.record_request(self.connection, "req-2", "POST", "/api/pending/approve", 500, 80,
                                   "usr_sales", "sales", "10.0.0.8")

        result = api_monitor.summary(self.connection, {"days": "30"})["data"]

        self.assertEqual(2, result["requests"])
        self.assertEqual(1, result["failures"])
        self.assertEqual("/api/pending/approve", result["routes"][0]["path"])

    def test_health_snapshot_does_not_claim_database_healthy_without_probe(self):
        healthy = health_snapshot({"connected": True, "engine": "postgresql"}, 12, 45)
        unhealthy = health_snapshot({"connected": False, "engine": "postgresql"}, 12, 45)

        self.assertEqual("ok", healthy["status"])
        self.assertEqual("blocked", unhealthy["status"])
        self.assertEqual(12, healthy["database"]["probe_ms"])

    def test_monitor_routes_are_admin_only(self):
        for path in (API_EVENTS_ROUTE, API_HEALTH_ROUTE):
            self.assertTrue(authorize("GET", path, "admin")["ok"])
            self.assertFalse(authorize("GET", path, "sales")["ok"])

    def test_old_snapshot_without_api_events_table_remains_importable(self):
        snapshot = manualdb.export_all(self.connection)
        snapshot.pop(API_EVENT_TABLE)
        snapshot.pop(QUOTE_USAGE_TABLE)

        result = manualdb.load_snapshot(self.connection, snapshot)

        self.assertIsInstance(result, str)
        self.assertIsNotNone(self.connection.execute(
            f"SELECT 1 FROM sqlite_master WHERE type='table' AND name='{API_EVENT_TABLE}'").fetchone())


if __name__ == "__main__":
    unittest.main()
