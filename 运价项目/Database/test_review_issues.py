"""Source-level review issues remain separate from releasable pending records."""
import sqlite3
import unittest

import manualdb
from transport.access_rules import authorize


class ReviewIssuesTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        manualdb.ensure_review_issue_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_upsert_is_idempotent_and_resolved_issue_stays_resolved(self):
        issue = {"source_group": "turkey_price", "message_id": "msg-1",
                 "message_text": "原文", "media_ids": ["@image"], "reasons": ["缺少OCR"]}
        self.assertEqual(manualdb.upsert_review_issues(self.conn, [issue]), {"count": 1})
        manualdb.upsert_review_issues(self.conn, [issue])
        listed = manualdb.list_review_issues(self.conn)
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["rows"][0]["media_ids"], ["@image"])
        self.assertTrue(manualdb.resolve_review_issue(self.conn, "turkey_price", "msg-1", "航管")["resolved"])
        manualdb.upsert_review_issues(self.conn, [dict(issue, reasons=["重试"])])
        self.assertEqual(manualdb.list_review_issues(self.conn)["total"], 0)
        self.assertEqual(manualdb.list_review_issues(self.conn, "resolved")["rows"][0]["reasons"], ["缺少OCR"])

    def test_permission_boundary(self):
        self.assertTrue(authorize("POST", "/api/review-issues", agent=True)["ok"])
        self.assertFalse(authorize("GET", "/api/review-issues", role="sales")["ok"])
        self.assertFalse(authorize("POST", "/api/review-issues/resolve", agent=True)["ok"])
        self.assertTrue(authorize("GET", "/api/review-issues", role="operator")["ok"])
        self.assertTrue(authorize("POST", "/api/review-issues/resolve", role="operator")["ok"])
        self.assertTrue(authorize("POST", "/api/agent/review-issues/settle", agent=True)["ok"])
        self.assertFalse(authorize("POST", "/api/agent/review-issues/settle", role="sales")["ok"])

    def test_settlement_requires_complete_matching_pending_receipt(self):
        import json
        self.conn.execute("CREATE TABLE pending_records(id INTEGER PRIMARY KEY,payload TEXT,review_status TEXT)")
        manualdb.upsert_review_issues(self.conn, [{"source_group":"g","message_id":"m",
                                                 "message_text":"source","reasons":["unread"]}])
        for number in (1, 2):
            self.conn.execute("INSERT INTO pending_records VALUES(?,?,?)", (number,json.dumps({"消息ID":"m","消息原文":"source"}),"awaiting"))
        self.conn.commit()
        with self.assertRaises(ValueError):
            manualdb.settle_review_issue(self.conn, {"source_group":"g","message_id":"m","pending_ids":[1],"expected_count":1})
        self.assertEqual(manualdb.list_review_issues(self.conn)["total"], 1)
        result = manualdb.settle_review_issue(self.conn, {"source_group":"g","message_id":"m","pending_ids":[1,2],"expected_count":2})
        self.assertEqual(result, {"resolved":True,"pending_count":2})
        self.assertEqual(manualdb.list_review_issues(self.conn)["total"], 0)

    def test_non_quote_requires_matching_evidence_and_no_existing_quotes(self):
        self.conn.execute("CREATE TABLE pending_records(id INTEGER PRIMARY KEY,payload TEXT,review_status TEXT)")
        manualdb.upsert_review_issues(self.conn, [{"source_group":"g","message_id":"m",
            "message_text":"source","media_ids":["@image"],"reasons":["unread"]}])
        body = {"source_group":"g","message_id":"m","classification":"non_quote",
                "reason":"only free-time conditions","media_ids":[],"pending_ids":[],"expected_count":0}
        with self.assertRaises(ValueError):
            manualdb.settle_review_issue(self.conn, body)
        body["media_ids"] = ["@image"]
        self.assertTrue(manualdb.settle_review_issue(self.conn, body)["resolved"])


if __name__ == "__main__":
    unittest.main()
