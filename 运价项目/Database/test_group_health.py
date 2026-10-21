# -*- coding: utf-8 -*-
"""Group-health tests for the registered source-group boundary."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import manualdb


class GroupHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = manualdb.connect(Path(self.temp.name) / "group-health.db")

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _pending(self, entry_id: str, group: str, status: str = "awaiting") -> None:
        payload = {"来源群": group, "消息ID": entry_id}
        self.conn.execute(
            "INSERT INTO pending_records "
            "(entry_id,kind,payload,origin,check_status,review_status,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (entry_id, "rates", json.dumps(payload, ensure_ascii=False), "agent",
             "passed", status, "2026-10-21 10:00:00"),
        )

    def test_only_registered_groups_with_history_are_returned(self):
        self.conn.execute(
            "INSERT INTO chat_groups(name,active,source,created_at) VALUES(?,?,?,?)",
            ("已登记有数据群", 1, "manual", "2026-10-21 09:00:00"),
        )
        self.conn.execute(
            "INSERT INTO chat_groups(name,active,source,created_at) VALUES(?,?,?,?)",
            ("已登记无数据群", 1, "manual", "2026-10-21 09:00:00"),
        )
        self._pending("known-1", "已登记有数据群")
        self._pending("unknown-1", "未登记群")
        self._pending("missing-1", "")
        self.conn.commit()

        result = manualdb.group_accuracy_stats(self.conn)

        self.assertTrue(result["ok"])
        self.assertEqual([row["group"] for row in result["groups"]], ["已登记有数据群"])
        self.assertEqual(result["total_groups"], 1)
        self.assertEqual(result["groups"][0]["total"], 1)


if __name__ == "__main__":
    unittest.main()
