"""Bulk approval source boundaries; only disposable databases are written."""
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch

import manualdb as db
from test_port_routes import seed, quote


def stage(conn, master, mid, ctype="20GP", bad=False, kind="rates", sender="Alice"):
    record = dict(quote(), **{"消息ID": mid or "legacy-" + uuid.uuid4().hex, "柜型": ctype, "来源群": "bulk-test", "发送人": sender})
    if kind == "space":
        record.update({"船名航次": "TEST V001", "舱位类型": "现舱", "价格USD": "100"})
    if bad:
        record["船司"] = "UNKNOWN-BULK-TEST"
    ids = db.stage_pending(conn, master, kind, [record], auto_suggest=False, preserve_other_sources=True)
    if len(ids) != 1:
        raise AssertionError("fixture did not stage exactly one row")
    account=conn.execute('SELECT publisher_id FROM operators WHERE name=?',(sender,)).fetchone()
    publisher=conn.execute('SELECT publisher_id FROM pending_records WHERE id=?',(ids[0],)).fetchone()[0]
    if account and account['publisher_id'] != publisher:
        from test_publisher_identity import binding
        binding(conn,sender,publisher)
    if not mid:
        stored = json.loads(conn.execute("SELECT payload FROM pending_records WHERE id=?", (ids[0],)).fetchone()[0])
        stored["消息ID"] = mid
        encoded = json.dumps(stored)
        conn.execute("UPDATE pending_records SET payload=?,orig_payload=? WHERE id=?", (encoded, encoded, ids[0]))
        conn.commit()
    return ids[0]


def verify_group_gate(test, conn):
    seed(conn)
    db.invalidate_master()
    master = db.get_master(conn)
    good = [stage(conn, master, "whole", ct) for ct in ("20GP", "40HQ/HC")]
    held = []
    for mid in ("mixed", "normalized", "cross-kind"):
        stored_mid = "msg//normalized==" if mid == "normalized" else mid
        held.append(stage(conn, master, stored_mid))
        stage(conn, master, mid, "40HQ/HC", bad=True, kind="space" if mid == "cross-kind" else "rates")
    good.append(stage(conn, master, "rejected-sibling"))
    rejected = stage(conn, master, "rejected-sibling", "40HQ/HC", bad=True)
    conn.execute("UPDATE pending_records SET review_status='rejected' WHERE id=?", (rejected,))
    conn.commit()
    good.extend(stage(conn, master, mid, ct) for mid, ct in (("", "20GP"), (None, "40HQ/HC")))
    before = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM pending_records")}
    eligible = db.list_pending(conn, check="passed", whole_passed=True)
    test.assertEqual(eligible["total"], len(good))
    test.assertEqual({r["id"] for r in eligible["rows"]}, set(good))
    test.assertEqual(db.list_pending(conn, check="passed")["total"], len(good) + len(held))
    test.assertEqual(db.list_pending(conn, kind="space", check="passed", whole_passed=True)["total"], 0)
    result = db.approve_all_pending(conn, master, reviewed_by="bulk-fixture")
    test.assertEqual(result["approved"], len(good), result)
    test.assertEqual(result["held_by_group"], len(held))
    test.assertEqual(result["remaining_passed"], 0)
    for pid in held:
        test.assertEqual(dict(conn.execute("SELECT * FROM pending_records WHERE id=?", (pid,)).fetchone()), before[pid])
    test.assertEqual(db.approve_all_pending(conn, master)["approved"], 0)


def verify_atomic_recheck(test, conn):
    seed(conn)
    db.invalidate_master()
    master = db.get_master(conn)
    first = stage(conn, master, "stale-group")
    stale = stage(conn, master, "stale-group", "40HQ/HC")
    fresh = stage(conn, master, "fresh-group", "40NOR")
    old = dict(conn.execute("SELECT * FROM pending_records WHERE id=?", (first,)).fetchone())
    payload = json.loads(conn.execute("SELECT payload FROM pending_records WHERE id=?", (stale,)).fetchone()[0])
    payload["船司"] = "UNKNOWN-BULK-TEST"
    conn.execute("UPDATE pending_records SET payload=? WHERE id=?", (json.dumps(payload), stale))
    conn.commit()
    result = db.approve_all_pending(conn, master, reviewed_by="bulk-fixture")
    test.assertEqual(result["approved"], 1, result)
    test.assertEqual({row["id"] for row in result["skipped"]}, {first, stale})
    test.assertEqual(result["held_by_group"], 1)
    test.assertEqual(dict(conn.execute("SELECT * FROM pending_records WHERE id=?", (first,)).fetchone()), old)
    test.assertEqual(conn.execute("SELECT check_status FROM pending_records WHERE id=?", (stale,)).fetchone()[0], "failed")
    released = {row[0] for row in conn.execute("SELECT entry_id FROM freight_items")}
    test.assertEqual(released, {conn.execute("SELECT entry_id FROM pending_records WHERE id=?", (fresh,)).fetchone()[0]})


