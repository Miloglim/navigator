import tempfile
import unittest
from pathlib import Path

import manualdb
import manual_server
from core.feedback_contracts import FEEDBACK_DISMISS_ROUTE, FEEDBACK_ROUTE
from ops import item_feedback
from transport.access_rules import authorize


class ItemFeedbackTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = manualdb.connect(Path(self.temp.name) / "feedback.db")
        self.conn.execute(
            "INSERT INTO freight_items(entry_id,content_key,record_type,pol,pod,carrier,container_type,price_usd,status) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("frt_test", "feedback-test", "rates", "SHA", "HAM", "MSC", "40HQ", 2650, "当前生效"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_suggestion_preserves_quote_and_can_be_withdrawn(self):
        created = item_feedback.create(self.conn, "frt_test",
            {"price_usd": "2550", "destination_terminal": "CTA"},
            {"price_usd": "2650", "destination_terminal": ""}, "客户确认", "usr_sales", "销售甲", "2026-09-30 12:00:00")
        self.assertEqual(len(item_feedback.read(self.conn, "frt_test")["feedback"]), 2)
        self.assertEqual(self.conn.execute("SELECT price_usd FROM freight_items WHERE entry_id='frt_test'").fetchone()[0], 2650)
        with self.assertRaises(PermissionError):
            item_feedback.dismiss(self.conn, created["submission_id"], "withdraw", "usr_other", "sales", "2026-09-30 12:01:00")
        item_feedback.dismiss(self.conn, created["submission_id"], "withdraw", "usr_sales", "sales", "2026-09-30 12:01:00")
        self.assertEqual(item_feedback.read(self.conn, "frt_test")["feedback"], [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM item_feedback WHERE status='withdrawn'").fetchone()[0], 2)

    def test_author_avatar_is_whitelisted_and_optional(self):
        self.conn.execute(
            "INSERT INTO operators(user_id,name,display_name,role,preferences_json,active) VALUES(?,?,?,?,?,1)",
            ("usr_sales", "sales-login", "销售甲", "sales", '{"avatar_id":"av2-w01-1"}'))
        self.conn.commit()
        item_feedback.create(self.conn, "frt_test", {"price_usd": "2550"},
                             {"price_usd": "2650"}, "客户确认", "usr_sales", "销售甲", "2026-09-30 12:00:00")
        row = item_feedback.read(self.conn, "frt_test")["feedback"][0]
        self.assertEqual(row["author_avatar_id"], "av2-w01-1")
        self.assertEqual(row["author_name"], "销售甲")
        self.assertNotIn("preferences_json", row)
        previous = dict(manual_server.CFG)
        manual_server.CFG.update(db=str(Path(self.temp.name) / "feedback.db"), backend="sqlite")
        try:
            flat = manual_server.act_flat("rates", {"limit": "10"})
        finally:
            manual_server.CFG.update(previous)
        self.assertEqual(flat["rows"][0]["feedback"][0]["author_avatar_id"], "av2-w01-1")
        self.conn.execute("UPDATE operators SET preferences_json=? WHERE user_id=?",
                          ('{"avatar_id":"../../private"}', "usr_sales"))
        self.conn.commit()
        self.assertIsNone(item_feedback.read(self.conn, "frt_test")["feedback"][0]["author_avatar_id"])
        self.conn.execute("UPDATE operators SET preferences_json=? WHERE user_id=?",
                          ("not-json", "usr_sales"))
        self.conn.commit()
        self.assertIsNone(item_feedback.read(self.conn, "frt_test")["feedback"][0]["author_avatar_id"])
        self.conn.execute("DELETE FROM operators WHERE user_id=?", ("usr_sales",))
        self.conn.commit()
        row = item_feedback.read(self.conn, "frt_test")["feedback"][0]
        self.assertIsNone(row["author_avatar_id"])
        self.assertEqual(row["author_name"], "销售甲")

    def test_reject_conflict_stale_and_forbidden_fields(self):
        first = item_feedback.create(self.conn, "frt_test", {"price_usd": "2550"},
                                     {"price_usd": "2650"}, "依据", "usr_a", "销售甲", "2026-09-30 12:00:00")
        with self.assertRaises(ValueError):
            item_feedback.create(self.conn, "frt_test", {"price_usd": "2400"},
                                 {"price_usd": "2650"}, "依据", "usr_b", "销售乙", "2026-09-30 12:01:00")
        with self.assertRaises(ValueError):
            item_feedback.create(self.conn, "frt_test", {"message_text": "伪造"},
                                 {"message_text": ""}, "依据", "usr_a", "销售甲", "2026-09-30 12:01:00")
        with self.assertRaises(ValueError):
            item_feedback.create(self.conn, "frt_test", {"pod": "ROT"},
                                 {"pod": "OLD"}, "依据", "usr_a", "销售甲", "2026-09-30 12:01:00")
        with self.assertRaises(PermissionError):
            item_feedback.dismiss(self.conn, first["submission_id"], "reject", "usr_a", "sales", "2026-09-30 12:01:00")
        item_feedback.dismiss(self.conn, first["submission_id"], "reject", "usr_ops", "operator", "2026-09-30 12:02:00")
        self.assertEqual(item_feedback.read(self.conn, "frt_test")["feedback"], [])

    def test_role_routes(self):
        self.assertTrue(authorize("GET", FEEDBACK_ROUTE, "sales")["ok"])
        self.assertTrue(authorize("POST", FEEDBACK_ROUTE, "sales")["ok"])
        self.assertTrue(authorize("POST", FEEDBACK_DISMISS_ROUTE, "operator")["ok"])
        self.assertFalse(authorize("POST", FEEDBACK_ROUTE, agent=True)["ok"])
        self.assertFalse(authorize("POST", FEEDBACK_ROUTE)["ok"])

    def test_formal_price_change_makes_old_feedback_stale(self):
        item_feedback.create(self.conn, "frt_test", {"price_usd": "2550"},
                             {"price_usd": "2650"}, "依据", "usr_a", "销售甲", "2026-09-30 12:00:00")
        self.conn.execute("UPDATE freight_items SET price_usd=2600 WHERE entry_id='frt_test'")
        self.conn.commit()
        self.assertTrue(item_feedback.read(self.conn, "frt_test")["feedback"][0]["stale"])
        item_feedback.create(self.conn, "frt_test", {"price_usd": "2500"},
                             {"price_usd": "2600"}, "新依据", "usr_b", "销售乙", "2026-09-30 12:05:00")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM item_feedback WHERE status='stale'").fetchone()[0], 1)
        self.assertEqual(item_feedback.read(self.conn, "frt_test")["feedback"][0]["suggested_value"], "2500")

    def test_sales_queries_attach_only_relevant_entry_feedback(self):
        item_feedback.create(self.conn, "frt_test", {"price_usd": "2550"},
                             {"price_usd": "2650"}, "依据", "usr_a", "销售甲", "2026-09-30 12:00:00")
        previous = dict(manual_server.CFG)
        manual_server.CFG.update(db=str(Path(self.temp.name) / "feedback.db"), backend="sqlite")
        try:
            flat = manual_server.act_flat("rates", {"limit": "10"})
            grouped = manual_server.act_grouped("rates", {"limit": "10"})
        finally:
            manual_server.CFG.update(previous)
        self.assertEqual(flat["rows"][0]["feedback"][0]["suggested_value"], "2550")
        quote = grouped["groups"][0]["current"]
        self.assertEqual(quote["feedback_by_entry"]["frt_test"][0]["field"], "price_usd")
        self.assertEqual(quote["cab"]["40HQ/HC"], 2650)


if __name__ == "__main__":
    unittest.main()
