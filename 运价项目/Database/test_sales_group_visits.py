"""Account-scoped source-group click ordering on an isolated SQLite database."""
import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import manualdb
import manual_server
from core.contracts import SALES_GROUP_VISIT_ROUTE
from transport.access_rules import authorize


class SalesGroupVisitTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        manualdb.init_schema(self.conn)
        self.conn.executemany(
            "INSERT INTO operators(user_id,name,role,active) VALUES(?,?,?,1)",
            [("usr_one", "sales-one", "sales"), ("usr_two", "sales-two", "sales"),
             ("usr_ops", "operator", "operator")])
        self.conn.executemany(
            "INSERT INTO freight_items(entry_id,content_key,record_type,source_group,message_time,status) "
            "VALUES(?,?,?,?,?,?)",
            [("frt_a", "a", "rates", "A group", "2026-09-29 10:00:00", "当前生效"),
             ("frt_b", "b", "space", "B group", "2026-09-30 10:00:00", "当前有效")])
        self.conn.commit()
        self.master = manualdb.Master(self.conn)

    def tearDown(self):
        self.conn.close()

    def names(self, user_id):
        result = manualdb.sales_group_summary(self.conn, self.master, {}, user_id)
        self.assertEqual(result["order"], "personal_clicks")
        return [row["name"] for row in result["rows"]]

    def test_per_account_clicks_and_tie_break(self):
        self.assertEqual(self.names("usr_one"), ["B group", "A group"])
        self.assertEqual(manualdb.record_sales_group_visit(
            self.conn, "usr_one", "A group", {}), {"click_count": 1})
        self.assertEqual(self.names("usr_one"), ["A group", "B group"])
        manualdb.record_sales_group_visit(self.conn, "usr_one", "B group", {})
        self.assertEqual(self.names("usr_one"), ["B group", "A group"])
        manualdb.record_sales_group_visit(self.conn, "usr_two", "A group", {})
        manualdb.record_sales_group_visit(self.conn, "usr_two", "A group", {})
        self.assertEqual(self.names("usr_two"), ["A group", "B group"])
        self.assertEqual(self.names("usr_one"), ["B group", "A group"])
        preferred = {"display_mode": "preferred", "display_groups": '["B group"]'}
        self.assertEqual([row["name"] for row in manualdb.sales_group_summary(
            self.conn, self.master, preferred, "usr_two")["rows"]], ["B group"])

    def test_rejects_unknown_or_inaccessible_groups(self):
        for name in ("", "missing", "A group' OR 1=1 --", "x" * 513):
            with self.subTest(name=name), self.assertRaises(ValueError):
                manualdb.record_sales_group_visit(self.conn, "usr_one", name, {})
        self.assertEqual(manualdb.record_sales_group_visit(self.conn, "usr_ops", "A group", {}),
                         {"click_count": 1})
        with self.assertRaises(ValueError):
            manualdb.record_sales_group_visit(self.conn, "usr_one", "A group", {"group":"B group", "viewer":"missing"})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sales_group_visits").fetchone()[0], 1)

    def test_snapshot_and_access_policy(self):
        manualdb.record_sales_group_visit(self.conn, "usr_one", "A group", {})
        snapshot = manualdb.export_all(self.conn)
        self.assertEqual(snapshot["sales_group_visits"][0]["user_id"], "usr_one")
        self.assertEqual(authorize("POST", SALES_GROUP_VISIT_ROUTE, "sales")["ok"], True)
        self.assertEqual(authorize("POST", SALES_GROUP_VISIT_ROUTE, agent=True)["ok"], False)
        self.assertEqual(authorize("POST", SALES_GROUP_VISIT_ROUTE)["ok"], False)
        other = sqlite3.connect(":memory:")
        other.row_factory = sqlite3.Row
        try:
            manualdb.init_schema(other)
            manualdb.load_snapshot(other, snapshot)
            self.assertEqual(other.execute("SELECT click_count FROM sales_group_visits ")
                             .fetchone()[0], 1)
        finally:
            other.close()

    def test_account_deletion_clears_only_its_own_clicks(self):
        manualdb.record_sales_group_visit(self.conn, "usr_one", "A group", {})
        manualdb.record_sales_group_visit(self.conn, "usr_two", "A group", {})
        self.assertEqual(manualdb.delete_named(self.conn, "operators", "sales-one"), 1)
        self.assertEqual([r["user_id"] for r in self.conn.execute(
            "SELECT user_id FROM sales_group_visits")], ["usr_two"])


class SalesGroupVisitHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db = Path(self.temp.name) / "test.db"
        conn = manualdb.connect(db)
        try:
            conn.executemany(
                "INSERT INTO operators(user_id,name,role,active,password_hash) VALUES(?,?,?,1,?)",
                [("usr_one", "sales-one", "sales", "fixture-hash"),
                 ("usr_two", "sales-two", "sales", "fixture-hash"),
                 ("usr_ops", "operator", "operator", "fixture-hash")])
            conn.executemany(
                "INSERT INTO freight_items(entry_id,content_key,record_type,source_group,message_time,status) "
                "VALUES(?,?,?,?,?,?)",
                [("frt_a", "a", "rates", "A group", "2026-09-29 10:00:00", "当前生效"),
                 ("frt_b", "b", "space", "B group", "2026-09-30 10:00:00", "当前有效")])
            for name in ("sales-one", "sales-two", "operator"):
                token = "fixture-" + name
                conn.execute("INSERT INTO auth_sessions(token_hash,operator_name,client_ip,expires_at,created_at) "
                             "VALUES(?,?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), name,
                                                 "127.0.0.1", int(time.time()) + 3600, manualdb.now_cn()))
            conn.commit()
        finally:
            conn.close()
        self.config = dict(manual_server.CFG)
        manual_server.CFG.update(db=str(db), backend="sqlite", token="fixture-agent")
        self.server = manual_server.FreightHTTPServer(("127.0.0.1", 0), manual_server.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        manual_server.CFG.update(self.config)
        self.temp.cleanup()

    def request(self, path, token=None, body=None, agent=False):
        headers = {"Origin": self.base}
        if token:
            headers["Cookie"] = "freight_session=fixture-" + token
        if agent:
            headers["x-push-token"] = "fixture-agent"
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                      headers=headers, method="POST" if body is not None else "GET")
        try:
            with urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_http_session_isolation_and_permission(self):
        path = "/api/sales/groups"
        self.assertEqual([r["name"] for r in self.request(path, "sales-one")[1]["data"]["rows"]],
                         ["B group", "A group"])
        self.assertEqual(self.request(SALES_GROUP_VISIT_ROUTE, "sales-one", {"name": "A group"})[1]["data"]["click_count"], 1)
        self.assertEqual([r["name"] for r in self.request(path, "sales-one")[1]["data"]["rows"]],
                         ["A group", "B group"])
        self.assertEqual([r["name"] for r in self.request(path, "sales-two")[1]["data"]["rows"]],
                         ["B group", "A group"])
        self.assertEqual(self.request(SALES_GROUP_VISIT_ROUTE, "operator", {"name": "A group"})[0], 200)
        self.assertEqual(self.request(SALES_GROUP_VISIT_ROUTE, body={"name": "A group"})[0], 401)
        self.assertEqual(self.request(SALES_GROUP_VISIT_ROUTE, body={"name": "A group"}, agent=True)[0], 403)
        self.assertEqual(self.request(SALES_GROUP_VISIT_ROUTE, "sales-one", {"name": "unknown"})[0], 400)


if __name__ == "__main__":
    unittest.main()