def verify_ownership_gate(test, conn):
    seed(conn)
    for name in ("Alice", "Bob"):
        db.save_operator(conn, {"name": name, "role": "operator"})
    db.invalidate_master()
    master = db.get_master(conn)
    own = stage(conn, master, "shared-owner")
    stage(conn, master, "shared-owner", "40HQ/HC", sender="Bob")
    test.assertEqual(db.list_pending(conn, check="passed", viewer="Alice")["total"], 1)
    test.assertEqual(db.list_pending(conn, check="passed", viewer="Alice", whole_passed=True)["total"], 0)
    result = db.approve_all_pending(conn, master, viewer="Alice")
    test.assertEqual(result["approved"], 0)
    test.assertEqual(result["held_by_group"], 1)
    test.assertEqual(conn.execute("SELECT review_status FROM pending_records WHERE id=?", (own,)).fetchone()[0], "awaiting")
    test.assertEqual(db.approve_all_pending(conn, master)["approved"], 2)


def verify_http_bulk(test, connect):
    import manual_server
    conn = connect()
    try:
        seed(conn)
        for name in ("Alice", "Bob"):
            db.save_operator(conn, {"name": name, "role": "operator"})
        db.invalidate_master()
        master = db.get_master(conn)
        for ct in ("20GP", "40HQ/HC"):
            stage(conn, master, "http-whole", ct)
        stage(conn, master, "http-held")
        stage(conn, master, "http-held", "40HQ/HC", bad=True)
        stage(conn, master, "http-other", sender="Bob")
    finally:
        conn.close()
    session = {"user_id": "usr_bulk_fixture", "name": "Alice", "role": "operator"}
    with patch.object(manual_server, "conn", side_effect=connect), patch.object(manual_server.Handler, "_session", return_value=session), patch.object(manual_server, "log"):
        server = manual_server.FreightHTTPServer(("127.0.0.1", 0), manual_server.Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            base = "http://127.0.0.1:" + str(server.server_port)
            def call(path, payload=None):
                data = json.dumps(payload).encode() if payload is not None else None
                request = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request) as response:
                    return json.load(response)
            path = "/api/pending?status=awaiting&check=passed&kind=rates&limit=1"
            test.assertEqual(call(path)["total"], 3)
            test.assertEqual(call(path + "&whole_passed=1")["total"], 2)
            test.assertEqual(call(path + "&whole_passed=1&ownership=all")["total"], 3)
            result = call("/api/pending/approve_all", {"ownership": "mine"})
            test.assertEqual(result["approved"], 2)
            test.assertEqual(result["held_by_group"], 1)
            test.assertEqual(call(path + "&whole_passed=1")["total"], 0)
            test.assertEqual(call(path)["total"], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class WholeGroupBulkTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        db.invalidate_master()

    def test_whole_source_gate_and_preserved_checks(self):
        verify_group_gate(self, self.conn)

    def test_stale_check_rolls_back_whole_group(self):
        verify_atomic_recheck(self, self.conn)

    def test_ownership_does_not_cut_a_group(self):
        verify_ownership_gate(self, self.conn)

    def test_http_count_matches_release(self):
        with tempfile.TemporaryDirectory(prefix="bulk-whole-http-") as directory:
            path = Path(directory) / "isolated.db"
            def connect():
                connection = sqlite3.connect(path)
                connection.row_factory = sqlite3.Row
                return connection
            connection = connect()
            db.init_schema(connection)
            connection.close()
            verify_http_bulk(self, connect)


if __name__ == "__main__":
    unittest.main()
