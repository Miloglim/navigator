"""Message-board persistence, validation and role access regressions."""
import tempfile
import unittest
from pathlib import Path

import manualdb
from core.message_board_contracts import MESSAGE_BOARD_CONTENT_MAX
from transport.access_rules import authorize
from transport.message_board_routes import build_message_board_routes


class MessageBoardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="message-board-")
        self.path = Path(self.temp.name) / "board.db"
        manualdb.connect(str(self.path)).close()

    def tearDown(self):
        self.temp.cleanup()

    def routes(self):
        def connect():
            return manualdb.connect(str(self.path))
        return build_message_board_routes(connect, lambda: "2026-10-22 16:30:00", lambda *_: None)

    def test_publish_and_list_from_server_identity(self):
        get_routes, post_routes = self.routes()
        created = post_routes["/api/message-board/create"]({
            "content": "请补充南美东报价\n谢谢",
            "_actor_id": "usr_sales",
            "_actor": "sales-login",
            "_actor_display": "销售甲",
        })
        self.assertTrue(created["ok"])
        self.assertEqual(created["data"]["post"]["author_name"], "销售甲")
        listed = get_routes["/api/message-board"]({"limit": "30", "offset": "0"})
        self.assertTrue(listed["ok"])
        self.assertEqual(listed["data"]["total"], 1)
        self.assertEqual(listed["data"]["rows"][0]["content"], "请补充南美东报价\n谢谢")
        self.assertEqual(listed["data"]["rows"][0]["user_id"], "usr_sales")

    def test_content_is_plain_text_and_validation_is_bounded(self):
        _, post_routes = self.routes()
        create = post_routes["/api/message-board/create"]
        payload = {"content": "<script>alert(1)</script>", "_actor_id": "usr_1", "_actor_display": "用户一"}
        self.assertEqual(create(payload)["data"]["post"]["content"], "<script>alert(1)</script>")
        with self.assertRaisesRegex(ValueError, "不能为空"):
            create({**payload, "content": "   "})
        with self.assertRaisesRegex(ValueError, "不能超过"):
            create({**payload, "content": "a" * (MESSAGE_BOARD_CONTENT_MAX + 1)})

    def test_all_authenticated_roles_can_read_and_publish(self):
        for role in ("admin", "operator", "sales"):
            self.assertTrue(authorize("GET", "/api/message-board", role)["ok"])
            self.assertTrue(authorize("POST", "/api/message-board/create", role)["ok"])
        self.assertFalse(authorize("GET", "/api/message-board")["ok"])
        self.assertFalse(authorize("POST", "/api/message-board/create")["ok"])


if __name__ == "__main__":
    unittest.main()
